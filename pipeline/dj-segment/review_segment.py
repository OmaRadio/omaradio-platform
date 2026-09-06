#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "python-dotenv",
# ]
# ///
"""
OmaRadio DJ Segment Review (MVP)
==================================

The Orchestrator's approval gate for generate_segment.py's output. Nothing
generate_segment.py produces reaches the media library -- and therefore can
never sync or go on-air -- until `approve` is run on it here.

Usage:
    uv run review_segment.py list [--dj dj-mox] [--status pending]
    uv run review_segment.py show <segment-id>
    uv run review_segment.py approve <segment-id> [--note "..."] [--by "..."]
    uv run review_segment.py reject <segment-id> --note "..."

`show` prints the script text and the absolute path to segment.mp3 -- play
it with whatever local player you already use (no bundled playback here).

`approve` copies (never moves -- the review folder stays as an audit trail)
segment.mp3 + a script sidecar into
    $LOCAL_LIBRARY/dj-segments/<dj>/generated/<segment-id>.{mp3,script.json}
From there, infra/transmitter/vault/sync-library.sh dj-segments pushes it to
the vault -- see README.md in this directory for the rest of the on-air
runbook. `reject` never touches the library, so a rejected take can never
sync or air.
"""

import argparse
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"
# OMARADIO_DB_PATH override -- same convention as LOCAL_LIBRARY, lets
# transmitter-one point this at /opt/omaradio/db/ (outside the platform/
# git checkout). Unset falls back to a repo-relative default for local use.
SCHEDULER_DB_PATH = Path(os.environ.get("OMARADIO_DB_PATH", str(REPO_ROOT / "scheduler" / "db" / "omaradio.sqlite3")))


def review_root() -> Path:
    local_library = Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))
    return local_library.parent / "review" / "dj-segments"


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def find_segment_dir(segment_id: str) -> Path:
    matches = list(review_root().glob(f"*/{segment_id}"))
    if not matches:
        print(f"[!] No segment found with id '{segment_id}' under {review_root()}", file=sys.stderr)
        raise SystemExit(1)
    if len(matches) > 1:
        print(f"[!] Ambiguous segment id '{segment_id}' -- found in multiple DJ folders:", file=sys.stderr)
        for m in matches:
            print(f"    {m}", file=sys.stderr)
        raise SystemExit(1)
    return matches[0]


def load_decision(seg_dir: Path) -> dict | None:
    path = seg_dir / "decision.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def default_by() -> str:
    return os.environ.get("REVIEWER_INITIALS", "CRH")


def cmd_list(args):
    root = review_root()
    if not root.exists():
        print(f"No segments yet -- {root} doesn't exist.")
        return
    dj_filter = args.dj
    found = False
    for dj_dir in sorted(root.iterdir()):
        if not dj_dir.is_dir() or (dj_filter and dj_dir.name != dj_filter):
            continue
        for seg_dir in sorted(dj_dir.iterdir()):
            script_path = seg_dir / "script.json"
            if not script_path.is_dir() and not script_path.exists():
                continue
            decision = load_decision(seg_dir)
            status = decision["status"] if decision else "pending"
            if args.status and status != args.status:
                continue
            data = json.loads(script_path.read_text(encoding="utf-8"))
            found = True
            print(f"{seg_dir.name}  [{status}]  {dj_dir.name}  \"{data.get('title', '?')}\"")
    if not found:
        print("No matching segments.")


def cmd_show(args):
    seg_dir = find_segment_dir(args.segment_id)
    data = json.loads((seg_dir / "script.json").read_text(encoding="utf-8"))
    decision = load_decision(seg_dir)
    print(f"Title:       {data.get('title')}")
    print(f"Est. length: ~{data.get('est_seconds')}s")
    meta = data.get("meta", {})
    print(f"DJ:          {meta.get('dj')}  (station: {meta.get('station')})")
    print(f"Brief:       {meta.get('brief')}")
    print(f"Model:       {meta.get('model')}")
    usage = meta.get("usage")
    if usage:
        print(f"Tokens:      {usage.get('input_tokens')} in / {usage.get('output_tokens')} out")
    print(f"Status:      {decision['status'] if decision else 'pending'}")
    print()
    print("--- script ---")
    print(data.get("script"))
    print("---------------")
    mp3_path = seg_dir / "segment.mp3"
    print(f"\nAudio: {mp3_path}" + (" (missing)" if not mp3_path.exists() else ""))


def record_mention(segment_id: str, data: dict, approved_by: str) -> None:
    """Best-effort approval-time usage tracking into scheduler/db's
    `mentions` table -- one row per successful approve, human or AUTO-*.

    Deliberately NOT shared/imported from scheduler/db/*.py (this repo's
    convention: duplicate small connection helpers rather than cross-import
    between differently-scoped scripts) and deliberately never allowed to
    fail the approval itself -- same pattern as send_email/send_pushover in
    auto_dj.py for a secondary, best-effort side-effect. `items` isn't
    backfilled yet as of this writing, so "no matching item" is the expected
    common case, not an error.
    """
    if not SCHEDULER_DB_PATH.exists():
        print(f"[i] No scheduler DB found at {SCHEDULER_DB_PATH} -- skipping mentions logging.")
        return

    try:
        meta = data.get("meta", {})
        dj_slug = meta.get("dj")

        conn = sqlite3.connect(SCHEDULER_DB_PATH)
        try:
            conn.execute("PRAGMA foreign_keys = ON;")

            dj_row = conn.execute("SELECT id, station_id FROM djs WHERE slug = ?", (dj_slug,)).fetchone()
            if not dj_row:
                print(f"[i] No djs row for slug '{dj_slug}' -- skipping mentions logging for {segment_id}.")
                return
            dj_id, station_id = dj_row

            # Candidate items.slug values to resolve to a surrogate item_id,
            # in priority order: a DJ's own topic first, then any news items
            # referenced (Vera's rundowns can reference several).
            candidate_slugs = []
            if meta.get("topic_id"):
                candidate_slugs.append(meta["topic_id"])
            candidate_slugs.extend(meta.get("news_item_ids") or [])

            item_id = None
            for slug in candidate_slugs:
                row = conn.execute("SELECT id FROM items WHERE slug = ?", (slug,)).fetchone()
                if row:
                    item_id = row[0]
                    break

            if item_id is None:
                print(
                    f"[i] No matching items row for {segment_id} "
                    f"(topic_id={meta.get('topic_id')!r}, news_item_ids={meta.get('news_item_ids')!r}) -- "
                    "skipping mentions log (expected until items is backfilled)."
                )
                return

            conn.execute(
                "INSERT INTO mentions (item_id, station_id, dj_id, segment_id, approved_by, occurred_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (item_id, station_id, dj_id, segment_id, approved_by, datetime.now(timezone.utc).isoformat()),
            )
            conn.commit()
            print(f"[+] Logged mentions row for {segment_id} (item_id={item_id}).")
        finally:
            conn.close()
    except Exception as exc:
        print(f"[!] Could not record mentions usage for {segment_id} -- approval still stands: {exc}", file=sys.stderr)


def cmd_approve(args):
    seg_dir = find_segment_dir(args.segment_id)
    if load_decision(seg_dir):
        print(f"[!] {args.segment_id} already has a decision -- not re-approving.", file=sys.stderr)
        raise SystemExit(1)

    data = json.loads((seg_dir / "script.json").read_text(encoding="utf-8"))
    dj_slug = data["meta"]["dj"]
    mp3_src = seg_dir / "segment.mp3"
    if not mp3_src.exists():
        print(f"[!] {mp3_src} doesn't exist -- can't approve a segment with no audio.", file=sys.stderr)
        raise SystemExit(1)

    dest_dir = local_library() / "dj-segments" / dj_slug / "generated"
    dest_dir.mkdir(parents=True, exist_ok=True)
    mp3_dest = dest_dir / f"{args.segment_id}.mp3"
    script_dest = dest_dir / f"{args.segment_id}.script.json"
    mp3_dest.write_bytes(mp3_src.read_bytes())
    script_dest.write_text(json.dumps(data, indent=2), encoding="utf-8")

    decision = {
        "status": "approved",
        "by": args.by or default_by(),
        "at": datetime.now(timezone.utc).isoformat(),
        "note": args.note or "",
    }
    (seg_dir / "decision.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")

    record_mention(args.segment_id, data, decision["by"])

    print(f"[+] Approved by {decision['by']}.")
    print(f"[+] Copied to {mp3_dest}")
    print("\nNext: infra/transmitter/vault/sync-library.sh -n dj-segments   (dry-run preview)")
    print("      infra/transmitter/vault/sync-library.sh dj-segments        (actual sync)")


def cmd_unapprove(args):
    seg_dir = find_segment_dir(args.segment_id)
    decision = load_decision(seg_dir)
    if not decision or decision["status"] != "approved":
        print(f"[!] {args.segment_id} is not currently approved -- nothing to undo.", file=sys.stderr)
        raise SystemExit(1)

    data = json.loads((seg_dir / "script.json").read_text(encoding="utf-8"))
    dj_slug = data["meta"]["dj"]
    dest_dir = local_library() / "dj-segments" / dj_slug / "generated"
    removed = []
    for dest in (dest_dir / f"{args.segment_id}.mp3", dest_dir / f"{args.segment_id}.script.json"):
        if dest.exists():
            dest.unlink()
            removed.append(dest)

    (seg_dir / "decision.json").unlink()

    print(f"[+] Unapproved {args.segment_id} -- back to pending.")
    for f in removed:
        print(f"[+] Removed {f}")
    print("\n[!] This only undoes the local library copy. If it was already synced to")
    print("    the vault (sync-library.sh) or symlinked into on-air/, remove it there too.")


def cmd_reject(args):
    seg_dir = find_segment_dir(args.segment_id)
    if load_decision(seg_dir):
        print(f"[!] {args.segment_id} already has a decision -- not re-rejecting.", file=sys.stderr)
        raise SystemExit(1)
    decision = {
        "status": "rejected",
        "by": args.by or default_by(),
        "at": datetime.now(timezone.utc).isoformat(),
        "note": args.note or "",
    }
    (seg_dir / "decision.json").write_text(json.dumps(decision, indent=2), encoding="utf-8")
    print(f"[+] Rejected by {decision['by']}. Nothing was copied to the library.")


def main():
    try:
        from dotenv import load_dotenv
        # Repo root is the documented location; also check next to this
        # script, since .env.example lives here too and it's an easy place
        # to accidentally `cp` a real .env into instead.
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="List generated segments")
    p_list.add_argument("--dj", help="Filter by DJ slug")
    p_list.add_argument("--status", choices=["pending", "approved", "rejected"], help="Filter by status")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Show a segment's script + audio path")
    p_show.add_argument("segment_id")
    p_show.set_defaults(func=cmd_show)

    p_approve = sub.add_parser("approve", help="Approve a segment and copy it into the library")
    p_approve.add_argument("segment_id")
    p_approve.add_argument("--note", default="")
    p_approve.add_argument("--by", default=None, help="Reviewer identity (default: CRH, or $REVIEWER_INITIALS)")
    p_approve.set_defaults(func=cmd_approve)

    p_unapprove = sub.add_parser("unapprove", help="Revert an approval -- removes it from the library, back to pending")
    p_unapprove.add_argument("segment_id")
    p_unapprove.set_defaults(func=cmd_unapprove)

    p_reject = sub.add_parser("reject", help="Reject a segment (never touches the library)")
    p_reject.add_argument("segment_id")
    p_reject.add_argument("--note", default="")
    p_reject.add_argument("--by", default=None)
    p_reject.set_defaults(func=cmd_reject)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
