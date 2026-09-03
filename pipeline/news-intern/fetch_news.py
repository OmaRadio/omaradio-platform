#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "feedparser",
#     "anthropic",
#     "python-dotenv",
# ]
# ///
"""
Relay's News Fetcher (MVP -- web/RSS-Atom only)
==================================================

Polls Orchestrator-approved RSS/Atom feeds (sources.toml), stores new items
under $LOCAL_LIBRARY/news-desk/YYYY-MM-DD/, and tracks what's already been
seen so re-running only picks up genuinely new items. No approval gate --
this is research material one step removed from air (unlike DJ segments,
which go through review_segment.py), visible via `list`/`show` instead.

Summarization is category-driven, not uniform, to avoid spending API calls
where they're not needed:
  - category = "news"    -> the feed's own description is already a tight,
                             human-written summary. Used directly (light
                             HTML cleanup only), zero API cost.
  - category = "release"  -> the feed's content is a full HTML changelog,
                             not broadcast-ready. Gets one Claude call per
                             new item (default claude-haiku-4-5 -- this is
                             mechanical summarization, not creative writing,
                             so the cheaper tier is the right default; see
                             --model / $NEWS_INTERN_MODEL to override).

Usage:
    fetch_news.py fetch                          # poll all enabled sources
    fetch_news.py fetch --dry-run                # parse + print, write nothing, no LLM calls
    fetch_news.py fetch --source omarchy-news     # just one source
    fetch_news.py fetch --max-age-hours 24        # override the first-run/staleness cutoff (default 72)
    fetch_news.py list [--source ...] [--category ...] [--since YYYY-MM-DD]
    fetch_news.py show <item-id>

Feeds are Orchestrator-curated in sources.toml -- this script only ever
fetches from what's listed there with enabled = true. Requires
ANTHROPIC_API_KEY in a repo-root .env for "release"-category items (see
pipeline/dj-segment/.env.example -- same key, same loading convention).

Output reaches the vault via the existing, unmodified
infra/transmitter/vault/sync-library.sh news-desk category -- no changes
needed there.
"""

import argparse
import html
import json
import os
import re
import sys
import tomllib
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_FILE = Path(__file__).resolve().parent / "sources.toml"
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"
DEFAULT_MODEL = os.environ.get("NEWS_INTERN_MODEL", "claude-haiku-4-5")
DEFAULT_MAX_AGE_HOURS = 72
MAX_SEEN_IDS = 200


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def state_file_path() -> Path:
    return local_library().parent / "state" / "news-intern" / "seen.json"


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(text: str, max_len: int = 50) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def clean_html(text: str, max_len: int = 500) -> str:
    """Strip tags, decode entities, collapse whitespace, cap length --
    enough to make feed-provided description/content fields safe to store
    or hand to an LLM, without a full HTML parser dependency."""
    text = html.unescape(text or "")
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len].rsplit(" ", 1)[0] + "..."
    return text


def load_sources(only: str | None = None) -> list[dict]:
    with SOURCES_FILE.open("rb") as f:
        data = tomllib.load(f)
    sources = [s for s in data.get("source", []) if s.get("enabled", True)]
    if only:
        sources = [s for s in sources if s["slug"] == only]
    return sources


def load_seen() -> dict:
    path = state_file_path()
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_seen(seen_state: dict) -> None:
    path = state_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(seen_state, indent=2), encoding="utf-8")
    tmp.replace(path)


def parse_published(entry) -> datetime | None:
    struct = entry.get("published_parsed") or entry.get("updated_parsed")
    if not struct:
        return None
    import calendar
    return datetime.fromtimestamp(calendar.timegm(struct), tz=timezone.utc)


def summarize_release(title: str, body_text: str, model: str) -> str:
    import anthropic
    from pydantic import BaseModel

    class ReleaseSummary(BaseModel):
        summary: str

    client = anthropic.Anthropic()
    system = (
        "You summarize a software release changelog into a short, factual, "
        "neutral note meant to be read aloud on radio -- 2-3 sentences, "
        "plain prose, no markdown, no bullet points, no headers, no links. "
        "Stick to what's actually in the changelog; don't editorialize or "
        "invent details beyond it."
    )
    response = client.messages.parse(
        model=model,
        max_tokens=512,
        system=system,
        messages=[{"role": "user", "content": f"Release: {title}\n\n{body_text}"}],
        output_format=ReleaseSummary,
    )
    return response.parsed_output.summary


def fetch_source(source: dict, seen_state: dict, max_age_hours: int, model: str, dry_run: bool) -> list[dict]:
    import feedparser

    slug = source["slug"]
    feed = feedparser.parse(source["url"])
    if feed.bozo and not feed.entries:
        print(f"[!] [{slug}] feed parse error: {feed.get('bozo_exception')}", file=sys.stderr)
        return []

    entry = seen_state.setdefault(slug, {"seen_ids": [], "last_fetched_at": None})
    seen_ids = set(entry["seen_ids"])
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=max_age_hours)

    new_items = []
    newly_seen = []

    for e in feed.entries:
        entry_id = e.get("id") or e.get("guid") or e.get("link")
        if not entry_id or entry_id in seen_ids:
            continue
        newly_seen.append(entry_id)

        published = parse_published(e)
        if published and published < cutoff:
            # First-run/staleness guard: too old to be worth surfacing now,
            # but still marked seen so it never resurfaces later either.
            continue

        title = e.get("title", "(untitled)")
        link = e.get("link", "")

        if source["category"] == "release":
            content_list = e.get("content")
            raw_body = content_list[0].get("value", "") if content_list else e.get("summary", "")
            body_text = clean_html(raw_body, max_len=4000)
            if dry_run:
                summary = "(dry-run -- not summarized, no API call made)"
            else:
                summary = summarize_release(title, body_text, model)
            method = "llm"
        else:
            summary = clean_html(e.get("summary", ""), max_len=500)
            method = "direct"

        new_items.append({
            "source": slug,
            "source_name": source["name"],
            "category": source["category"],
            "title": title,
            "url": link,
            "published_at": iso(published) if published else None,
            "fetched_at": iso(now),
            "summary": summary,
            "summarization_method": method,
            "model": model if method == "llm" and not dry_run else None,
            "guid": entry_id,
        })

    entry["seen_ids"] = (newly_seen + entry["seen_ids"])[:MAX_SEEN_IDS]
    entry["last_fetched_at"] = iso(now)
    return new_items


def write_item(item: dict) -> Path:
    published = item["published_at"] or item["fetched_at"]
    dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    date_dir = local_library() / "news-desk" / dt.strftime("%Y-%m-%d")
    date_dir.mkdir(parents=True, exist_ok=True)

    item_id = f"{item['source']}-{slugify(item['title'])}"
    path = date_dir / f"{item_id}.json"
    suffix = 2
    while path.exists():
        path = date_dir / f"{item_id}-{suffix}.json"
        suffix += 1
    item["id"] = path.stem

    path.write_text(json.dumps(item, indent=2), encoding="utf-8")
    return path


def find_news_item(item_id: str) -> Path | None:
    matches = list((local_library() / "news-desk").glob(f"*/{item_id}.json"))
    return matches[0] if matches else None


def cmd_fetch(args):
    sources = load_sources(only=args.source)
    if not sources:
        print("[!] No enabled sources match.", file=sys.stderr)
        raise SystemExit(1)

    seen_state = load_seen()
    total = 0

    for source in sources:
        print(f"[*] Fetching {source['name']} ({source['slug']})...")
        items = fetch_source(source, seen_state, args.max_age_hours, args.model, args.dry_run)
        for item in items:
            if args.dry_run:
                print(f"  [dry-run] would store: \"{item['title']}\" ({item['summarization_method']})")
            else:
                path = write_item(item)
                print(f"  [+] {path.relative_to(local_library())}")
            total += 1
        if not items:
            print("  (nothing new)")

    print(f"\n{total} new item(s) {'would be ' if args.dry_run else ''}stored.")

    if not args.dry_run:
        save_seen(seen_state)
        print(f"Updated {state_file_path()}")


def cmd_list(args):
    news_dir = local_library() / "news-desk"
    if not news_dir.exists():
        print("No news items yet.")
        return

    found = False
    for date_dir in sorted(news_dir.iterdir()):
        if not date_dir.is_dir():
            continue
        if args.since and date_dir.name < args.since:
            continue
        for item_file in sorted(date_dir.glob("*.json")):
            item = json.loads(item_file.read_text(encoding="utf-8"))
            if args.source and item["source"] != args.source:
                continue
            if args.category and item["category"] != args.category:
                continue
            found = True
            print(f"{item['id']:<45} {date_dir.name}  {item['source']:<20} \"{item['title']}\"")
    if not found:
        print("No matching items.")


def cmd_show(args):
    path = find_news_item(args.item_id)
    if not path:
        print(f"[!] No news item found with id '{args.item_id}'", file=sys.stderr)
        raise SystemExit(1)
    print(path.read_text(encoding="utf-8"))


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    p_fetch = sub.add_parser("fetch", help="Poll enabled sources for new items")
    p_fetch.add_argument("--source", help="Only fetch this source slug")
    p_fetch.add_argument("--dry-run", action="store_true", help="Parse and print only -- no writes, no LLM calls")
    p_fetch.add_argument("--max-age-hours", type=int, default=DEFAULT_MAX_AGE_HOURS,
                          help=f"Items older than this are marked seen but not stored (default: {DEFAULT_MAX_AGE_HOURS})")
    p_fetch.add_argument("--model", default=DEFAULT_MODEL,
                          help=f"Anthropic model for release-category summarization (default: {DEFAULT_MODEL})")
    p_fetch.set_defaults(func=cmd_fetch)

    p_list = sub.add_parser("list", help="List stored news-desk items")
    p_list.add_argument("--source", help="Filter by source slug")
    p_list.add_argument("--category", help="Filter by category (news|release)")
    p_list.add_argument("--since", help="Only items on/after this date (YYYY-MM-DD)")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="Print one stored item's full JSON")
    p_show.add_argument("item_id")
    p_show.set_defaults(func=cmd_show)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
