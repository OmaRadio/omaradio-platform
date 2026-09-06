#!/usr/bin/env python3
"""
OmaRadio scheduler DB -- migration runner
=============================================

Applies every .sql file under scheduler/db/migrations/ (sorted by
filename) that hasn't already been applied, tracked in a bookkeeping
table (_migrations) so re-running this is always safe. Stdlib-only
(sqlite3) -- same constraint as build_playlist.py, since this is meant
to run on transmitter-one alongside it.

Usage:
    python3 migrate.py [--db-path PATH]
"""

import argparse
import sqlite3
import sys
from pathlib import Path

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "omaradio.sqlite3"


def migrate(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS _migrations ("
            "  filename TEXT PRIMARY KEY,"
            "  applied_at TEXT NOT NULL DEFAULT (datetime('now'))"
            ");"
        )
        applied = {row[0] for row in conn.execute("SELECT filename FROM _migrations")}

        pending = sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.name not in applied)
        if not pending:
            print("No pending migrations.")
            return

        for path in pending:
            print(f"[*] Applying {path.name}...")
            conn.executescript(path.read_text(encoding="utf-8"))
            conn.execute("INSERT INTO _migrations (filename) VALUES (?)", (path.name,))
            conn.commit()
            print(f"[+] Applied {path.name}")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    migrate(args.db_path)


if __name__ == "__main__":
    main()
