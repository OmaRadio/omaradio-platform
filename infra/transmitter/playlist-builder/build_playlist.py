#!/usr/bin/env python3
"""
OmaRadio Playlist Builder
===========================

Rebuilds a station's `on-air/` symlink farm from already-approved,
already-synced content sitting in the media vault, then tells cliamp-server
to pick up the change. Runs unattended on transmitter-one via a systemd
timer (see ../../../deploy/omaradio-playlist-builder@.timer), once per
6-hour block (00:00, 06:00, 12:00, 18:00 UTC).

cliamp-server has no built-in scheduling and loops its playlist forever
sorted by plain string comparison on the file path -- both of which are
why this script exists and why on-air/ entries are zero-padded 3-digit
numbers (mixing digit widths would break the sort order outright).

Default mode is --reload-mode reload: `systemctl reload cliamp-server`
(SIGHUP) swaps the new playlist in live, with NO listener disconnection --
this only works on the github.com/choyer/cliamp-server hot-reload fork
we're currently running (see deploy/Makefile's header comment); a stock
upstream binary has no SIGHUP handler. --reload-mode restart falls back to
the old `systemctl restart` behavior (disconnects listeners) -- keep this
available as an escape hatch while the fork is still in trial use.

Stdlib only, deliberately -- transmitter-one runs no other Python and this
needs no third-party packages.

Usage (run on transmitter-one, or locally against --vault-root for testing):
    build_playlist.py --station one                          # build the current UTC block
    build_playlist.py --station one --block-start 2026-09-03T00:00:00Z
    build_playlist.py --station one --dry-run                # plan + print only, write nothing
    build_playlist.py --station one --plan-only               # write schedule JSON, don't touch on-air/
    build_playlist.py --station one --no-apply                # rebuild on-air/ but skip reload/restart entirely
    build_playlist.py --station one --reload-mode restart     # fall back to the old restart-based apply
    build_playlist.py --rebuild-from schedule/one/2026/09/03-0000.json   # replay an existing schedule

Requires ffprobe (part of ffmpeg) on PATH, and passwordless sudo for the
exact commands `systemctl reload cliamp-server` and `systemctl restart
cliamp-server` for the invoking user -- see
infra/transmitter/vault/on-air-place.sh's header comment for the sudoers
setup (this script reuses those same rules; no new privilege escalation).

IMPORTANT -- manual on-air-place.sh placements are ephemeral once this
timer is running: every 6-hour block does a FULL replace of on-air/, so an
ad hoc Alan/Relay appearance placed manually survives only until the next
scheduled boundary, then gets silently wiped. This script has no knowledge
of those placements.

Rollback: this script keeps exactly one prior generation as on-air.prev/
(sibling to on-air/, overwritten every run). To manually revert to it:
    rm -rf on-air && mv on-air.prev on-air && sudo systemctl reload cliamp-server
"""

import argparse
import json
import logging
import random
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_VAULT_ROOT = Path("/mnt/media_library")

# Which DJ owns each 6-hour UTC block, per station. None = open block
# (music-only, no DJ auto-selected). Add a station here to enable it --
# an unlisted station is a hard error, not a silent no-op.
STATION_SHIFTS = {
    "one": {0: "dj-mox", 6: None, 12: "dj-nova", 18: None},
}

BLOCK_SECONDS = 6 * 3600
MAX_ENTRIES = 999  # safety cap -- see header comment on why digit width matters


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def floor_to_block(dt: datetime) -> datetime:
    block_hour = (dt.hour // 6) * 6
    return dt.replace(hour=block_hour, minute=0, second=0, microsecond=0)


def parse_block_start(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"Invalid --block-start {s!r}, expected ISO8601 UTC e.g. 2026-09-03T00:00:00Z"
        )


def owning_dj(station: str, block_start: datetime) -> str | None:
    shifts = STATION_SHIFTS.get(station)
    if shifts is None:
        raise SystemExit(f"No shift schedule configured for station '{station}' -- add it to STATION_SHIFTS.")
    return shifts.get(block_start.hour)


def list_segments(vault_root: Path, dj: str) -> list[Path]:
    d = vault_root / "library" / "dj-segments" / dj / "generated"
    return sorted(d.glob("*.mp3")) if d.is_dir() else []


def list_music(vault_root: Path) -> list[Path]:
    d = vault_root / "library" / "music"
    return sorted(d.rglob("*.mp3")) if d.is_dir() else []


def probe_duration(path: Path) -> float | None:
    """Real track duration via ffprobe -- not a fixed heuristic. The music
    pool is small and uneven enough that a fixed per-track estimate risks
    badly over/undershooting the 6h block target."""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        logging.warning(f"ffprobe invocation failed for {path}: {exc}")
        return None
    if result.returncode != 0:
        logging.warning(f"ffprobe error for {path}: {result.stderr.strip()}")
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        logging.warning(f"ffprobe returned a non-numeric duration for {path}: {result.stdout!r}")
        return None


def pick_playable(pool: list[Path], last: Path | None) -> tuple[Path | None, float | None]:
    """Randomly pick from pool, excluding `last` when possible (anti
    immediate-repeat), retrying a different candidate if ffprobe fails on
    the pick. Returns (None, None) if every candidate in the pool fails to
    probe."""
    tried: set[Path] = set()
    while True:
        candidates = [p for p in pool if p not in tried]
        if not candidates:
            return None, None
        pickable = [p for p in candidates if p != last] if len(candidates) > 1 else candidates
        pick = random.choice(pickable)
        dur = probe_duration(pick)
        if dur is not None:
            return pick, dur
        logging.warning(f"Excluding unplayable candidate from this pick: {pick}")
        tried.add(pick)


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def make_entry(index: int, kind: str, dj: str | None, source_path: Path, duration: float) -> dict:
    return {
        "index": index,
        "type": kind,
        "dj": dj,
        "source_path": str(source_path),
        "duration_seconds": round(duration, 1),
        "on_air_filename": f"{index:03d}-{slugify(source_path.stem)}.mp3",
    }


def plan_block(vault_root: Path, station: str, block_start: datetime) -> dict:
    block_end = block_start + timedelta(seconds=BLOCK_SECONDS)
    dj = owning_dj(station, block_start)

    segment_pool = list_segments(vault_root, dj) if dj else []
    music_pool = list_music(vault_root)

    if not music_pool:
        raise SystemExit(
            f"FATAL: no music tracks found under {vault_root}/library/music/ -- aborting, nothing touched."
        )

    if dj and not segment_pool:
        logging.warning(
            f"No approved segments found for {dj} under library/dj-segments/{dj}/generated/ "
            "-- degrading this block to music-only."
        )

    entries: list[dict] = []
    total_seconds = 0.0
    last_segment: Path | None = None
    last_track: Path | None = None

    while total_seconds < BLOCK_SECONDS:
        if len(entries) >= MAX_ENTRIES:
            raise SystemExit(
                f"FATAL: exceeded {MAX_ENTRIES} on-air entries while only reaching "
                f"{total_seconds:.0f}s of {BLOCK_SECONDS}s -- likely a duration-probing bug. Aborting."
            )

        if segment_pool:
            seg, dur = pick_playable(segment_pool, last_segment)
            if seg is not None:
                entries.append(make_entry(len(entries) + 1, "segment", dj, seg, dur))
                total_seconds += dur
                last_segment = seg

        for _ in range(random.randint(3, 6)):
            if len(entries) >= MAX_ENTRIES or total_seconds >= BLOCK_SECONDS:
                break
            track, dur = pick_playable(music_pool, last_track)
            if track is None:
                break
            entries.append(make_entry(len(entries) + 1, "track", None, track, dur))
            total_seconds += dur
            last_track = track

    return {
        "schema_version": 1,
        "station": station,
        "block_start_utc": iso(block_start),
        "block_end_utc": iso(block_end),
        "owning_dj": dj,
        "generated_at_utc": iso(utc_now()),
        "estimated_duration_seconds": round(total_seconds, 1),
        "entries": entries,
    }


def write_schedule(vault_root: Path, station: str, block_start: datetime, schedule: dict) -> Path:
    schedule_dir = vault_root / "schedule" / station / f"{block_start.year:04d}" / f"{block_start.month:02d}"
    schedule_dir.mkdir(parents=True, exist_ok=True)
    path = schedule_dir / f"{block_start.day:02d}-{block_start.hour:02d}{block_start.minute:02d}.json"
    path.write_text(json.dumps(schedule, indent=2), encoding="utf-8")
    return path


def apply_cliamp_change(mode: str) -> bool:
    """mode is 'reload' (SIGHUP via `systemctl reload` -- no listener
    disconnect, only works on the choyer/cliamp-server hot-reload fork) or
    'restart' (the old behavior, disconnects listeners -- kept as a
    fallback while the fork is in trial use)."""
    verb = "reload" if mode == "reload" else "restart"
    result = subprocess.run(["sudo", "systemctl", verb, "cliamp-server"], capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"systemctl {verb} failed: {result.stderr.strip()}")
        return False
    logging.info(f"cliamp-server {verb}ed.")
    return True


def build_on_air(vault_root: Path, station: str, schedule: dict, apply_mode: str | None = "reload") -> None:
    on_air_dir = vault_root / "stations" / station / "on-air"
    staging_dir = on_air_dir.parent / "on-air.new"
    prev_dir = on_air_dir.parent / "on-air.prev"

    if staging_dir.exists():
        shutil.rmtree(staging_dir)
    staging_dir.mkdir(parents=True)

    written = 0
    for entry in schedule["entries"]:
        src = Path(entry["source_path"])
        if not src.exists():
            logging.warning(f"Schedule entry {entry['index']} references a missing source file, skipping: {src}")
            continue
        (staging_dir / entry["on_air_filename"]).symlink_to(src)
        written += 1

    if written == 0:
        shutil.rmtree(staging_dir)
        logging.error("No entries could be placed on-air (all source files missing) -- aborting, on-air/ left untouched.")
        raise SystemExit(1)

    if prev_dir.exists():
        shutil.rmtree(prev_dir)
    if on_air_dir.exists():
        on_air_dir.rename(prev_dir)
    staging_dir.rename(on_air_dir)
    logging.info(f"on-air/ rebuilt with {written} entries (prior generation kept at {prev_dir}).")

    if apply_mode is None:
        logging.info("--no-apply set -- on-air/ updated but cliamp-server was not told to pick it up.")
        return

    if not apply_cliamp_change(apply_mode):
        logging.error(
            f"on-air/ WAS already updated, but the cliamp-server {apply_mode} failed -- "
            f"run 'sudo systemctl {apply_mode} cliamp-server' manually."
        )
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--station", help="Station id, e.g. 'one' (must be a key in STATION_SHIFTS)")
    parser.add_argument("--block-start", type=parse_block_start, default=None,
                         help="Override the block start (ISO8601 UTC, e.g. 2026-09-03T00:00:00Z). "
                              "Defaults to the current 6-hour block based on real UTC time.")
    parser.add_argument("--vault-root", type=Path, default=DEFAULT_VAULT_ROOT,
                         help=f"Root of the media vault (default: {DEFAULT_VAULT_ROOT})")
    parser.add_argument("--dry-run", action="store_true", help="Plan and print only -- write nothing")
    parser.add_argument("--plan-only", action="store_true", help="Write the schedule JSON but don't touch on-air/")
    parser.add_argument("--no-apply", action="store_true",
                         help="Rebuild on-air/ but don't tell cliamp-server -- skips both reload and restart")
    parser.add_argument("--reload-mode", choices=["reload", "restart"], default="reload",
                         help="How to apply the change once on-air/ is rebuilt (default: reload). "
                              "'reload' needs the choyer/cliamp-server hot-reload fork; 'restart' is the "
                              "old listener-disconnecting fallback, useful if the fork misbehaves.")
    parser.add_argument("--rebuild-from", type=Path, default=None,
                         help="Skip planning; rebuild on-air/ directly from an existing schedule JSON file")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    apply_mode = None if args.no_apply else args.reload_mode

    if args.rebuild_from:
        schedule = json.loads(args.rebuild_from.read_text(encoding="utf-8"))
        if schedule.get("schema_version") != 1:
            raise SystemExit(f"Unsupported or missing schema_version in {args.rebuild_from}")
        build_on_air(args.vault_root, schedule["station"], schedule, apply_mode=apply_mode)
        return

    if not args.station:
        parser.error("--station is required (unless using --rebuild-from)")

    block_start = args.block_start or floor_to_block(utc_now())
    schedule = plan_block(args.vault_root, args.station, block_start)

    dj_label = schedule["owning_dj"] or "none (open block)"
    n_segments = sum(1 for e in schedule["entries"] if e["type"] == "segment")
    n_tracks = sum(1 for e in schedule["entries"] if e["type"] == "track")
    hours = schedule["estimated_duration_seconds"] / 3600
    logging.info(
        f"Block {schedule['block_start_utc']}-{schedule['block_end_utc']} station={args.station} dj={dj_label}: "
        f"{n_segments} segments / {n_tracks} tracks, ~{hours:.2f}h, {len(schedule['entries'])} entries total"
    )

    if args.dry_run:
        print(json.dumps(schedule, indent=2))
        return

    schedule_path = write_schedule(args.vault_root, args.station, block_start, schedule)
    logging.info(f"Wrote schedule: {schedule_path}")

    if args.plan_only:
        return

    build_on_air(args.vault_root, args.station, schedule, apply_mode=apply_mode)


if __name__ == "__main__":
    main()
