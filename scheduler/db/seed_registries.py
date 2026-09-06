#!/usr/bin/env python3
"""
OmaRadio scheduler DB -- registry seeder
============================================

Populates stations and djs from the real persona.toml files under
staff/stations/*/djs/*/ -- the actual source of truth for "what stations
and DJs exist," so this never needs manually-maintained duplicate data.
Idempotent: re-running upserts by slug rather than erroring on conflict,
so it's safe to run again after adding a new DJ/station.

Usage:
    python3 seed_registries.py [--db-path PATH]
"""

import argparse
import sqlite3
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = Path(__file__).resolve().parent / "omaradio.sqlite3"
DJS_GLOB = "staff/stations/*/djs/*/persona.toml"

# No display-name registry exists for stations today (persona.toml only
# carries the slug, e.g. "one") -- this is the one piece of human-facing
# data seeding has to supply directly rather than read from disk.
STATION_DISPLAY_NAMES = {
    "one": "OmaRadio One",
}


def seed(db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")

        station_slugs = set()
        djs = []
        for toml_path in sorted(REPO_ROOT.glob(DJS_GLOB)):
            with toml_path.open("rb") as f:
                persona = tomllib.load(f)
            station_slugs.add(persona["station"])
            djs.append(persona)

        for slug in sorted(station_slugs):
            name = STATION_DISPLAY_NAMES.get(slug, slug)
            conn.execute(
                "INSERT INTO stations (slug, name, created_at) VALUES (?, ?, datetime('now')) "
                "ON CONFLICT(slug) DO UPDATE SET name = excluded.name",
                (slug, name),
            )
            print(f"[+] station: {slug} ({name})")

        for persona in djs:
            station_id = conn.execute(
                "SELECT id FROM stations WHERE slug = ?", (persona["station"],)
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO djs (slug, station_id, name, active) VALUES (?, ?, ?, 1) "
                "ON CONFLICT(slug) DO UPDATE SET name = excluded.name, station_id = excluded.station_id",
                (persona["slug"], station_id, persona["name"]),
            )
            print(f"[+] dj: {persona['slug']} ({persona['name']}, station={persona['station']})")

        conn.commit()
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    seed(args.db_path)


if __name__ == "__main__":
    main()
