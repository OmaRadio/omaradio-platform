#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic",
#     "kokoro-onnx",
#     "soundfile",
#     "numpy",
#     "python-dotenv",
#     "pydantic",
# ]
# ///
"""
OmaRadio Song Shoutout Generator
====================================

Generates short, DJ-voiced "song shoutout" clips that reference a specific
track (or the 1-2 tracks just played) by artist/title -- for use right
around a music transition, e.g. "That was Nihilore, with Blaxland" or
"Up next, something from LASVEGAS."

Why this is a separate script from generate_segment.py: a normal DJ
segment is written once and pooled for random reuse at schedule-build
time (see build_playlist.py's pick_playable()), so it can never correctly
name a specific upcoming/just-played track -- it doesn't know, at writing
time, what will actually be adjacent to it whenever it's eventually
picked, maybe days later. A shoutout inverts that: it's generated fresh
once a specific track sequence is actually known (by a not-yet-built
block-planner -- out of scope here, see the module docstring's caller
note below), tied to exactly one planned schedule slot, used once, then
discarded. NOT a reusable pool like generate_outros.py/
generate_vera_handoffs.py -- there's no "regenerate replaces the whole
pool" step here, every run produces a disposable, slot-specific batch.

One Claude call generates the whole batch (cheap, and avoids paying the
DJ-persona system-prompt cost once per shoutout) -- the same reasoning
generate_outros.py/generate_vera_handoffs.py already use for their pools.

Caller contract (the block-planner that doesn't exist yet): give this
script a JSON file of "slots" (each one, specific track(s) + a slot id
naming its planned schedule position), get back one rendered mp3 per
slot plus a manifest.json describing which file is which -- that's the
whole handoff. This script doesn't know or care about schedule times,
on-air placement, or block ownership; that's entirely the planner's job.

Slots file format (JSON array):
    [
      {"slot_id": "block-2026-09-06T00:00:00Z-slot-04",
       "kind": "next",
       "tracks": [{"artist": "Nihilore", "title": "Blaxland"}]},
      {"slot_id": "block-2026-09-06T00:00:00Z-slot-07",
       "kind": "recap",
       "tracks": [{"artist": "Syem", "title": "Song of Sky"},
                  {"artist": "LASVEGAS", "title": "GoldBlend #1"}]}
    ]

`kind` is "next" (tease an upcoming track) or "recap" (reference the 1-2
tracks just played -- recap tracks are listed oldest-played-first).

Usage:
    uv run generate_shoutouts.py --dj dj-mox --slots-file slots.json --out-dir out/
    uv run generate_shoutouts.py --dj dj-mox --slots-file slots.json --out-dir out/ --dry-run

Output: <out-dir>/<sanitized-slot-id>.mp3 per slot, plus a single
<out-dir>/manifest.json listing {slot_id, text, tracks, kind} for every
slot successfully rendered -- what a future block-planner reads to know
which file goes in which schedule position.

Setup: same as generate_segment.py (ANTHROPIC_API_KEY in .env, Kokoro
model weights, ffmpeg on PATH) -- see that script's docstring / this
directory's README.md.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DJS_GLOB = "staff/stations/*/djs/{slug}"
# Same fallback as generate_segment.py's find_dj_dir() -- roles other than
# DJ are singular per station, so match them by the `slug` field inside
# persona.toml instead of a djs/<slug>/-style subfolder. Duplicated, not
# imported, per this pipeline's standalone-script convention.
SINGULAR_ROLE_GLOB = "staff/stations/*/{role}"
SINGULAR_ROLES = ("station-manager", "intern")
SPIRIT_DOC = REPO_ROOT / "The-Spirit-of-OmaRadio.md"

# Same rationale/values as generate_segment.py's identical constants --
# shoutouts sit right next to music/segments in the on-air stream and need
# to match their loudness, not a generic external convention. Keep these
# copies in sync if the library's mastering is ever re-measured.
LOUDNORM_TARGET_LUFS = -12
LOUDNORM_TRUE_PEAK = -1.0
LOUDNORM_RANGE = 11

DEFAULT_KOKORO_DIR = Path.home() / ".cache" / "omaradio" / "kokoro"
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"

VALID_KINDS = ("next", "recap")


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def find_dj_dir(slug: str) -> Path:
    """Identical to generate_segment.py's find_dj_dir() -- duplicated, not
    imported, per this pipeline's standalone-script convention."""
    matches = list(REPO_ROOT.glob(DJS_GLOB.format(slug=slug)))
    if not matches:
        matches = _find_singular_role_dir(slug)
    if not matches:
        print(f"[!] No staff persona found for slug '{slug}' under staff/stations/*/", file=sys.stderr)
        print(f"    Expected either staff/stations/<station>/djs/{slug}/persona.toml", file=sys.stderr)
        print(f"    or a persona.toml with slug = \"{slug}\" under station-manager/ or intern/", file=sys.stderr)
        raise SystemExit(1)
    return matches[0]


def _find_singular_role_dir(slug: str) -> list[Path]:
    matches = []
    for role in SINGULAR_ROLES:
        for role_dir in REPO_ROOT.glob(SINGULAR_ROLE_GLOB.format(role=role)):
            toml_path = role_dir / "persona.toml"
            if not toml_path.exists():
                continue
            try:
                with toml_path.open("rb") as f:
                    data = tomllib.load(f)
            except tomllib.TOMLDecodeError:
                continue
            if data.get("slug") == slug:
                matches.append(role_dir)
    return matches


def load_persona(dj_dir: Path) -> tuple[dict, str]:
    """Identical to generate_segment.py's load_persona() -- duplicated, not
    imported, per this pipeline's standalone-script convention."""
    toml_path = dj_dir / "persona.toml"
    md_path = dj_dir / "persona.md"
    if not toml_path.exists():
        print(f"[!] Missing {toml_path}", file=sys.stderr)
        raise SystemExit(1)
    with toml_path.open("rb") as f:
        persona = tomllib.load(f)
    persona_md = md_path.read_text(encoding="utf-8") if md_path.exists() else ""
    for field in ("name", "slug", "station", "voice", "lang"):
        if field not in persona:
            print(f"[!] {toml_path} is missing required field '{field}'", file=sys.stderr)
            raise SystemExit(1)
    return persona, persona_md


def load_slots(path: Path) -> list[dict]:
    if not path.exists():
        print(f"[!] Slots file not found: {path}", file=sys.stderr)
        raise SystemExit(1)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list) or not data:
        print(f"[!] {path} must contain a non-empty JSON array of slot objects.", file=sys.stderr)
        raise SystemExit(1)

    seen_ids = set()
    for i, slot in enumerate(data):
        for field in ("slot_id", "kind", "tracks"):
            if field not in slot:
                print(f"[!] Slot #{i} in {path} is missing required field '{field}': {slot!r}", file=sys.stderr)
                raise SystemExit(1)
        if slot["kind"] not in VALID_KINDS:
            print(f"[!] Slot '{slot['slot_id']}' has invalid kind {slot['kind']!r} -- must be one of {VALID_KINDS}", file=sys.stderr)
            raise SystemExit(1)
        if not slot["tracks"] or not isinstance(slot["tracks"], list):
            print(f"[!] Slot '{slot['slot_id']}' has an empty/invalid 'tracks' list.", file=sys.stderr)
            raise SystemExit(1)
        for track in slot["tracks"]:
            if "artist" not in track or "title" not in track:
                print(f"[!] Slot '{slot['slot_id']}' has a track missing artist/title: {track!r}", file=sys.stderr)
                raise SystemExit(1)
        if slot["slot_id"] in seen_ids:
            print(f"[!] Duplicate slot_id in {path}: {slot['slot_id']!r}", file=sys.stderr)
            raise SystemExit(1)
        seen_ids.add(slot["slot_id"])

    return data


def build_system_prompt(persona: dict, persona_md: str) -> str:
    spirit = SPIRIT_DOC.read_text(encoding="utf-8")
    return "\n\n".join([
        "You are writing very short spoken-word radio \"shoutout\" lines for OmaRadio, to be read "
        "aloud by a text-to-speech voice in this DJ's own voice and broadcast as-is, right around a "
        "music transition. Each line either teases a track that's about to play (\"next\") or "
        "references the track(s) that just finished (\"recap\") -- always naming the artist and/or "
        "song title given for that slot. These are heard once, live, mixed right next to the actual "
        "song -- not read off a page, so they need to sound like a real DJ talking over a transition, "
        "not a metadata announcement.",
        "=== The Spirit of OmaRadio (platform-wide, applies to every segment) ===",
        spirit,
        f"=== DJ persona: {persona['name']} ({persona['slug']}) ===",
        persona_md or f"(No persona.md written yet for {persona['slug']} -- write in a neutral, on-brand OmaRadio voice.)",
    ])


def _format_tracks(tracks: list[dict]) -> str:
    return "; ".join(f'{t["artist"]} - "{t["title"]}"' for t in tracks)


def generate_shoutouts(system_prompt: str, slots: list[dict], model: str) -> tuple[list[dict], dict]:
    import anthropic
    from pydantic import BaseModel, field_validator

    requested_ids = {slot["slot_id"] for slot in slots}

    class ShoutoutOut(BaseModel):
        slot_id: str
        text: str

    class ShoutoutBatch(BaseModel):
        shoutouts: list[ShoutoutOut]

        @field_validator("shoutouts")
        @classmethod
        def check_shape(cls, v):
            ids = [s.slot_id for s in v]
            if len(ids) != len(set(ids)):
                raise ValueError(f"duplicate slot_id(s) in response: {ids}")
            got = set(ids)
            missing = requested_ids - got
            extra = got - requested_ids
            if missing or extra:
                raise ValueError(f"slot_id mismatch -- missing: {missing or None}, extra: {extra or None}")
            return v

    slot_lines = []
    for slot in slots:
        kind_desc = "tease the upcoming track" if slot["kind"] == "next" else "reference the track(s) just played"
        slot_lines.append(
            f'- slot_id: "{slot["slot_id"]}"\n'
            f'  kind: {slot["kind"]} ({kind_desc})\n'
            f'  track(s): {_format_tracks(slot["tracks"])}'
        )

    user_message = (
        f"Write exactly one shoutout line per slot below -- {len(slots)} slots, {len(slots)} lines total.\n\n"
        "Slots:\n" + "\n".join(slot_lines) + "\n\n"
        "Rules:\n"
        "- One shoutout per slot, matching that slot's slot_id exactly in your response.\n"
        "- Always name the artist and/or song title given for that slot -- these are the whole point "
        "of the clip, don't write a generic transition line that omits them.\n"
        "- For \"recap\" slots with two tracks, it's fine to only linger on one of them if that reads "
        "more natural, but at least name both at some point in the line.\n"
        "- Spoken-word radio copy, heard once over a live transition -- not read off a page.\n"
        "- Stay fully in this DJ's own persona/voice as described above.\n\n"
        "CRITICAL -- variation, not templating: a batch like this defaults to \"Next up, we've got "
        "{artist} with {title}\" repeated with names swapped. Don't do that. For each shoutout, use a "
        "DIFFERENT approach than the others in this batch:\n"
        "- Lead with the artist name\n"
        "- Lead with the song title or a phrase from it\n"
        "- Lead with a mood/vibe observation, name the track after\n"
        "- Tie it to something the DJ might have just been talking about, then pivot to the track\n"
        "- A quick, terse drop -- no lead-in at all\n"
        "- A rhetorical question or aside before naming the track\n\n"
        "Don't repeat the same sentence structure twice in a row. Vary length too -- some should be a "
        "single short clause, others a full sentence.\n\n"
        "No titles, no numbering, no stage directions -- just the shoutout text for each slot."
    )

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_format=ShoutoutBatch,
    )
    shoutouts = [{"slot_id": s.slot_id, "text": s.text} for s in response.parsed_output.shoutouts]
    usage = {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }
    return shoutouts, usage


def apply_pronunciation_fixes(text: str) -> str:
    """Identical to generate_segment.py's fix -- see that script's comment
    for why this exact respelling was chosen. Duplicated, not imported,
    per this pipeline's standalone-script convention."""
    def _respell(match: re.Match) -> str:
        word = match.group(0)
        if word.isupper():
            return "OH-MAH-CHEE"
        if word[0].isupper():
            return "Oh-mah-chee"
        return "oh-mah-chee"

    return re.sub(r"\bOmarchy\b", _respell, text, flags=re.IGNORECASE)


def _check_kokoro_files(model_path: Path, voices_path: Path) -> bool:
    missing = [p for p in (model_path, voices_path) if not p.exists()]
    if not missing:
        return True
    print(f"[!] Missing Kokoro model file(s): {', '.join(str(p) for p in missing)}", file=sys.stderr)
    print("    See generate_segment.py's docstring for the one-time download.", file=sys.stderr)
    return False


def render_audio(text: str, out_mp3: Path, voice: str, lang: str, speed: float,
                  model_path: Path, voices_path: Path) -> bool:
    """Identical rendering pipeline to generate_segment.py's render_audio()
    (kokoro-onnx synth + ffmpeg loudnorm to mp3) -- duplicated, not
    imported, per this pipeline's standalone-script convention."""
    import soundfile as sf
    from kokoro_onnx import Kokoro

    if not _check_kokoro_files(model_path, voices_path):
        return False
    if shutil.which("ffmpeg") is None:
        print("[!] ffmpeg not found on PATH -- can't create mp3.", file=sys.stderr)
        return False

    kokoro = Kokoro(str(model_path), str(voices_path))
    samples, sample_rate = kokoro.create(apply_pronunciation_fixes(text), voice=voice, speed=speed, lang=lang)

    tmp_wav = out_mp3.with_suffix(".tmp.wav")
    sf.write(tmp_wav, samples, sample_rate)

    cmd = ["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp_wav),
           "-af", f"loudnorm=I={LOUDNORM_TARGET_LUFS}:TP={LOUDNORM_TRUE_PEAK}:LRA={LOUDNORM_RANGE}",
           "-codec:a", "libmp3lame", "-b:a", "192k", str(out_mp3)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tmp_wav.unlink(missing_ok=True)
    if result.returncode != 0:
        print(f"[!] ffmpeg failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def sanitize_slot_id(slot_id: str, max_len: int = 100) -> str:
    """Filesystem-safe filename stem for a slot_id -- unlike slugify()
    elsewhere in this pipeline, this deliberately preserves case and most
    punctuation (slot ids are caller-defined identifiers, not prose titles
    that read better lowercased), only swapping out characters that are
    unsafe or awkward in a filename."""
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", slot_id).strip("-")
    return safe[:max_len].rstrip("-") or "slot"


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dj", required=True, help="Whose voice/persona this whole batch is in (one DJ per invocation)")
    parser.add_argument("--slots-file", type=Path, required=True, help="JSON file of slots to generate shoutouts for (see module docstring for shape)")
    parser.add_argument("--out-dir", type=Path, required=True, help="Directory to write <slot_id>.mp3 clips + manifest.json into")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL_SHOUTOUTS", "claude-haiku-4-5"),
                         help="Anthropic model id (default: claude-haiku-4-5 -- short/mechanical text, same "
                              "cheap-tier reasoning as generate_outros.py/generate_vera_handoffs.py)")
    parser.add_argument("--dry-run", action="store_true", help="Print the assembled prompt and exit -- no API call")
    parser.add_argument("--kokoro-model", type=Path,
                         default=Path(os.environ.get("KOKORO_MODEL_PATH", DEFAULT_KOKORO_DIR / "kokoro-v1.0.onnx")))
    parser.add_argument("--kokoro-voices", type=Path,
                         default=Path(os.environ.get("KOKORO_VOICES_PATH", DEFAULT_KOKORO_DIR / "voices-v1.0.bin")))
    args = parser.parse_args()

    slots = load_slots(args.slots_file)
    dj_dir = find_dj_dir(args.dj)
    persona, persona_md = load_persona(dj_dir)
    system_prompt = build_system_prompt(persona, persona_md)

    if args.dry_run:
        print("=== System prompt ===\n")
        print(system_prompt)
        print(f"\n=== Would request {len(slots)} shoutout(s) for {persona['name']} ({persona['slug']}) -- no API call (--dry-run) ===")
        for slot in slots:
            print(f"  - {slot['slot_id']} [{slot['kind']}]: {_format_tracks(slot['tracks'])}")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[!] ANTHROPIC_API_KEY not set. Copy pipeline/dj-segment/.env.example to .env and fill it in,", file=sys.stderr)
        print("    or export it in your shell.", file=sys.stderr)
        raise SystemExit(1)

    print(f"[*] Generating {len(slots)} shoutout(s) for {persona['name']} ({persona['slug']}) via {args.model}...")
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            shoutouts, usage = generate_shoutouts(system_prompt, slots, args.model)
            break
        except Exception as exc:
            last_exc = exc
            print(f"[!] Attempt {attempt + 1} failed validation ({exc}) -- retrying..." if attempt == 0
                  else f"[!] Attempt {attempt + 1} failed validation ({exc}).", file=sys.stderr)
    else:
        raise SystemExit(f"[!] Giving up after 2 attempts: {last_exc}")
    print(f"    tokens: {usage['input_tokens']} in / {usage['output_tokens']} out")

    text_by_slot_id = {s["slot_id"]: s["text"] for s in shoutouts}
    for slot in slots:
        print(f"    {slot['slot_id']} [{slot['kind']}]: {text_by_slot_id[slot['slot_id']]!r}")

    args.out_dir.mkdir(parents=True, exist_ok=True)

    manifest = []
    rendered = 0
    for slot in slots:
        slot_id = slot["slot_id"]
        text = text_by_slot_id[slot_id]
        stem = sanitize_slot_id(slot_id)
        mp3_path = args.out_dir / f"{stem}.mp3"
        ok = render_audio(
            text, mp3_path,
            voice=persona["voice"], lang=persona["lang"], speed=persona.get("speed", 1.0),
            model_path=args.kokoro_model, voices_path=args.kokoro_voices,
        )
        if not ok:
            print(f"[!] Audio render failed for slot '{slot_id}' -- skipping it: {text!r}", file=sys.stderr)
            continue
        manifest.append({
            "slot_id": slot_id,
            "text": text,
            "tracks": slot["tracks"],
            "kind": slot["kind"],
        })
        rendered += 1

    (args.out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"[+] Wrote {rendered}/{len(slots)} shoutout clips + manifest.json to {args.out_dir}")
    if rendered < len(slots):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
