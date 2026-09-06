#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "icalendar",
# ]
# ///
"""
Relay's Meetup Fetcher (MVP -- Luma calendar / ICS only)
=========================================================

Pulls the "Omarchy" community meetup calendar (a public Luma calendar,
https://luma.com/omarchy) as an ICS/iCalendar feed and stores each event
as a row in the scheduler DB's `items` table (category = 'meetup') --
NOT as news-desk JSON files like fetch_news.py, because meetups have a
fundamentally different lifecycle: dated future/past events that get
re-checked and can change (date moves, registration fills up), not
"did this get published recently, once" items.

Uses the `icalendar` library for real RFC 5545 parsing (line folding,
TZID/UTC handling, escaped text) rather than hand-rolling VEVENT
parsing -- ICS is fiddlier than it looks.

Storage is a SQLite UPSERT keyed by slug (derived from the ICS UID,
stable across re-fetches), so re-running this is always safe and
diffs cleanly:
  - `fetched_at` is set once, at first insert, and never touched again
    -- it marks when this item was first discovered.
  - `last_checked_at` is updated on every fetch, confirmed or not.
  - `event_start` / `event_end` / `registration_status` / `status` are
    refreshed from the feed every run, with a clear log line when a
    refresh actually changes one of them (a date moving, or
    registration flipping open -> waitlist -> sold_out) versus a
    plain unchanged re-confirmation -- these are meaningfully
    different pieces of information.
  - `status = 'archived'` is left alone if a row already has it (no
    "archived" transition logic exists yet; this fetcher never *sets*
    archived, and won't un-archive a row that got that status some
    other way).

No approval gate here, same reasoning as fetch_news.py: this is
curatable research/scheduling material, not broadcast audio.

Usage:
    fetch_meetups.py fetch                     # pull the calendar, upsert into items
    fetch_meetups.py fetch --dry-run           # parse + print, no DB writes
    fetch_meetups.py fetch --url <ics-url>     # override the calendar URL (testing)
    fetch_meetups.py list                      # upcoming (active) meetups, soonest first
    fetch_meetups.py list --all                # include past/archived too
    fetch_meetups.py show <slug>               # full stored row as JSON

Requires the scheduler DB to already exist (run
scheduler/db/migrate.py first) -- unlike optional secrets elsewhere in
this repo, there's no sensible silent no-op for "the table doesn't
exist yet."
"""

import argparse
import json
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = REPO_ROOT / "scheduler" / "db" / "omaradio.sqlite3"

LUMA_ICS_URL = "https://api.luma.com/ics/get?entity=calendar&id=cal-SDGGMsEps9ExsrT"
SOURCE_NAME = "luma-omarchy-calendar"

# Luma sits behind Cloudflare, which 403s Python's default
# Python-urllib/x.y User-Agent outright -- confirmed for real fetching
# this feed (curl with a browser-style UA went through fine, plain
# urllib.request.urlopen() with no header didn't). Same gotcha
# documented for Resend in pipeline/dj-segment/auto_dj.py -- an
# explicit User-Agent is load-bearing here too, don't drop it.
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) OmaRadio-NewsIntern/1.0"

# How long after an event's end (or start, if no end) it's still
# considered 'active' -- gives same-day recap mentions a window before
# an event flips to 'past'.
PAST_GRACE_HOURS = 24

# Hand-curated allowlist of notable/major world cities (and a few
# common abbreviations actually seen in this calendar's titles, e.g.
# "BLR", "NYC", "SF") used to flag `notable_location`. This is a
# starter list, not an algorithmically derived one -- edit it by hand
# as the calendar's real geographic spread becomes clearer. Matching
# is a case-insensitive substring check against the event's title +
# location text, so entries should be specific enough not to false-
# positive on unrelated words.
NOTABLE_CITIES = {
    "new york", "nyc", "san francisco", "sf bay area", "silicon valley",
    "los angeles", "seattle", "austin", "boston", "chicago", "miami",
    "toronto", "vancouver", "mexico city", "cdmx", "sao paulo",
    "são paulo", "buenos aires", "london", "berlin", "paris", "madrid",
    "barcelona", "amsterdam", "lisbon", "dublin", "zurich", "stockholm",
    "copenhagen", "warsaw", "vienna", "rome", "milan", "tel aviv",
    "dubai", "tokyo", "seoul", "singapore", "hong kong", "shanghai",
    "beijing", "shenzhen", "bangalore", "bengaluru", "blr", "mumbai",
    "delhi", "new delhi", "sydney", "melbourne",
}

# Registration-status detection is a heuristic, not a guarantee: it's
# a case-insensitive substring match against the event's title +
# description text. Luma's own "Sold Out"/"Waitlist" badges live on
# the calendar's web page, not reliably in the ICS feed's text fields
# -- as of this writing none of the 59 live events carry either
# phrase in their ICS text, so in practice almost everything currently
# resolves to 'open'. That's expected, not a bug: it just means this
# signal is sparse until organizers start writing it into their event
# description, at which point this catches it for free.
SOLD_OUT_MARKERS = ("sold out",)
WAITLIST_MARKERS = ("waitlist", "wait list", "wait-list")


def db_path_or_die(path: Path) -> Path:
    if not path.exists():
        print(
            f"[!] No database found at {path}.\n"
            f"    Run `python3 scheduler/db/migrate.py --db-path {path}` "
            f"first to create it.",
            file=sys.stderr,
        )
        raise SystemExit(1)
    return path


def connect_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.row_factory = sqlite3.Row
    return conn


def iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def slugify(text: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def slug_from_uid(uid: str) -> str:
    """Derive a stable, DB-safe slug from an ICS UID, e.g.
    'evt-YDyCEwwpiMzSFDB@events.lu.ma' -> 'luma-evt-ydycewwpimzsfdb'.
    Stripping the '@events.lu.ma' host part is fine -- it's constant
    across every event on this calendar, not part of the identity."""
    local_part = uid.split("@", 1)[0]
    return "luma-" + slugify(local_part, max_len=56)


def clean_text(text: str, max_len: int = 1000) -> str:
    text = re.sub(r"[ \t]+", " ", text or "").strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


def extract_luma_url(description: str) -> str | None:
    m = re.search(r"Get up-to-date information at:\s*(\S+)", description)
    return m.group(1) if m else None


def extract_organizer(description: str, component_organizer: str | None) -> str | None:
    m = re.search(r"Hosted by\s+(.+?)\s*$", description.strip(), flags=re.MULTILINE)
    if m:
        return m.group(1).strip()
    return component_organizer


def extract_address_block(description: str) -> str | None:
    """Pull the free-text address block Luma puts between 'Address:'
    and 'Hosted by' in DESCRIPTION -- the best fallback location text
    when LOCATION itself is just a luma.com event-page URL (true for
    roughly 1 in 3 events on this calendar, since not every organizer
    fills in a real address)."""
    m = re.search(r"Address:\s*\n(.+?)\n\s*\n\s*Hosted by", description, flags=re.DOTALL)
    if not m:
        return None
    block = m.group(1).strip()
    if not block or "check event page" in block.lower():
        return None
    return ", ".join(line.strip() for line in block.splitlines() if line.strip())


def resolve_location_text(location: str, description: str) -> str | None:
    """Best-effort human-readable location string. LOCATION is
    preferred when it's a real address (Luma geocodes most events that
    have one); otherwise fall back to the DESCRIPTION's address block;
    otherwise there's genuinely no location text to show, only the
    luma.com event page URL."""
    location = (location or "").strip()
    if location and not location.lower().startswith("http"):
        return location
    return extract_address_block(description)


def detect_registration_status(title: str, description: str) -> str:
    text = f"{title} {description}".lower()
    if any(marker in text for marker in SOLD_OUT_MARKERS):
        return "sold_out"
    if any(marker in text for marker in WAITLIST_MARKERS):
        return "waitlist"
    return "open"


def detect_notable_location(title: str, location_text: str | None) -> bool:
    haystack = f"{title} {location_text or ''}".lower()
    return any(city in haystack for city in NOTABLE_CITIES)


def compute_status(event_start: datetime, event_end: datetime | None, now: datetime) -> str:
    reference = event_end or event_start
    grace_cutoff = reference + timedelta(hours=PAST_GRACE_HOURS)
    return "active" if now <= grace_cutoff else "past"


def fetch_ics(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def parse_events(ics_bytes: bytes, now: datetime) -> list[dict]:
    import icalendar

    cal = icalendar.Calendar.from_ical(ics_bytes)
    events = []

    for comp in cal.walk("VEVENT"):
        uid = str(comp.get("UID") or "")
        if not uid:
            continue

        dtstart_prop = comp.get("DTSTART")
        if dtstart_prop is None:
            continue
        event_start = dtstart_prop.dt
        if not isinstance(event_start, datetime):
            # All-day (date-only) VEVENT -- doesn't occur on this
            # calendar today, but don't crash if it ever does.
            event_start = datetime.combine(event_start, datetime.min.time(), tzinfo=timezone.utc)
        if event_start.tzinfo is None:
            event_start = event_start.replace(tzinfo=timezone.utc)

        dtend_prop = comp.get("DTEND")
        event_end = None
        if dtend_prop is not None:
            event_end = dtend_prop.dt
            if isinstance(event_end, datetime):
                if event_end.tzinfo is None:
                    event_end = event_end.replace(tzinfo=timezone.utc)
            else:
                event_end = datetime.combine(event_end, datetime.min.time(), tzinfo=timezone.utc)

        title = clean_text(str(comp.get("SUMMARY") or "(untitled)"), max_len=200)
        raw_location = str(comp.get("LOCATION") or "")
        raw_description = clean_text(str(comp.get("DESCRIPTION") or ""), max_len=2000)

        organizer_prop = comp.get("ORGANIZER")
        component_organizer = None
        if organizer_prop is not None:
            component_organizer = organizer_prop.params.get("CN")

        geo_prop = comp.get("GEO")
        geo = None
        if geo_prop is not None:
            try:
                geo = {"lat": float(geo_prop.latitude), "lon": float(geo_prop.longitude)}
            except (AttributeError, TypeError, ValueError):
                geo = None

        location_text = resolve_location_text(raw_location, raw_description)
        luma_event_url = extract_luma_url(raw_description) or (
            raw_location if raw_location.lower().startswith("http") else None
        )
        organizer = extract_organizer(raw_description, component_organizer)

        summary = clean_text(location_text or raw_description, max_len=280)

        events.append({
            "uid": uid,
            "slug": slug_from_uid(uid),
            "title": title,
            "summary": summary,
            "event_start": event_start,
            "event_end": event_end,
            "location_text": location_text,
            "raw_location": raw_location or None,
            "raw_description": raw_description or None,
            "luma_event_url": luma_event_url,
            "organizer": organizer,
            "geo": geo,
            "registration_status": detect_registration_status(title, raw_description),
            "notable_location": detect_notable_location(title, location_text),
            "status": compute_status(event_start, event_end, now),
        })

    return events


def build_payload(event: dict) -> str:
    return json.dumps({
        "uid": event["uid"],
        "location": event["location_text"],
        "raw_location": event["raw_location"],
        "url": event["luma_event_url"],
        "organizer": event["organizer"],
        "geo": event["geo"],
        "description": event["raw_description"],
    })


def upsert_event(conn: sqlite3.Connection, event: dict, now_iso: str, dry_run: bool) -> str:
    """Insert or update one meetup row, returning a short verdict
    string for logging: 'new', 'updated:<fields>', or 'unchanged'."""
    slug = event["slug"]
    existing = conn.execute(
        "SELECT event_start, event_end, registration_status, status FROM items WHERE slug = ?",
        (slug,),
    ).fetchone()

    event_start_iso = iso(event["event_start"])
    event_end_iso = iso(event["event_end"]) if event["event_end"] else None

    if dry_run:
        if existing is None:
            return "new"
        changed = []
        if existing["event_start"] != event_start_iso:
            changed.append("event_start")
        if existing["event_end"] != event_end_iso:
            changed.append("event_end")
        if existing["registration_status"] != event["registration_status"]:
            changed.append("registration_status")
        return f"updated:{','.join(changed)}" if changed else "unchanged"

    conn.execute(
        """
        INSERT INTO items (
            slug, category, dj_scope_id, title, summary, payload, source,
            event_start, event_end, registration_status, notable_location,
            status, touch_policy, fetched_at, last_checked_at
        ) VALUES (
            :slug, 'meetup', NULL, :title, :summary, :payload, :source,
            :event_start, :event_end, :registration_status, :notable_location,
            :status, 'multi', :now, :now
        )
        ON CONFLICT(slug) DO UPDATE SET
            title               = excluded.title,
            summary             = excluded.summary,
            payload             = excluded.payload,
            source              = excluded.source,
            event_start         = excluded.event_start,
            event_end           = excluded.event_end,
            registration_status = excluded.registration_status,
            notable_location    = excluded.notable_location,
            -- Never resurrect an item some other mechanism archived --
            -- no 'archived' transition logic exists yet, but this
            -- fetcher must not undo it either. fetched_at is
            -- deliberately absent from this SET clause: it's an
            -- insert-only column, marking first discovery.
            status              = CASE WHEN items.status = 'archived'
                                        THEN items.status
                                        ELSE excluded.status END,
            last_checked_at     = excluded.last_checked_at
        """,
        {
            "slug": slug,
            "title": event["title"],
            "summary": event["summary"],
            "payload": build_payload(event),
            "source": SOURCE_NAME,
            "event_start": event_start_iso,
            "event_end": event_end_iso,
            "registration_status": event["registration_status"],
            "notable_location": 1 if event["notable_location"] else 0,
            "status": event["status"],
            "now": now_iso,
        },
    )

    if existing is None:
        return "new"

    changed = []
    if existing["event_start"] != event_start_iso:
        changed.append(f"event_start {existing['event_start']} -> {event_start_iso}")
    if existing["event_end"] != event_end_iso:
        changed.append(f"event_end {existing['event_end']} -> {event_end_iso}")
    if existing["registration_status"] != event["registration_status"]:
        changed.append(f"registration_status {existing['registration_status']} -> {event['registration_status']}")
    return f"updated: {'; '.join(changed)}" if changed else "unchanged"


def cmd_fetch(args):
    db_path = db_path_or_die(args.db_path)
    now = datetime.now(timezone.utc)
    now_iso = iso(now)

    print(f"[*] Fetching {args.url} ...")
    try:
        ics_bytes = fetch_ics(args.url)
    except Exception as e:
        print(f"[!] Failed to fetch calendar: {e}", file=sys.stderr)
        raise SystemExit(1)

    events = parse_events(ics_bytes, now)
    print(f"[*] Parsed {len(events)} event(s) from the feed.")

    # Connected even in --dry-run: upsert_event() needs to read the
    # existing row to compute a diff, it just skips the write.
    conn = connect_db(db_path)
    try:
        new_count = updated_count = unchanged_count = 0
        for event in sorted(events, key=lambda e: e["event_start"]):
            verdict = upsert_event(conn, event, now_iso, args.dry_run)
            label = f"{event['slug']:<40} \"{event['title']}\""
            if verdict == "new":
                new_count += 1
                print(f"  [+] new       {label}")
            elif verdict == "unchanged":
                unchanged_count += 1
                if args.verbose:
                    print(f"  [=] unchanged {label}")
            else:
                updated_count += 1
                print(f"  [~] {verdict.split(':', 1)[0]:<10}{label}")
                detail = verdict.split(":", 1)[1] if ":" in verdict else ""
                if detail:
                    print(f"        {detail}")

        if not args.dry_run:
            conn.commit()

        print(
            f"\n{new_count} new, {updated_count} updated, {unchanged_count} unchanged "
            f"{'(dry-run, no writes made)' if args.dry_run else f'-> {db_path}'}."
        )
    finally:
        if conn is not None:
            conn.close()


def cmd_list(args):
    db_path = db_path_or_die(args.db_path)
    conn = connect_db(db_path)
    try:
        query = "SELECT slug, title, payload, event_start, event_end, registration_status, status FROM items WHERE category = 'meetup'"
        params = []
        if not args.all:
            query += " AND status = 'active'"
        query += " ORDER BY event_start ASC"

        rows = conn.execute(query, params).fetchall()
        if not rows:
            print("No meetups found.")
            return

        for row in rows:
            payload = json.loads(row["payload"] or "{}")
            city = payload.get("location") or "(location unknown)"
            start = row["event_start"] or "?"
            print(f"{row['slug']:<40} {start:<21} [{row['status']:<7}] [{row['registration_status'] or '?':<9}] \"{row['title']}\" -- {city}")
    finally:
        conn.close()


def cmd_show(args):
    db_path = db_path_or_die(args.db_path)
    conn = connect_db(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM items WHERE category = 'meetup' AND slug = ?", (args.slug,)
        ).fetchone()
        if not row:
            print(f"[!] No meetup found with slug '{args.slug}'", file=sys.stderr)
            raise SystemExit(1)
        item = dict(row)
        item["payload"] = json.loads(item["payload"] or "{}")
        print(json.dumps(item, indent=2))
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db-path", type=Path, default=DEFAULT_DB_PATH,
                         help=f"Scheduler DB path (default: {DEFAULT_DB_PATH})")
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Pull the Luma calendar and upsert into items")
    p_fetch.add_argument("--url", default=LUMA_ICS_URL, help="Override the ICS feed URL (testing)")
    p_fetch.add_argument("--dry-run", action="store_true", help="Parse and print only -- no DB writes")
    p_fetch.add_argument("--verbose", action="store_true", help="Also print unchanged re-confirmations")
    p_fetch.set_defaults(func=cmd_fetch)

    p_list = sub.add_parser("list", help="List stored meetups, soonest first")
    p_list.add_argument("--all", action="store_true", help="Include past/archived meetups too")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Print one stored meetup's full row as JSON")
    p_show.add_argument("slug")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
