#!/usr/bin/env python3
"""
Vera Auto-Generator
=====================

Hands-off feeder for DJ Vera's news rundown: fetches the latest news,
figures out which items she hasn't covered yet, generates a segment for
them, and auto-approves it -- no human review step. This is a deliberate,
narrow exception to the rest of the platform's approval-gated pipeline (see
review_segment.py's own docstring), accepted specifically for Vera because
her content is strictly grounded in fetched facts per her persona's
boundaries (staff/stations/one/djs/dj-vera/persona.md) and can't
editorialize the way a freeform DJ segment could. Every other DJ still goes
through the manual generate -> review -> approve -> sync workflow.

Meant to run unattended on transmitter-one via systemd
(deploy/omaradio-vera-auto.service/.timer), timed 15min ahead of the
00:00/06:00/12:00 UTC block builds that can place her (see
STATION_PERIODIC_SEGMENTS / find_periodic_insertions() in
infra/transmitter/playlist-builder/build_playlist.py for why those three
times, not her nominal 00:00/08:00/16:00 slot hours -- pick_latest() is
evaluated once, at block-build time, so a fresh segment for the 08:00 slot
must exist by 06:00, not 08:00).

Deliberately stdlib-only, like build_playlist.py -- shells out to the
existing uv-run, PEP-723 scripts (fetch_news.py, generate_segment.py,
review_segment.py) for anything that needs a real dependency, rather than
pulling those dependencies into this script itself. Reads LOCAL_LIBRARY
from the environment (systemd's EnvironmentFile= on transmitter-one points
this straight at the vault -- see deploy/omaradio-vera-auto.service) using
the same default-and-override convention as the other scripts in this
pipeline, so it also runs locally against a disposable vault copy for
testing.

"Unused news items" is recomputed from scratch every run (recently-used ids
come from the last few approved Vera segments' own script.json metadata,
not a separate state file) -- self-healing, nothing to get out of sync.

Usage:
    uv run auto_vera.py                 # normal run
    uv run auto_vera.py --dry-run       # show what would be generated, no API calls, no writes
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"

DJ_SLUG = "dj-vera"
GENERATE_SCRIPT = Path(__file__).resolve().parent / "generate_segment.py"
REVIEW_SCRIPT = Path(__file__).resolve().parent / "review_segment.py"
FETCH_SCRIPT = REPO_ROOT / "pipeline" / "news-intern" / "fetch_news.py"


def find_uv() -> str:
    """Resolve an absolute path to `uv` for the subprocess calls below.
    This script is itself launched via `uv run`, but that does NOT put uv
    on PATH for child processes -- confirmed for real 2026-09-03, when the
    systemd unit (which sets ExecStart to uv's absolute path, since
    ~/.local/bin isn't on a systemd unit's PATH either) still failed with
    FileNotFoundError('uv') from these subprocess.run(["uv", ...]) calls.
    shutil.which() covers normal interactive/dev-machine use where uv IS on
    PATH; the fallback candidates cover systemd's minimal PATH."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path("/usr/local/bin/uv"), Path("/usr/bin/uv")):
        if candidate.is_file():
            return str(candidate)
    return "uv"  # last resort -- let subprocess raise a clear error


UV = find_uv()

RECENT_SEGMENTS_WINDOW = 5  # how many of Vera's most-recent segments count as "recently covered"
MAX_ITEM_AGE_HOURS = 72     # mirrors fetch_news.py's own --max-age-hours default
MAX_ITEMS_PER_RUNDOWN = 5   # bounds rundown length/cost

BRIEF_TEMPLATE = (
    "Deliver your regular news rundown covering these stories. Lead with "
    "whatever's most significant, close on the lightest one -- your usual "
    "structure, no special framing needed."
)


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def recently_used_item_ids() -> set[str]:
    generated_dir = local_library() / "dj-segments" / DJ_SLUG / "generated"
    if not generated_dir.is_dir():
        return set()
    script_files = sorted(generated_dir.glob("*.script.json"))[-RECENT_SEGMENTS_WINDOW:]
    used: set[str] = set()
    for path in script_files:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning(f"Couldn't read {path}, skipping: {exc}")
            continue
        used.update(data.get("meta", {}).get("news_item_ids") or [])
    return used


def find_unused_items(used_ids: set[str]) -> list[dict]:
    news_dir = local_library() / "news-desk"
    if not news_dir.is_dir():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_ITEM_AGE_HOURS)
    candidates = []
    for path in news_dir.glob("*/*.json"):
        try:
            item = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            logging.warning(f"Couldn't read {path}, skipping: {exc}")
            continue
        if item["id"] in used_ids:
            continue
        ts_raw = item.get("published_at") or item.get("fetched_at")
        try:
            ts = datetime.fromisoformat(ts_raw)
        except (TypeError, ValueError):
            continue
        if ts < cutoff:
            continue
        candidates.append((ts, item))
    candidates.sort(key=lambda pair: pair[0], reverse=True)
    return [item for _, item in candidates[:MAX_ITEMS_PER_RUNDOWN]]


def run_fetch(dry_run: bool) -> None:
    cmd = [UV, "run", str(FETCH_SCRIPT), "fetch"]
    logging.info(f"Running: {' '.join(cmd)}")
    if dry_run:
        return
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.warning(f"fetch_news.py fetch failed (continuing with whatever's already in news-desk/):\n{result.stderr.strip()}")
    else:
        logging.info(result.stdout.strip() or "fetch_news.py fetch: nothing new.")


def generate_segment(items: list[dict], dry_run: bool) -> str | None:
    cmd = [UV, "run", str(GENERATE_SCRIPT), "--dj", DJ_SLUG]
    for item in items:
        cmd += ["--news-item", item["id"]]
    cmd += ["--brief", BRIEF_TEMPLATE]

    logging.info(f"Running: {' '.join(cmd)}")
    if dry_run:
        return None

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"generate_segment.py failed:\n{result.stderr.strip()}")
        return None

    match = re.search(r"review_segment\.py show (\S+)", result.stdout)
    if not match:
        logging.error(f"Couldn't find a segment id in generate_segment.py's output:\n{result.stdout}")
        return None
    return match.group(1)


def approve_segment(segment_id: str, dry_run: bool) -> bool:
    cmd = [UV, "run", str(REVIEW_SCRIPT), "approve", segment_id, "--by", "AUTO-VERA"]
    logging.info(f"Running: {' '.join(cmd)}")
    if dry_run:
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"review_segment.py approve failed:\n{result.stderr.strip()}")
        return False
    logging.info(result.stdout.strip())
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true",
                         help="Show what would be generated -- no fetch, no API calls, no writes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    run_fetch(args.dry_run)

    used_ids = recently_used_item_ids()
    items = find_unused_items(used_ids)

    if not items:
        logging.info("Quiet cycle -- no unused news items for Vera. Skipping generation (her last rundown will keep replaying).")
        return

    logging.info(f"Found {len(items)} unused item(s) for Vera: {[item['id'] for item in items]}")

    if args.dry_run:
        logging.info("--dry-run set -- stopping before generation.")
        return

    segment_id = generate_segment(items, args.dry_run)
    if segment_id is None:
        sys.exit(1)

    if not approve_segment(segment_id, args.dry_run):
        sys.exit(1)

    logging.info(f"Done -- {segment_id} generated and auto-approved, covering: {[item['id'] for item in items]}")


if __name__ == "__main__":
    main()
