#!/usr/bin/env python3
"""
OmaRadio scheduler DB -- media backfill
===========================================

Scans the vault's real media layout (vault_root/library/...) and
populates media (every kind: segment, track, outro, handoff) and tracks
(ID3 metadata for kind='track' rows). Idempotent -- upserts by path/
media_id, safe to re-run after adding new content (e.g. after a fresh
sync-library.sh push or generate_outros.py run).

Path conventions mirror build_playlist.py's own list_segments()/
list_station_outros()/list_handoffs()/list_music() exactly:
    library/dj-segments/<dj>/generated/*.mp3   -> kind='segment'
    library/dj-segments/<dj>/handoff/<tgt>/*.mp3 -> kind='handoff'
    library/jingles/station-outro/*.mp3        -> kind='outro'
    library/music/<genre>/*.mp3                -> kind='track' (+ tracks row)

A dj-segments/<slug>/ directory not found in the djs table (e.g. Alan's
or Relay's own occasional on-air appearances, which aren't part of the
DJ-rotation `djs` registry) gets dj_id=NULL rather than failing -- their
media still gets backfilled, just not attributed to a rotation DJ.

Usage:
    python3 backfill_media.py --vault-root /mnt/media_library [--db-path PATH]
"""

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

# OMARADIO_DB_PATH override -- same convention as LOCAL_LIBRARY, lets
# transmitter-one point this at /opt/omaradio/db/ (outside the platform/
# git checkout). Unset falls back to a repo-relative default for local use.
DEFAULT_DB_PATH = Path(os.environ.get("OMARADIO_DB_PATH", str(Path(__file__).resolve().parent / "omaradio.sqlite3")))

ID3_TAGS = ["artist", "title", "album", "genre", "copyright", "comment"]


def probe_duration(path: Path) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        print(f"[!] ffprobe invocation failed for {path}: {exc}", file=sys.stderr)
        return None
    try:
        return float(result.stdout.strip())
    except ValueError:
        return None


def probe_tags(path: Path) -> dict:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags", "-of", "default=noprint_wrappers=1", str(path)],
            capture_output=True, text=True, timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    tags = {}
    for line in result.stdout.splitlines():
        if line.startswith("TAG:"):
            key, _, value = line[4:].partition("=")
            tags[key.lower()] = value
    return tags


def upsert_media(conn, path: str, kind: str, dj_id: int | None, duration: float | None) -> int:
    conn.execute(
        "INSERT INTO media (path, kind, dj_id, duration_seconds, created_at) "
        "VALUES (?, ?, ?, ?, datetime('now')) "
        "ON CONFLICT(path) DO UPDATE SET duration_seconds = excluded.duration_seconds",
        (path, kind, dj_id, duration),
    )
    return conn.execute("SELECT id FROM media WHERE path = ?", (path,)).fetchone()[0]


def backfill_segments_and_handoffs(conn, vault_root: Path, dj_slugs: dict) -> tuple[int, int]:
    n_segments = n_handoffs = 0
    dj_segments_dir = vault_root / "library" / "dj-segments"
    if not dj_segments_dir.is_dir():
        return 0, 0
    for dj_dir in sorted(dj_segments_dir.iterdir()):
        if not dj_dir.is_dir():
            continue
        dj_id = dj_slugs.get(dj_dir.name)
        if dj_id is None:
            print(f"[i] {dj_dir.name} not in djs table (e.g. a non-rotation persona) -- backfilling with dj_id=NULL")

        generated_dir = dj_dir / "generated"
        if generated_dir.is_dir():
            for mp3 in sorted(generated_dir.glob("*.mp3")):
                rel = str(mp3.relative_to(vault_root))
                upsert_media(conn, rel, "segment", dj_id, probe_duration(mp3))
                n_segments += 1

        handoff_dir = dj_dir / "handoff"
        if handoff_dir.is_dir():
            for target_dir in sorted(handoff_dir.iterdir()):
                if not target_dir.is_dir():
                    continue
                for mp3 in sorted(target_dir.glob("*.mp3")):
                    rel = str(mp3.relative_to(vault_root))
                    upsert_media(conn, rel, "handoff", dj_id, probe_duration(mp3))
                    n_handoffs += 1
    return n_segments, n_handoffs


def backfill_station_outros(conn, vault_root: Path) -> int:
    d = vault_root / "library" / "jingles" / "station-outro"
    if not d.is_dir():
        return 0
    n = 0
    for mp3 in sorted(d.glob("*.mp3")):
        rel = str(mp3.relative_to(vault_root))
        upsert_media(conn, rel, "outro", None, probe_duration(mp3))
        n += 1
    return n


def backfill_tracks(conn, vault_root: Path) -> int:
    music_dir = vault_root / "library" / "music"
    if not music_dir.is_dir():
        return 0
    n = 0
    for mp3 in sorted(music_dir.rglob("*.mp3")):
        rel = str(mp3.relative_to(vault_root))
        genre = mp3.relative_to(music_dir).parts[0]
        duration = probe_duration(mp3)
        media_id = upsert_media(conn, rel, "track", None, duration)

        tags = probe_tags(mp3)
        artist = tags.get("artist")
        title = tags.get("title")
        album = tags.get("album")
        license_ = tags.get("copyright")
        attribution_url = None
        if tags.get("comment") and "http" in tags["comment"]:
            match = re.search(r"https?://\S+", tags["comment"])
            if match:
                attribution_url = match.group(0)
        metadata_source = "id3" if any([artist, title, album]) else None

        conn.execute(
            "INSERT INTO tracks (media_id, artist, title, album, genre, license, attribution_url, metadata_source) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(media_id) DO UPDATE SET "
            "  artist=excluded.artist, title=excluded.title, album=excluded.album, "
            "  genre=excluded.genre, license=excluded.license, "
            "  attribution_url=excluded.attribution_url, metadata_source=excluded.metadata_source",
            (media_id, artist, title, album, genre, license_, attribution_url, metadata_source),
        )
        n += 1
    return n


def backfill(vault_root: Path, db_path: Path) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("PRAGMA foreign_keys = ON;")
        dj_slugs = {row[0]: row[1] for row in conn.execute("SELECT slug, id FROM djs")}

        n_segments, n_handoffs = backfill_segments_and_handoffs(conn, vault_root, dj_slugs)
        n_outros = backfill_station_outros(conn, vault_root)
        n_tracks = backfill_tracks(conn, vault_root)
        conn.commit()

        print(f"[+] Backfilled {n_segments} segments, {n_handoffs} hand-offs, {n_outros} station outros, {n_tracks} tracks")
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--vault-root", type=Path, required=True)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH)
    args = parser.parse_args()
    backfill(args.vault_root, args.db_path)


if __name__ == "__main__":
    main()
