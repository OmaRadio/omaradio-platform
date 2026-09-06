#!/usr/bin/env python3
"""
OmaRadio Block Planner (song shoutouts)
===========================================

Pre-plans a station's NEXT 6-hour block -- track sequence, segment
placement, and (new here) song-shoutout slots -- ahead of the actual
block boundary, so a short DJ-voiced "shoutout" clip (see
pipeline/dj-segment/generate_shoutouts.py) can be generated and spliced
in while it still has time to render, instead of only at the moment the
block would otherwise go live.

Why this exists: a DJ segment's own script can't correctly say "next up,
X by Y" -- segments are pre-rendered into a reusable pool and picked
randomly at schedule-build time (see build_playlist.py's
pick_playable()), so nobody knows what track will actually follow a given
segment until the moment it's picked, possibly days after it was written.
This script keeps segments generic/reusable (unchanged -- the random
pool-picking in build_playlist.py's plan_block() is reused wholesale, not
touched), and separately identifies which specific tracks end up adjacent
to which segment placements in ONE concrete planned block, so a
disposable, slot-specific shoutout can name them correctly.

Deliberate, documented exception to this pipeline's usual "duplicate
small helpers, don't cross-import" convention (see e.g.
generate_shoutouts.py's/auto_dj.py's own comments on why THEY duplicate):
this script imports build_playlist.py directly (`import build_playlist`)
rather than copying its track/segment/outro/handoff/periodic-insertion
logic. That convention exists to avoid coupling dev-machine scripts to
transmitter-one-only scripts (or vice versa) across environments that
don't share a Python install -- it does not apply here, since this script
and build_playlist.py are a tightly-coupled producer/consumer pair (this
one PRODUCES a schedule JSON, build_playlist.py --rebuild-from CONSUMES
it), live in the same directory, run on the same host (transmitter-one),
and share the same stdlib-only constraint. Duplicating ~400 lines of
plan_block()'s own selection logic here instead would be a correctness
risk (two copies to keep in sync) for zero portability benefit.

Stdlib only, deliberately, same reasoning as build_playlist.py itself --
this script imports it directly, so it has to run under the same
plain-python3 (no PEP 723 / uv-managed venv) as build_playlist.py does.
The one non-stdlib piece of work (the actual Claude API call that writes
shoutout lines) is entirely delegated to generate_shoutouts.py via a `uv
run` subprocess, exactly the way auto_dj.py shells out to
generate_segment.py -- see find_uv()'s docstring for why an absolute path,
not bare "uv" on PATH, is used for that.

Never touches on-air/ and never calls build_playlist.py itself (not even
--rebuild-from) -- this script only PRODUCES a schedule JSON in the exact
shape build_playlist.py's own plan_block() returns (see write_schedule()
below), written to the same audit-trail path build_playlist.py would use
for that block. A future timer is expected to run
`build_playlist.py --rebuild-from <that path>` at the real block boundary
to actually materialize it -- building that wiring is out of scope here.

Must never look like "the timer failed": DB unavailable, no track
metadata, or generate_shoutouts.py erroring out are all ordinary,
expected outcomes that degrade to writing the exact plain schedule
plan_block() alone would have produced (today's status quo, zero
shoutouts) -- never a crash. See plan_shoutouts()'s docstring.

Usage:
    plan_block.py --station one                                  # plan the block AFTER the current one
    plan_block.py --station one --block-start 2026-09-06T00:00:00Z
    plan_block.py --station one --dry-run                        # plan + print only, no shoutouts, no writes
    plan_block.py --station one --vault-root /path/to/scratch-vault --db-path /path/to/scratch.sqlite3
"""

import argparse
import json
import logging
import re
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path

import build_playlist

REPO_ROOT = Path(__file__).resolve().parents[3]
GENERATE_SHOUTOUTS_SCRIPT = REPO_ROOT / "pipeline" / "dj-segment" / "generate_shoutouts.py"

# How long to give generate_shoutouts.py -- one Claude call (haiku-tier,
# a small batch of short lines) plus local Kokoro TTS rendering per slot.
# Generous on purpose: a slow run here just delays a pre-plan that still
# has hours of lead time before the block airs; there's no live deadline.
GENERATE_SHOUTOUTS_TIMEOUT = 900


def find_uv() -> str:
    """Identical to auto_dj.py's/auto_vera.py's own find_uv() -- duplicated,
    not imported, per this pipeline's standalone-script convention (the
    build_playlist.py cross-import above is a narrow, documented exception
    that doesn't extend to other scripts). Needed because this script may
    itself eventually run from systemd (no ~/.local/bin on PATH there, see
    the gotcha this fixed for real on 2026-09-03), and even interactively,
    `uv run <child>.py` doesn't put `uv` on PATH for its own subprocesses."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path("/usr/local/bin/uv"), Path("/usr/bin/uv")):
        if candidate.is_file():
            return str(candidate)
    return "uv"


UV = find_uv()


def sanitize_slot_id(slot_id: str, max_len: int = 100) -> str:
    """Identical to generate_shoutouts.py's sanitize_slot_id() -- duplicated,
    not imported, per this pipeline's standalone-script convention (see
    this file's module docstring for why build_playlist.py, specifically,
    is the one exception). Needed here to independently re-derive which
    <slot_id>.mp3 filename a manifest entry corresponds to."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", slot_id).strip("-")
    return safe[:max_len].rstrip("-") or "slot"


def default_shoutouts_dir(vault_root: Path, station: str, block_start) -> Path:
    """Where rendered shoutout clips + manifest.json land for this block.
    Under library/shoutouts/ -- reusing sync-library.sh's existing
    "shoutouts" vault category name (see infra/transmitter/vault/
    sync-library.sh) for a new, related-but-distinct use: that category
    was defined for dev-machine-generated shoutouts pushed up by
    sync-library.sh, whereas this script runs directly against the vault
    (same "generate in place, no sync step" pattern auto_dj.py/
    auto_vera.py already use) -- not literally synced, but the same
    library/ subtree makes sense as a home for this kind of disposable,
    DJ-voiced clip. NOT a reusable pool like station-outro/handoff -- one
    subdirectory per planned block, used once by this exact schedule then
    safe to garbage-collect later."""
    tag = block_start.strftime("%Y%m%d-%H%M")
    return vault_root / "library" / "shoutouts" / station / f"block-{tag}"


def _adjacent_track_entries(entries: list[dict], index: int, direction: int, max_count: int) -> list[dict]:
    """Walk from entries[index] in `direction` (+1 forward, -1 backward),
    collecting up to max_count entries of type "track", skipping over
    anything else (outro/handoff/other segments) along the way.

    Judgment call, documented here rather than asked about since it's an
    implementation detail, not a correctness ambiguity: plan_block()'s own
    splicing routinely puts a station outro and/or a periodic DJ's segment
    + hand-off directly after a segment (see STATION_OUTRO_CHANCE /
    STATION_PERIODIC_SEGMENTS in build_playlist.py) before the next real
    track resumes. A literal "the very next entry" reading would almost
    never find a track adjacent to a segment at all (75% of the time
    there's an outro in the way for station "one"). "The nearest actual
    track, skipping filler" is the only reading that makes the feature
    fire in practice, and it's also musically correct: the shoutout is
    meant to sit right at the boundary where real music resumes/just
    ended, wherever that boundary actually falls.
    """
    found: list[dict] = []
    i = index + direction
    while 0 <= i < len(entries) and len(found) < max_count:
        if entries[i]["type"] == "track":
            found.append(entries[i])
        i += direction
    return found


def lookup_track_metadata(conn, vault_root: Path, path: Path) -> dict | None:
    """Best-effort artist/title lookup for one track, by relative vault
    path, via the scheduler DB's tracks/media join (same join shape as
    build_playlist.py's own DB-backed lookups). Returns None -- "don't
    shout this one out" -- on a missing media/tracks row, a missing
    artist or title (an empty announcement isn't worth making), or any
    query failure."""
    try:
        rel = str(path.relative_to(vault_root))
    except ValueError:
        return None
    try:
        row = conn.execute(
            "SELECT t.artist, t.title FROM media m JOIN tracks t ON t.media_id = m.id WHERE m.path = ?",
            (rel,),
        ).fetchone()
    except Exception as exc:
        logging.warning(f"Track metadata lookup failed for {rel}: {exc}")
        return None
    if not row or not row[0] or not row[1]:
        return None
    return {"artist": row[0], "title": row[1]}


def identify_slots(entries: list[dict], owning_dj: str, vault_root: Path, conn,
                    station: str, block_start_iso: str) -> tuple[list[dict], dict]:
    """Finds shoutout-worthy segment placements in an already-planned
    block's entries list. Returns (slots, anchors):
      - slots: the JSON-serializable list generate_shoutouts.py expects
        (see its module docstring for the shape).
      - anchors: slot_id -> the specific entry dict the rendered clip
        should be spliced in front of, or None meaning "append at the end
        of the block" (only for a "recap" slot at the very tail, where by
        construction there's no following track to anchor to).

    Only considers segments belonging to the block's shift-owning DJ that
    aren't a periodic insertion (e.g. Vera) -- periodic DJs already have
    their own dedicated hand-off mechanism (list_handoffs() /
    generate_vera_handoffs.py), so giving them a shoutout slot too would
    be a redundant second "next up" mechanism for the same segment.

    A "recap" slot only gets created when _adjacent_track_entries()
    finds truly zero tracks anywhere after the segment for the rest of
    the block (see that function's skip-filler behavior) -- i.e. this is
    a genuine end-of-block condition, not just "the next entry happens to
    not be a track."
    """
    slots: list[dict] = []
    anchors: dict[str, dict | None] = {}

    for i, entry in enumerate(entries):
        if entry["type"] != "segment" or entry.get("dj") != owning_dj or entry.get("periodic"):
            continue

        following = _adjacent_track_entries(entries, i, +1, 1)
        preceding = _adjacent_track_entries(entries, i, -1, 2)

        if following:
            kind = "next"
            track_entries = following
            anchor: dict | None = following[0]
        elif preceding:
            kind = "recap"
            track_entries = list(reversed(preceding))  # oldest-played-first, per generate_shoutouts.py's slot format
            anchor = None  # nothing to anchor to -- append at the very end of the block
        else:
            continue  # no adjacent track at all in either direction -- nothing to shout out

        metas = []
        for te in track_entries:
            meta = lookup_track_metadata(conn, vault_root, Path(te["source_path"]))
            if meta is None:
                metas = None
                break
            metas.append(meta)
        if metas is None:
            # At least one referenced track has no known artist/title --
            # don't shout out an unknown song, and don't create a
            # half-formed slot naming only the other one either.
            continue

        slot_id = f"{station}-{block_start_iso}-{entry['index']}"
        slots.append({"slot_id": slot_id, "kind": kind, "tracks": metas})
        anchors[slot_id] = anchor

    return slots, anchors


def run_generate_shoutouts(dj: str, slots_file: Path, out_dir: Path, model: str | None) -> list[dict]:
    """Shells out to generate_shoutouts.py exactly the way auto_dj.py's
    generate_segment() shells out to generate_segment.py. Always returns
    a list (possibly empty) rather than raising -- every failure mode
    (can't invoke uv, timeout, non-zero exit, missing/unreadable
    manifest.json) degrades to "zero shoutouts for this block," never an
    exception. Even a non-zero exit is still worth checking manifest.json
    for -- generate_shoutouts.py writes it before its own final
    all-slots-rendered check, so a batch where only some clips failed to
    render still has a usable partial manifest on disk.
    """
    cmd = [UV, "run", str(GENERATE_SHOUTOUTS_SCRIPT), "--dj", dj,
           "--slots-file", str(slots_file), "--out-dir", str(out_dir)]
    if model:
        cmd += ["--model", model]
    logging.info(f"Running: {' '.join(cmd)}")

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=GENERATE_SHOUTOUTS_TIMEOUT)
    except Exception as exc:
        logging.warning(f"Failed to run generate_shoutouts.py -- degrading to zero shoutouts for this block: {exc}")
        return []

    if result.stdout.strip():
        logging.info(result.stdout.strip())
    if result.returncode != 0:
        logging.warning(
            f"generate_shoutouts.py exited {result.returncode} (some/all shoutouts may not have rendered): "
            f"{result.stderr.strip()}"
        )

    manifest_path = out_dir / "manifest.json"
    if not manifest_path.exists():
        return []
    try:
        return json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logging.warning(f"Could not read shoutout manifest at {manifest_path}: {exc}")
        return []


def splice_shoutouts(schedule: dict, entries: list[dict], anchors: dict, manifest: list[dict],
                      out_dir: Path, owning_dj: str) -> dict:
    """Builds a NEW schedule dict with rendered shoutouts spliced in and
    every entry's index/on_air_filename cleanly renumbered -- deliberately
    never mutates the `schedule`/`entries` passed in, so that if anything
    here raises partway through, the caller's original (shoutout-free)
    schedule is still intact to fall back to (see plan_shoutouts()).
    """
    insert_before: dict[int, list[dict]] = {}
    append_at_end: list[dict] = []
    placed = 0

    for item in manifest:
        slot_id = item.get("slot_id")
        if slot_id not in anchors:
            logging.warning(f"Shoutout manifest entry {slot_id!r} doesn't match any slot planned this run -- skipping.")
            continue
        mp3_path = out_dir / f"{sanitize_slot_id(slot_id)}.mp3"
        if not mp3_path.exists():
            logging.warning(f"Manifest lists {slot_id!r} but its audio file is missing at {mp3_path} -- skipping.")
            continue
        duration = build_playlist.probe_duration(mp3_path)
        if duration is None:
            logging.warning(f"Could not probe duration for rendered shoutout {mp3_path} -- skipping.")
            continue

        stub = {"_shoutout": True, "dj": owning_dj, "source_path": mp3_path, "duration": duration,
                "kind": item.get("kind")}
        anchor = anchors[slot_id]
        if anchor is None:
            append_at_end.append(stub)
        else:
            insert_before.setdefault(id(anchor), []).append(stub)
        placed += 1

    if placed == 0:
        logging.warning("No shoutout manifest entries could be spliced in -- block will air without them.")
        return schedule

    ordered_raw: list[dict] = []
    for entry in entries:
        ordered_raw.extend(insert_before.get(id(entry), []))
        ordered_raw.append(entry)
    ordered_raw.extend(append_at_end)

    final_entries = []
    for i, item in enumerate(ordered_raw, start=1):
        if item.get("_shoutout"):
            entry = build_playlist.make_entry(i, "shoutout", item["dj"], item["source_path"], item["duration"])
            entry["shoutout_kind"] = item["kind"]  # informational extra field -- harmless to --rebuild-from
        else:
            entry = build_playlist.make_entry(i, item["type"], item.get("dj"), Path(item["source_path"]), item["duration_seconds"])
            if item.get("periodic"):
                entry["periodic"] = True
        final_entries.append(entry)

    new_schedule = dict(schedule)
    new_schedule["entries"] = final_entries
    new_schedule["estimated_duration_seconds"] = round(sum(e["duration_seconds"] for e in final_entries), 1)
    new_schedule["generated_at_utc"] = build_playlist.iso(build_playlist.utc_now())
    logging.info(f"Spliced {placed} shoutout(s) into the block plan ({len(entries)} -> {len(final_entries)} entries).")
    return new_schedule


def plan_shoutouts(schedule: dict, vault_root: Path, station: str, block_start,
                    shoutouts_dir: Path | None, shoutouts_model: str | None) -> dict:
    """Top-level orchestrator: identify slots, generate audio, splice.
    Every branch that can't proceed logs a clear reason and returns
    `schedule` completely unchanged -- "a block airs without pre-planned
    shoutouts" is today's actual status quo (no shoutouts exist for any
    block yet) and must always be an acceptable, non-error outcome here,
    not something that blocks this script from finishing and writing a
    valid plan.
    """
    owning_dj = schedule["owning_dj"]
    entries = schedule["entries"]

    if not owning_dj:
        logging.info("No shift-owning DJ for this block (open/music-only) -- skipping shoutout planning.")
        return schedule

    conn = build_playlist._db_connect()
    if conn is None:
        logging.warning(
            "Scheduler DB unavailable -- skipping shoutout planning (block will air without pre-planned "
            "shoutouts, today's status quo)."
        )
        return schedule
    try:
        slots, anchors = identify_slots(entries, owning_dj, vault_root, conn, station, schedule["block_start_utc"])
    finally:
        conn.close()

    if not slots:
        logging.info(
            "No eligible shoutout slots for this block (no segment had an adjacent track with known "
            "artist/title) -- skipping generate_shoutouts.py."
        )
        return schedule

    out_dir = shoutouts_dir or default_shoutouts_dir(vault_root, station, block_start)
    out_dir.mkdir(parents=True, exist_ok=True)
    slots_file = out_dir / "slots.json"
    slots_file.write_text(json.dumps(slots, indent=2), encoding="utf-8")
    logging.info(f"Identified {len(slots)} shoutout slot(s) for {owning_dj} -- requesting audio via generate_shoutouts.py.")

    manifest = run_generate_shoutouts(owning_dj, slots_file, out_dir, shoutouts_model)
    if not manifest:
        logging.warning("generate_shoutouts.py produced no usable shoutouts -- block will air without them.")
        return schedule

    return splice_shoutouts(schedule, entries, anchors, manifest, out_dir, owning_dj)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--station", required=True, help="Station id, e.g. 'one' (must be a key in build_playlist.STATION_SHIFTS)")
    parser.add_argument("--block-start", type=build_playlist.parse_block_start, default=None,
                         help="Override which block to plan (ISO8601 UTC, e.g. 2026-09-06T00:00:00Z). "
                              "Defaults to the 6-hour block AFTER the current one -- this script needs lead "
                              "time to generate shoutouts before the block airs, so it deliberately never "
                              "plans the currently-airing block.")
    parser.add_argument("--vault-root", type=Path, default=build_playlist.DEFAULT_VAULT_ROOT,
                         help=f"Root of the media vault (default: {build_playlist.DEFAULT_VAULT_ROOT})")
    parser.add_argument("--db-path", type=Path, default=None,
                         help="Override the scheduler DB path (mainly for testing against a scratch DB) -- "
                              "monkeypatches build_playlist.DB_PATH before planning.")
    parser.add_argument("--shoutouts-dir", type=Path, default=None,
                         help="Where generate_shoutouts.py writes rendered clips + manifest.json for this "
                              "block (default: <vault-root>/library/shoutouts/<station>/block-<timestamp>/)")
    parser.add_argument("--shoutouts-model", default=None,
                         help="Passed through to generate_shoutouts.py's --model (default: that script's own default)")
    parser.add_argument("--dry-run", action="store_true",
                         help="Plan the base block and print it -- no shoutout generation (no API calls), no writes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.db_path is not None:
        build_playlist.DB_PATH = args.db_path

    block_start = args.block_start
    if block_start is None:
        current_block = build_playlist.floor_to_block(build_playlist.utc_now())
        block_start = current_block + timedelta(seconds=build_playlist.BLOCK_SECONDS)

    logging.info(f"Planning NEXT block for station={args.station}: {build_playlist.iso(block_start)}")

    # Reused wholesale, not reimplemented -- see this file's module
    # docstring for why. A genuinely fatal condition here (e.g. no music
    # at all under the vault) raises SystemExit exactly the way running
    # build_playlist.py itself would -- there's no plan to salvage in
    # that case, so mirroring its real failure mode is correct, not a
    # gap in this script's own graceful-degradation handling (which is
    # about the shoutout add-on below, not about whether a block is
    # plannable at all).
    schedule = build_playlist.plan_block(args.vault_root, args.station, block_start)

    dj_label = schedule["owning_dj"] or "none (open block)"
    n_segments = sum(1 for e in schedule["entries"] if e["type"] == "segment")
    logging.info(
        f"Base plan: {schedule['block_start_utc']}-{schedule['block_end_utc']} dj={dj_label}, "
        f"{n_segments} segment(s), {len(schedule['entries'])} entries total, "
        f"~{schedule['estimated_duration_seconds'] / 3600:.2f}h"
    )

    if args.dry_run:
        print(json.dumps(schedule, indent=2))
        return

    try:
        schedule = plan_shoutouts(schedule, args.vault_root, args.station, block_start,
                                   args.shoutouts_dir, args.shoutouts_model)
    except Exception:
        logging.exception(
            "Unexpected error while planning shoutouts -- falling back to the base plan (zero shoutouts). "
            "This must never look like a failed run: the schedule below is still valid and will still be written."
        )

    path = build_playlist.write_schedule(args.vault_root, args.station, block_start, schedule)
    logging.info(f"Wrote pre-planned schedule to {path}")
    n_shoutouts = sum(1 for e in schedule["entries"] if e["type"] == "shoutout")
    logging.info(f"Final entry count: {len(schedule['entries'])} ({n_shoutouts} shoutout(s)).")


if __name__ == "__main__":
    main()
