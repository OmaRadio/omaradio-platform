#!/usr/bin/env python3
"""
OmaRadio scheduler DB -- topics.toml migration
==================================================

One-time (but safe to re-run) migration of
pipeline/dj-segment/topics.toml's [[topic]] entries into the `items`
table (category='topic'), so Alan's topic-picking in auto_dj.py can read
from a real tracking store instead of scanning generated segments'
script.json meta to reconstruct "recently used" -- see
recently_used_topic_ids() in auto_dj.py for the workaround this replaces.

Idempotent: upserts by slug (the topic's own `id` field), so re-running
after adding new topics.toml entries only adds the new ones. Does NOT
delete items rows for topics removed from topics.toml -- an entry
someone deliberately restocked away should probably still be visible in
history (mentions may reference it), just no longer offered as a
candidate; a future cleanup pass can decide whether to actually prune
those, this script doesn't make that call.

`djs` in topics.toml is a list (today always 0 or 1 entries in
practice), but items.dj_scope_id is a single nullable FK -- if a topic
ever lists more than one DJ, only the first is used and a warning is
logged, since the schema doesn't support multi-DJ scoping today.

Usage:
    python3 migrate_topics.py [--db-path PATH] [--topics-file PATH]
"""

import argparse
import os
import sqlite3
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# OMARADIO_DB_PATH override -- same convention as LOCAL_LIBRARY, lets
# transmitter-one point this at /opt/omaradio/db/ (outside the platform/
# git checkout). Unset falls back to a repo-relative default for local use.
DEFAULT_DB_PATH = Path(os.environ.get("OMARADIO_DB_PATH", str(Path(__file__).resolve().parent / "omaradio.sqlite3")))
DEFAULT_TOPICS_FILE = REPO_ROOT / "pipeline" / "dj-segment" / "topics.toml"


def humanize(topic_id: str) -> str:
    return topic_id.replace("-", " ").strip().capitalize()


def migrate(db_path: Path, topics_file: Path) -> None:
    if not topics_file.exists():
        print(f"[!] {topics_file} not found.", file=sys.stderr)
        raise SystemExit(1)

    with topics_file.open("rb") as f:
        topics = tomllib.load(f).get("topic", [])

    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        n_inserted = n_updated = 0
        for topic in topics:
            topic_id = topic["id"]
            text = topic["text"]
            djs = topic.get("djs") or []
            dj_scope_id = None
            if djs:
                if len(djs) > 1:
                    print(f"[!] Topic '{topic_id}' lists multiple djs {djs} -- only '{djs[0]}' will be used "
                          "(items.dj_scope_id is a single FK, not multi-DJ today).", file=sys.stderr)
                row = conn.execute("SELECT id FROM djs WHERE slug = ?", (djs[0],)).fetchone()
                if row is None:
                    print(f"[!] Topic '{topic_id}' references unknown dj slug '{djs[0]}' -- leaving dj_scope_id NULL.",
                          file=sys.stderr)
                else:
                    dj_scope_id = row[0]

            existing = conn.execute("SELECT id FROM items WHERE slug = ?", (topic_id,)).fetchone()
            conn.execute(
                "INSERT INTO items (slug, category, dj_scope_id, title, summary, source, status, touch_policy, fetched_at) "
                "VALUES (?, 'topic', ?, ?, ?, 'topics.toml', 'active', 'multi', datetime('now')) "
                "ON CONFLICT(slug) DO UPDATE SET "
                "  dj_scope_id = excluded.dj_scope_id, title = excluded.title, summary = excluded.summary",
                (topic_id, dj_scope_id, humanize(topic_id), text),
            )
            if existing:
                n_updated += 1
            else:
                n_inserted += 1
        conn.commit()
        print(f"[+] Migrated {len(topics)} topics from {topics_file.name}: {n_inserted} new, {n_updated} updated.")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--topics-file", type=Path, default=DEFAULT_TOPICS_FILE)
    args = parser.parse_args()
    migrate(args.db_path, args.topics_file)


if __name__ == "__main__":
    main()
