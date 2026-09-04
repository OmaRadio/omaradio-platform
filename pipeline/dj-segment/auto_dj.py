#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic",
#     "python-dotenv",
#     "pydantic",
# ]
# ///
"""
Auto DJ (Mox / Nova)
=======================

Hands-off feeder for OmaRadio One's personality DJs. Unlike Vera (see
auto_vera.py, a news-anchor persona with a clean "unused fetched item"
signal), Mox and Nova are freeform, personality-driven radio -- per the
Spirit doc, "each DJ is their own creative director... writing their own
spoken content." What they need isn't a fact to recap, it's direction --
so this script has Alan (the Station Manager persona) pick a topic from a
human-curated backlog (topics.toml) and write an actual brief for the
target DJ, then generates and auto-approves a segment from it. No human
review step -- a deliberate, explicit choice (see the plan this was built
from), bigger than the same choice was for Vera: this content is freeform
and personality-driven, not fact-bound, so there's no factual grounding to
fall back on if something drifts off-persona.

Meant to run once a day on transmitter-one via systemd
(deploy/omaradio-dj-auto.service/.timer), covering both DJs in one process
-- their content sits in a pool pick_playable() cycles through randomly
across a whole 6h block (see build_playlist.py), not a precise per-slot
insertion like Vera's, so there's no shift-aligned timing to hit.

Unlike auto_vera.py (stdlib-only), this script needs the Anthropic SDK
directly for Alan's topic-pick-and-brief step -- a separate, cheap-tier
(claude-haiku-4-5) call in front of the existing segment-generation call
(claude-sonnet-5, unchanged, via generate_segment.py). Reads LOCAL_LIBRARY
from the environment same as every other script in this pipeline.

topics.toml needs your ongoing attention -- restock it periodically. A
DJ's cycle is skipped (with a clear warning) if every topic suited to them
has been used recently, rather than repeating stale ground.

Usage:
    uv run auto_dj.py --dj dj-mox            # single DJ
    uv run auto_dj.py --all                  # every configured DJ (used by the timer)
    uv run auto_dj.py --dj dj-mox --dry-run  # show Alan's pick + brief, no generation, no API cost beyond Alan's own call
"""

import argparse
import json
import logging
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"
SPIRIT_DOC = REPO_ROOT / "The-Spirit-of-OmaRadio.md"

STATION = "one"  # only station this repo supports today, same assumption STATION_SHIFTS/etc. already make
ALL_DJS = ["dj-mox", "dj-nova"]
ALAN_DIR = REPO_ROOT / "staff" / "stations" / STATION / "station-manager"

GENERATE_SCRIPT = Path(__file__).resolve().parent / "generate_segment.py"
REVIEW_SCRIPT = Path(__file__).resolve().parent / "review_segment.py"
TOPICS_FILE = Path(__file__).resolve().parent / "topics.toml"

RECENT_SEGMENTS_WINDOW = 5  # how many of a DJ's most-recent segments count as "recently covered"
ALAN_MODEL = "claude-haiku-4-5"  # cheap tier -- picking + drafting a brief is mechanical, not creative writing
LOW_STOCK_THRESHOLD = 2  # email when a DJ has this many or fewer unused topics left, every run while low


def send_email(subject: str, body: str) -> None:
    """Fire-and-forget notification via Resend's REST API -- stdlib
    urllib only, no new dependency. Never raises: a notification failure
    must never break the generation/approval this script is doing. No-ops
    quietly if RESEND_API_KEY isn't set (matches cliamp.env's
    optional-secret pattern -- notifications are opt-in, not required)."""
    api_key = os.environ.get("RESEND_API_KEY")
    if not api_key:
        logging.info("RESEND_API_KEY not set -- notifications not configured, skipping.")
        return
    import urllib.request
    payload = json.dumps({
        "from": os.environ.get("NOTIFY_FROM", "onboarding@resend.dev"),
        "to": [os.environ["NOTIFY_TO"]],
        "subject": subject,
        "text": body,
    }).encode()
    req = urllib.request.Request(
        "https://api.resend.com/emails", data=payload, method="POST",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            logging.info(f"Sent notification email ({resp.status}): {subject}")
    except Exception as exc:
        logging.warning(f"Failed to send notification email ({subject!r}): {exc}")


def find_uv() -> str:
    """Resolve an absolute path to `uv` -- see auto_vera.py's own copy of
    this for why (systemd's PATH doesn't include ~/.local/bin, and this
    script itself being launched via `uv run` doesn't put uv on PATH for
    its own child processes either)."""
    found = shutil.which("uv")
    if found:
        return found
    for candidate in (Path.home() / ".local" / "bin" / "uv", Path("/usr/local/bin/uv"), Path("/usr/bin/uv")):
        if candidate.is_file():
            return str(candidate)
    return "uv"


UV = find_uv()


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def dj_dir(dj_slug: str) -> Path:
    return REPO_ROOT / "staff" / "stations" / STATION / "djs" / dj_slug


def load_persona_md(persona_dir: Path) -> str:
    md_path = persona_dir / "persona.md"
    return md_path.read_text(encoding="utf-8") if md_path.exists() else ""


def load_topics() -> list[dict]:
    with TOPICS_FILE.open("rb") as f:
        data = tomllib.load(f)
    return data.get("topic", [])


def topics_for_dj(dj_slug: str) -> list[dict]:
    return [t for t in load_topics() if not t.get("djs") or dj_slug in t["djs"]]


def recently_used_topic_ids(dj_slug: str) -> set[str]:
    generated_dir = local_library() / "dj-segments" / dj_slug / "generated"
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
        topic_id = data.get("meta", {}).get("topic_id")
        if topic_id:
            used.add(topic_id)
    return used


def alan_pick_and_brief(dj_slug: str, dj_name: str, candidates: list[dict]) -> tuple[str, str] | None:
    import anthropic
    from pydantic import BaseModel

    class TopicChoice(BaseModel):
        topic_id: str
        brief: str

    spirit = SPIRIT_DOC.read_text(encoding="utf-8")
    alan_persona_md = load_persona_md(ALAN_DIR)
    dj_persona_md = load_persona_md(dj_dir(dj_slug))
    topic_list = "\n\n".join(f"- id: {t['id']}\n  seed: {t['text']}" for t in candidates)

    system_prompt = "\n\n".join([
        "You are Alan, OmaRadio One's Station Manager, choosing today's on-air direction for one of your DJs.",
        "=== The Spirit of OmaRadio (platform-wide) ===",
        spirit,
        "=== Your own persona (Alan) ===",
        alan_persona_md,
        f"=== The DJ you're briefing: {dj_name} ({dj_slug}) ===",
        dj_persona_md,
        "=== Available topic seeds (pick exactly one) ===",
        topic_list,
    ])
    user_message = (
        f"Pick the one topic seed above that's the best fit for {dj_name} right now, and write a short, "
        f"specific brief (a few sentences) in your own voice as Station Manager, directing {dj_name} on "
        "the angle to take. This is direction, not a script -- leave the actual writing and voice to them. "
        "Return the exact id of the topic seed you picked."
    )

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=ALAN_MODEL,
        max_tokens=1024,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_format=TopicChoice,
    )
    result = response.parsed_output
    if result.topic_id not in {t["id"] for t in candidates}:
        logging.error(f"Alan picked topic_id {result.topic_id!r}, which isn't one of the candidates offered -- aborting this cycle.")
        return None
    return result.topic_id, result.brief


def generate_segment(dj_slug: str, topic_id: str, brief: str, dry_run: bool) -> str | None:
    cmd = [UV, "run", str(GENERATE_SCRIPT), "--dj", dj_slug, "--brief", brief, "--topic-id", topic_id]
    logging.info(f"Running: {' '.join(cmd)}")
    if dry_run:
        return None

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"generate_segment.py failed:\n{result.stderr.strip()}")
        return None

    import re
    match = re.search(r"review_segment\.py show (\S+)", result.stdout)
    if not match:
        logging.error(f"Couldn't find a segment id in generate_segment.py's output:\n{result.stdout}")
        return None
    return match.group(1)


def approve_segment(segment_id: str, dj_slug: str, dry_run: bool) -> bool:
    by = f"AUTO-{dj_slug.removeprefix('dj-').upper()}"
    cmd = [UV, "run", str(REVIEW_SCRIPT), "approve", segment_id, "--by", by]
    logging.info(f"Running: {' '.join(cmd)}")
    if dry_run:
        return True

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        logging.error(f"review_segment.py approve failed:\n{result.stderr.strip()}")
        return False
    logging.info(result.stdout.strip())
    return True


def run_for_dj(dj_slug: str, dry_run: bool) -> None:
    persona_toml_path = dj_dir(dj_slug) / "persona.toml"
    dj_name = dj_slug
    if persona_toml_path.exists():
        with persona_toml_path.open("rb") as f:
            dj_name = tomllib.load(f).get("name", dj_slug)

    used = recently_used_topic_ids(dj_slug)
    candidates = [t for t in topics_for_dj(dj_slug) if t["id"] not in used]

    if len(candidates) <= LOW_STOCK_THRESHOLD:
        send_email(
            f"OmaRadio: topics.toml running low for {dj_name}",
            f"{dj_name} ({dj_slug}) has {len(candidates)} unused topic(s) left in topics.toml "
            f"(threshold: {LOW_STOCK_THRESHOLD}). Restock it soon.",
        )

    if not candidates:
        logging.warning(f"topics.toml exhausted for {dj_slug} -- every suitable entry was used recently. Restock it. Skipping this cycle.")
        return

    logging.info(f"{len(candidates)} candidate topic(s) available for {dj_name} ({dj_slug}).")
    picked = alan_pick_and_brief(dj_slug, dj_name, candidates)
    if picked is None:
        return
    topic_id, brief = picked
    logging.info(f"Alan picked '{topic_id}' for {dj_name}: {brief}")

    if dry_run:
        logging.info("--dry-run set -- stopping before generation.")
        return

    segment_id = generate_segment(dj_slug, topic_id, brief, dry_run)
    if segment_id is None:
        sys.exit(1)

    if not approve_segment(segment_id, dj_slug, dry_run):
        sys.exit(1)

    logging.info(f"Done -- {segment_id} generated and auto-approved for {dj_name}, topic: {topic_id}")


def main():
    try:
        from dotenv import load_dotenv
        # Same convention (and same must-run-before-argparse-defaults
        # ordering) as generate_segment.py/review_segment.py/fetch_news.py.
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--dj", help="Run for a single DJ slug (e.g. dj-mox)")
    group.add_argument("--all", action="store_true", help=f"Run for every configured DJ ({', '.join(ALL_DJS)})")
    parser.add_argument("--dry-run", action="store_true",
                         help="Show Alan's topic pick + brief -- no segment generation, no approval")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    djs = ALL_DJS if args.all else [args.dj]
    for dj_slug in djs:
        logging.info(f"--- {dj_slug} ---")
        run_for_dj(dj_slug, args.dry_run)


if __name__ == "__main__":
    main()
