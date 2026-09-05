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
    "one": {0: "dj-mox", 6: None, 12: "dj-nova", 18: "dj-nikon"},
}

# DJs who get exactly ONE segment inserted into whichever block contains
# their slot hour(s), independent of who (if anyone) owns that block.
# Deliberately a separate structure from STATION_SHIFTS, not merged in --
# STATION_SHIFTS is keyed by block_start.hour (only ever 0/6/12/18, the
# only values owning_dj() looks up) and encodes exclusive whole-block
# ownership; hours_utc here can be any hour (e.g. 8, 16) that ISN'T a
# valid STATION_SHIFTS key at all, and a block can have zero-to-N periodic
# insertions rather than exactly one owner. Looked up via .get(station, [])
# -- unlike owning_dj()'s hard-fail, an unlisted station here just means
# "no periodic DJs," which is a legitimate, not-a-bug station shape.
#
# With 6h blocks (starts at 0/6/12/18) and hours 8h apart, at most one
# periodic hour can ever fall inside a single block -- e.g. for "one":
# hour 0 -> the 00:00 block (offset 0s, i.e. right at block start), hour 8
# -> the 06:00 block (offset 7200s), hour 16 -> the 12:00 block (offset
# 14400s), and the 18:00 block gets NONE of the three -- confirmed
# algebraically, not just intended; see find_periodic_insertions().
STATION_PERIODIC_SEGMENTS = {
    "one": [{"dj": "dj-vera", "hours_utc": (0, 8, 16)}],
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


def find_periodic_insertions(station: str, block_start: datetime, block_end: datetime) -> list[tuple[str, int]]:
    """Returns (dj_slug, offset_seconds_from_block_start) for every
    periodic DJ whose configured UTC hour-of-day falls within
    [block_start, block_end). A list, not a single tuple|None, even though
    today's config provably yields at most one hit per block (6h blocks,
    8h-apart hours) -- costs nothing and doesn't silently drop a second
    periodic DJ if one's added later with a tighter/overlapping schedule.
    """
    hits = []
    for cfg in STATION_PERIODIC_SEGMENTS.get(station, []):
        for hour in cfg["hours_utc"]:
            candidate = block_start.replace(hour=hour, minute=0, second=0, microsecond=0)
            if block_start <= candidate < block_end:
                hits.append((cfg["dj"], int((candidate - block_start).total_seconds())))
    return hits


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


def pick_latest(pool: list[Path]) -> tuple[Path | None, float | None]:
    """For periodic DJs (see STATION_PERIODIC_SEGMENTS): pick the most
    recent segment, not a random one. pick_playable()'s anti-repeat
    exclusion only guards against repeating the immediately-previous pick
    WITHIN one plan_block() run -- a periodic DJ is picked at most once
    per run, so there's no in-run history to exclude against, and a
    random pick could silently repeat the same segment across separate
    runs (e.g. the same rundown airing at both 00:00 and 08:00) with zero
    protection. Picking the latest is simpler AND more correct for a
    "here's the latest news" persona: a freshly-approved segment is
    surfaced the moment it exists, and a quiet period predictably reuses
    the same most-recent segment rather than randomly resurfacing an
    older one. list_segments() already returns sorted(glob(...)), and
    segment filenames are timestamp-prefixed, so sorted order is
    chronological -- pool[-1] is the latest, reversed(pool) walks newest
    to oldest for probe-failure fallback."""
    for candidate in reversed(pool):
        dur = probe_duration(candidate)
        if dur is not None:
            return candidate, dur
        logging.warning(f"ffprobe failed on {candidate}, trying next-most-recent.")
    return None, None


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

    # Periodic DJs (e.g. a news anchor doing one rundown 3x/day, on a
    # cadence that doesn't align with block boundaries) get exactly one
    # segment spliced into this block's sequence once accumulated duration
    # reaches their configured offset -- see STATION_PERIODIC_SEGMENTS /
    # find_periodic_insertions(). Independent of block ownership above.
    periodic_state = []
    for periodic_dj, offset in find_periodic_insertions(station, block_start, block_end):
        periodic_pool = list_segments(vault_root, periodic_dj)
        if not periodic_pool:
            logging.warning(
                f"No approved segments found for periodic DJ {periodic_dj} -- "
                f"skipping this occurrence (offset {offset}s)."
            )
            continue
        periodic_state.append({"dj": periodic_dj, "offset": offset, "pool": periodic_pool, "inserted": False})

    def _maybe_insert_periodic():
        nonlocal total_seconds
        for st in periodic_state:
            if st["inserted"] or total_seconds < st["offset"]:
                continue
            st["inserted"] = True  # attempted either way -- never retried within this block
            seg, dur = pick_latest(st["pool"])
            if seg is None:
                logging.warning(f"All candidates failed to probe for periodic DJ {st['dj']} -- skipping this occurrence.")
                continue
            entry = make_entry(len(entries) + 1, "segment", st["dj"], seg, dur)
            entry["periodic"] = True
            entries.append(entry)
            total_seconds += dur
            logging.info(f"Inserted periodic segment for {st['dj']} at offset {st['offset']}s (block total now {total_seconds:.0f}s).")

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
                _maybe_insert_periodic()

        for _ in range(random.randint(3, 6)):
            if len(entries) >= MAX_ENTRIES or total_seconds >= BLOCK_SECONDS:
                break
            track, dur = pick_playable(music_pool, last_track)
            if track is None:
                break
            entries.append(make_entry(len(entries) + 1, "track", None, track, dur))
            total_seconds += dur
            last_track = track
            _maybe_insert_periodic()

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


def send_email(subject: str, body: str) -> None:
    """Fire-and-forget notification via Resend's REST API -- stdlib
    urllib only, keeping this script's stdlib-only constraint intact.
    Never raises: a notification failure must never block an on-air
    rebuild. No-ops quietly if RESEND_API_KEY isn't set (matches
    cliamp.env's optional-secret pattern -- notifications are opt-in)."""
    import os
    import urllib.error
    import urllib.request

    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logging.info("RESEND_API_KEY not set -- notifications not configured, skipping.")
        return
    payload = json.dumps({
        "from": os.environ.get("NOTIFY_FROM", "onboarding@resend.dev"),
        "to": [os.environ["NOTIFY_TO"]],
        "subject": subject,
        "text": body,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            # Resend sits behind Cloudflare, which blocks the default
            # Python-urllib/x.y User-Agent outright (error code 1010,
            # confirmed for real 2026-09-04 -- identical curl traffic with
            # no other differences went through fine).
            "User-Agent": "OmaRadio-Notify/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logging.info(f"Sent notification email ({resp.status}): {subject}")
    except urllib.error.HTTPError as exc:
        # Resend's actual error reason lives in the response body -- bare
        # str(exc) is just "HTTP Error 403: Forbidden" with no detail.
        detail = exc.read().decode("utf-8", "replace")
        logging.warning(f"Failed to send notification email ({subject!r}): {exc} -- {detail}")
    except Exception as exc:
        logging.warning(f"Failed to send notification email ({subject!r}): {exc}")


def send_pushover(title: str, message: str) -> None:
    """Fire-and-forget push notification via Pushover's REST API -- same
    stdlib-urllib-only, never-raises shape as send_email(). No-ops quietly
    if PUSHOVER_API_TOKEN/PUSHOVER_USER_KEY aren't set (same optional-secret
    pattern as RESEND_API_KEY -- this channel is opt-in too, independent of
    email)."""
    import os
    import urllib.error
    import urllib.parse
    import urllib.request

    api_token = os.environ.get("PUSHOVER_API_TOKEN")
    user_key = os.environ.get("PUSHOVER_USER_KEY")
    if not api_token or not user_key:
        logging.info("PUSHOVER_API_TOKEN/PUSHOVER_USER_KEY not set -- Pushover not configured, skipping.")
        return
    # Pushover hard-caps messages at 1024 chars -- truncate defensively
    # rather than let the API reject an over-length send outright.
    if len(message) > 1024:
        message = message[:1021] + "..."
    payload = urllib.parse.urlencode({
        "token": api_token, "user": user_key, "title": title, "message": message,
    }).encode()
    req = urllib.request.Request(
        "https://api.pushover.net/1/messages.json", data=payload, method="POST",
        headers={"User-Agent": "OmaRadio-Notify/1.0"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            # Pushover returns HTTP 200 even when it has nowhere to deliver
            # to (e.g. no device currently logged into the app) -- that
            # shows up as status != 1 or a non-empty "info" in the body,
            # not as an HTTP error, so the body has to be checked too
            # (confirmed for real 2026-09-04: a 200 with
            # {"info": "no active devices to send to", "status": 1} logged
            # as a silent "success" until this check was added).
            result = json.loads(resp.read().decode("utf-8", "replace"))
            if result.get("status") != 1 or result.get("info"):
                logging.warning(f"Pushover accepted but did not deliver ({title!r}): {result}")
            else:
                logging.info(f"Sent Pushover notification ({resp.status}): {title}")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        logging.warning(f"Failed to send Pushover notification ({title!r}): {exc} -- {detail}")
    except Exception as exc:
        logging.warning(f"Failed to send Pushover notification ({title!r}): {exc}")


def segment_title(source_path: Path) -> str | None:
    """Read a segment's own title out of its sibling script.json -- same
    directory, already on disk, no extra cost. Falls back to None (caller
    uses the on-air filename instead) if anything about that isn't there."""
    script_path = source_path.with_suffix("").with_suffix(".script.json")
    try:
        return json.loads(script_path.read_text(encoding="utf-8")).get("title")
    except (OSError, json.JSONDecodeError, AttributeError):
        return None


def notify_rebuild(schedule: dict) -> None:
    entries = schedule["entries"]
    n_tracks = sum(1 for e in entries if e["type"] == "track")
    n_segments = sum(1 for e in entries if e["type"] == "segment")
    block_start = datetime.strptime(schedule["block_start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)

    dj_segments: dict[str, list[tuple[datetime, str, float]]] = {}
    offset = 0.0
    for entry in entries:
        air_time = block_start + timedelta(seconds=offset)
        if entry["type"] == "segment" and entry["dj"]:
            title = segment_title(Path(entry["source_path"])) or entry["on_air_filename"]
            dj_segments.setdefault(entry["dj"], []).append((air_time, title, entry["duration_seconds"]))
        offset += entry["duration_seconds"]

    lines = [
        f"Station: {schedule['station']}",
        f"Block: {schedule['block_start_utc']} - {schedule['block_end_utc']}",
        f"Owning DJ: {schedule['owning_dj'] or 'none (open block)'}",
        f"Entries: {len(entries)} ({n_segments} segments / {n_tracks} tracks)",
        f"Estimated runtime: {schedule['estimated_duration_seconds'] / 3600:.2f}h",
        "",
        "DJ segments:",
    ]
    for dj, segs in dj_segments.items():
        lines.append(f"  {dj}:")
        for air_time, title, _duration in segs:
            lines.append(f"    ~{air_time.strftime('%H:%M')} UTC -- {title}")

    # Pushover's format is deliberately different from email's line-by-line
    # rundown above: a compact per-DJ stats summary (start/end/count/total
    # airtime), sized to comfortably clear Pushover's 1024-char cap even for
    # a busy multi-DJ block, rather than a truncated version of the email.
    subject = f"OmaRadio on-air rebuilt: {schedule['station']} {schedule['block_start_utc']}"
    if dj_segments:
        po_lines = []
        for dj, segs in dj_segments.items():
            start_time = segs[0][0]
            end_time = segs[-1][0] + timedelta(seconds=segs[-1][2])
            total_seconds = sum(duration for _, _, duration in segs)
            po_lines.append(
                f"{dj}: {len(segs)} seg, {total_seconds / 60:.1f}m, "
                f"{start_time.strftime('%H:%M')}-{end_time.strftime('%H:%M')} UTC"
            )
    else:
        po_lines = ["No DJ segments this block."]

    send_email(subject, "\n".join(lines))
    send_pushover(subject, "\n".join(po_lines))


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
    notify_rebuild(schedule)

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
