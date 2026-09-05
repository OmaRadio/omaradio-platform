-- OmaRadio scheduler/content database -- initial schema.
--
-- SQLite (stdlib `sqlite3`, no new dependency). Lives authoritatively on
-- transmitter-one; dev-machine tools reach it over SSH via a thin
-- script/CLI, never a network-mounted file -- SQLite's locking model
-- isn't safe over network filesystems (sshfs/NFS), which would defeat
-- the whole point of a single source of truth.
--
-- NOTE: SQLite does not enforce foreign keys by default. Every
-- connection that opens this database must run
--   PRAGMA foreign_keys = ON;
-- itself -- it's a per-connection setting, not something a migration
-- file can turn on permanently.
--
-- Design conventions used throughout this schema:
--   - Identity is always a meaningless surrogate INTEGER PRIMARY KEY.
--     Human-facing labels (slug, path, name) are unique but freely
--     editable attribute columns, never referenced by other tables --
--     confirmed necessary by real precedent: the dj-wax -> dj-nikon
--     rename changed a slug, not just a display name.
--   - "Unlisted = default, not an error" -- e.g. item_stations/
--     media_stations having no rows for something means it's relevant to
--     ALL stations, the same convention already used by
--     STATION_PERIODIC_SEGMENTS/STATION_OUTRO_CHANCE in build_playlist.py.
--   - Config that rarely changes (which sources exist, station shift
--     tables) stays in code/TOML; content that gets consumed, tracked,
--     and depleted over time lives here.

-- === Registries ============================================================

CREATE TABLE stations (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,  -- 'one' -- matches persona.toml's `station` field today, but can change
    name       TEXT NOT NULL,         -- 'OmaRadio One'
    created_at TEXT NOT NULL
);

CREATE TABLE djs (
    id         INTEGER PRIMARY KEY,
    slug       TEXT NOT NULL UNIQUE,  -- 'dj-mox' -- matches staff/stations/.../djs/<slug>/ on disk, but can change
    station_id INTEGER NOT NULL REFERENCES stations(id),
    name       TEXT NOT NULL,         -- 'Mox'
    active     INTEGER NOT NULL DEFAULT 1
);

-- === Content pool & media registry =========================================
-- Neither carries a station_id -- station relevance is a many-to-many
-- fact (item_stations/media_stations below), not ownership: a shared
-- resource (a music track, a general news item) can be relevant to more
-- than one station even though it's only ever scoped to one today.

CREATE TABLE items (
    id                   INTEGER PRIMARY KEY,
    slug                 TEXT NOT NULL UNIQUE,  -- 'omarchy-seoul-meetup-001' -- content id, but can be re-slugged
    category             TEXT NOT NULL,         -- 'topic' | 'news' | 'release' | 'meetup'
    dj_scope_id          INTEGER REFERENCES djs(id),  -- NULL = any DJ; set for topics like today's djs=["dj-mox"]
    title                TEXT NOT NULL,
    summary              TEXT,
    payload              TEXT,                  -- JSON: category-specific fields (city, url, organizer, ...)
    source               TEXT,                  -- which sources.toml entry this came from (informational)
    event_start          TEXT,                  -- NULL for timeless items (topics, news); set for meetups
    event_end            TEXT,
    registration_status  TEXT,                  -- 'open' | 'waitlist' | 'sold_out' -- NULL if not applicable
    notable_location     INTEGER NOT NULL DEFAULT 0,     -- 1 if from a curated "major city" allowlist
    status               TEXT NOT NULL DEFAULT 'active', -- 'active' | 'past' | 'archived'
    touch_policy         TEXT NOT NULL DEFAULT 'once',   -- 'once' | 'multi' -- informational, not enforced here
    fetched_at           TEXT NOT NULL,
    last_checked_at      TEXT                   -- last time this was re-verified against its source (diffing)
);

CREATE TABLE media (
    id                INTEGER PRIMARY KEY,
    path              TEXT NOT NULL UNIQUE,  -- relative vault path, e.g. "dj-segments/dj-mox/generated/....mp3"
    kind              TEXT NOT NULL,         -- 'segment' | 'track' | 'outro' | 'handoff' | 'jingle'
    dj_id             INTEGER REFERENCES djs(id),  -- NULL for tracks/station outros
    duration_seconds  REAL,                  -- cached so build_playlist.py stops re-running ffprobe every pick
    rotation_weight   REAL NOT NULL DEFAULT 1.0,  -- curated; scales how fast staleness accrues -- see plays below
    created_at        TEXT NOT NULL,
    synced_at         TEXT                   -- when sync-library.sh last confirmed it's on the vault
);

-- Extension table: only rows where media.kind = 'track'. Kept separate
-- from `media` rather than nullable columns on it, since none of this
-- applies to segments/outros/jingles.
CREATE TABLE tracks (
    media_id        INTEGER PRIMARY KEY REFERENCES media(id),
    artist          TEXT,
    title           TEXT,
    album           TEXT,
    genre           TEXT,      -- matches today's library/music/<genre>/ folder convention
    license         TEXT,      -- e.g. "CC BY-NC 2.1" -- attribution obligation, not just flavor
    attribution_url TEXT,      -- source page, when the tag carries one (freemusicarchive.org, bandcamp, ...)
    metadata_source TEXT       -- 'id3' | 'manual' | NULL if never resolved
);

-- === Relationships & ledgers ================================================

-- Which item(s) a piece of media traces back to: zero (a freeform riff),
-- one (a topic-driven segment), or several (Vera's multi-story rundown).
CREATE TABLE media_items (
    media_id  INTEGER NOT NULL REFERENCES media(id),
    item_id   INTEGER NOT NULL REFERENCES items(id),
    PRIMARY KEY (media_id, item_id)
);

-- No rows for a given item/media = relevant to ALL stations (today's
-- default, since there's only one). Explicit rows restrict to just those
-- stations once a second one exists and something needs it.
CREATE TABLE item_stations (
    item_id    INTEGER NOT NULL REFERENCES items(id),
    station_id INTEGER NOT NULL REFERENCES stations(id),
    PRIMARY KEY (item_id, station_id)
);

CREATE TABLE media_stations (
    media_id   INTEGER NOT NULL REFERENCES media(id),
    station_id INTEGER NOT NULL REFERENCES stations(id),
    PRIMARY KEY (media_id, station_id)
);

-- Approval-time usage ledger -- one row per review_segment.py approve
-- call (human or AUTO-*), the single instrumentation point both the
-- automated and manual paths already share.
CREATE TABLE mentions (
    id          INTEGER PRIMARY KEY,
    item_id     INTEGER NOT NULL REFERENCES items(id),
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    dj_id       INTEGER NOT NULL REFERENCES djs(id),
    segment_id  TEXT NOT NULL,              -- today's segment_id naming (timestamp-prefixed)
    approved_by TEXT NOT NULL,              -- 'AUTO-MOX' | 'AUTO-VERA' | a human name
    occurred_at TEXT NOT NULL
);

-- Air-time ledger, written locally by build_playlist.py on
-- transmitter-one (no SSH -- same host as the DB). Drives track-fairness
-- selection (weighted by media.rotation_weight) and "how many times has
-- item Y's content aired" (via a join through media_items, not a
-- duplicate write).
CREATE TABLE plays (
    id          INTEGER PRIMARY KEY,
    media_id    INTEGER NOT NULL REFERENCES media(id),
    station_id  INTEGER NOT NULL REFERENCES stations(id),
    block_start TEXT NOT NULL,              -- which schedule block, e.g. "2026-09-05T18:00:00Z"
    air_time    TEXT NOT NULL,              -- actual offset-computed air timestamp
    occurred_at TEXT NOT NULL               -- when build_playlist.py wrote this row
);

-- === Scheduling rules =======================================================

-- Time-of-day music constraints, independent of block/DJ ownership
-- (dj_id NULL = applies regardless of who's on) -- e.g. jazz only from
-- 02:00-04:00 during Mox's shift, without that being 1:1 with his whole
-- block. Filters the candidate pool BEFORE fairness/rotation-weighted
-- selection runs; not a separate selection mechanism, just a pre-filter
-- on the one that already exists.
CREATE TABLE genre_windows (
    id         INTEGER PRIMARY KEY,
    station_id INTEGER NOT NULL REFERENCES stations(id),
    dj_id      INTEGER REFERENCES djs(id),
    start_time TEXT NOT NULL,   -- 'HH:MM' UTC
    end_time   TEXT NOT NULL,   -- 'HH:MM' UTC
    genre      TEXT NOT NULL,   -- matches tracks.genre
    active     INTEGER NOT NULL DEFAULT 1
);
