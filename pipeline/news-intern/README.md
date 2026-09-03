# Relay's News Fetcher (MVP -- web/RSS-Atom only)

Watches Orchestrator-approved RSS/Atom feeds, stores new items under the
vault's `library/news-desk/`, and gives DJ segments a way to reference
real, fetched facts instead of hand-typed `--brief` text alone. This is
the first piece of Relay's documented-but-unbuilt job (see
`staff/stations/one/intern/persona.md`) -- Twitter/X and Discord are
separate, later phases, and the Intern's "Screening Rules" (referenced in
`The-Spirit-of-OmaRadio.md`, never defined) stay deferred: for this phase,
being on the Orchestrator-curated `sources.toml` list *is* the screening.

## How it fits together

```
fetch_news.py fetch
    -> polls each enabled [[source]] in sources.toml (RSS or Atom, feedparser)
    -> dedups against $LOCAL_LIBRARY/../state/news-intern/seen.json
    -> "news"-category items: feed description used directly, no API call
    -> "release"-category items: one Claude call to summarize (default claude-haiku-4-5)
    -> writes $LOCAL_LIBRARY/news-desk/YYYY-MM-DD/<source>-<title-slug>.json

pipeline/dj-segment/generate_segment.py --news-item <id|latest> [--news-item ...] --brief "..."
    -> pulls in one or more stored items as factual grounding
    -> --brief still sets the angle/tone/how prominently each item features
```

No approval gate on fetched items -- unlike DJ segments (which go through
`review_segment.py`), this is research material one step removed from air,
not final broadcast audio. Visibility is via `list`/`show` instead.

## Usage

```bash
# Fetch
uv run pipeline/news-intern/fetch_news.py fetch                       # poll all enabled sources
uv run pipeline/news-intern/fetch_news.py fetch --dry-run              # parse + print, no writes, no LLM calls
uv run pipeline/news-intern/fetch_news.py fetch --source omarchy-news  # just one source
uv run pipeline/news-intern/fetch_news.py fetch --max-age-hours 24     # override the staleness cutoff (default 72)

# Browse what's been fetched
uv run pipeline/news-intern/fetch_news.py list
uv run pipeline/news-intern/fetch_news.py list --category release
uv run pipeline/news-intern/fetch_news.py show <item-id>

# Use in a segment (from pipeline/dj-segment/)
uv run pipeline/dj-segment/generate_segment.py --list-news
uv run pipeline/dj-segment/generate_segment.py --dj dj-mox --news-item latest --brief "riff on this"
uv run pipeline/dj-segment/generate_segment.py --dj dj-mox \
    --news-item <id-1> --news-item <id-2> \
    --brief "mostly about the first one, only mention the second in passing"
```

Requires `ANTHROPIC_API_KEY` in a repo-root `.env` (same key/loading
convention as `pipeline/dj-segment/`) -- only spent on `"release"`-category
items; `"news"`-category items are free (the feed's own description is
used directly, just HTML-cleaned).

## Adding a source

Add a `[[source]]` block to `sources.toml`: `slug`, `name`, `url`,
`format` (`rss`/`atom`, informational only -- feedparser handles both the
same way), `category` (`"news"` for sources whose descriptions are
already usable as-is, `"release"` for sources needing real summarization),
`enabled`. Set `enabled = false` to pause a noisy source without losing
the config/reasoning for why it's there.

Only add sources with a real, working RSS/Atom feed -- this phase
deliberately doesn't do HTML scraping/diffing (fragile: breaks silently on
markup changes, no natural dedup key). If a source you want has no feed,
that's the trigger to design a scraping approach, not to force it into
this one.

## Dedup / state

`$LOCAL_LIBRARY/../state/news-intern/seen.json` (sibling to
`pipeline/dj-segment/`'s `review/` folder -- dev-machine run state, not
git-tracked, not vault content). Per source: up to 200 most-recent seen
item IDs plus a last-fetched timestamp. On a first run (or after adding a
new source), everything currently in the feed older than `--max-age-hours`
(default 72h) is marked seen but not stored -- avoids a backfill flood
while still converging to real-time behavior going forward.

## Reaching the vault

`news-desk` is already a supported category in
`infra/transmitter/vault/sync-library.sh` -- no changes needed there:

```bash
infra/transmitter/vault/sync-library.sh -n news-desk   # dry-run preview
infra/transmitter/vault/sync-library.sh news-desk        # actual sync
```

## Non-goals (this phase)

Twitter/X API and Discord connectors. Screening Rules content-level
filtering (source-list membership is the only screening for now). Generic
HTML scraping for feedless sources. Any approval gate on news-desk content.
Ambient/automatic news awareness in DJ segments without explicit
`--news-item` selection -- that's a future Station Manager automation
piece, not this one.
