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
OmaRadio Station Outro Generator
====================================

Generates a small reusable pool of short, generic station-identification
outro lines (e.g. "You're listening to OmaRadio") -- station branding, NOT
tied to any specific DJ's voice or persona, since these need to make sense
regardless of which DJ (if any) owns the current block. Rendered in a
single dedicated "station voice" (see STATION_VOICE below, distinct from
every staff persona's own voice) and stored in the vault's existing
`jingles` media category (jingles/station-outro/), not under
dj-segments/<dj>/ -- this is station identity, not a DJ's own content.

Deliberately NOT gated behind review_segment.py's approval step: generic,
brief-independent filler, not per-topic content. Still printed to stdout
in full at generation time for a human to eyeball.

build_playlist.py's plan_block() decides WHEN (and how often) to splice
one in after a DJ segment -- weighted, configurable, not guaranteed every
time (see STATION_OUTRO_CHANCE in that script). This script only produces
the pool it picks from.

Regenerating REPLACES the whole pool from scratch (old files removed
first) rather than appending -- a periodically-refreshed fixed-size set,
not an ever-growing archive.

Usage:
    uv run generate_outros.py                  # 13 outros (default)
    uv run generate_outros.py --count 12
    uv run generate_outros.py --dry-run          # print the assembled prompt, no API call

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
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIRIT_DOC = REPO_ROOT / "The-Spirit-of-OmaRadio.md"

STATION = "one"  # only station this repo supports today, same assumption build_playlist.py's tables make
STATION_NAME = "OmaRadio One"

# Not a staff persona (no personality, no persona.md) -- just the voice
# used for generic network/station-ID drops, so it's a plain constant
# here rather than a staff/ directory. bf_emma is unclaimed by any
# existing staff persona (Mox=bm_george, Nova=af_nova, Vera=af_sarah,
# Nikon=am_michael, Alan=am_onyx, Relay=af_river) -- picked for a neutral,
# slightly formal "network ID" read, distinct from any DJ's own voice.
# Starting guess, not locked in; audition and swap freely.
STATION_VOICE = {"voice": "bf_emma", "lang": "en-gb", "speed": 1.0}

DEFAULT_COUNT = 13  # middle of the requested 12-14 range

# Same rationale/values as generate_segment.py's identical constants --
# outros sit in the same on-air rotation as full segments and need to
# match their loudness, not a generic external convention. Keep these two
# copies in sync if the library's mastering is ever re-measured.
LOUDNORM_TARGET_LUFS = -12
LOUDNORM_TRUE_PEAK = -1.0
LOUDNORM_RANGE = 11

DEFAULT_KOKORO_DIR = Path.home() / ".cache" / "omaradio" / "kokoro"
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def build_system_prompt() -> str:
    spirit = SPIRIT_DOC.read_text(encoding="utf-8")
    return "\n\n".join([
        "You are writing very short spoken-word station-identification lines for OmaRadio, "
        "to be read aloud by a text-to-speech voice and broadcast as-is. These are NOT spoken "
        "by any specific DJ -- they are generic station-ID drops that can air regardless of "
        "which DJ (if any) is on at the time, so they must never reference a DJ by name or "
        "personality, and must never sound like they're mid-conversation with a specific host.",
        "=== The Spirit of OmaRadio (platform-wide) ===",
        spirit,
    ])


def generate_outro_lines(system_prompt: str, count: int, model: str) -> tuple[list[str], dict]:
    import anthropic
    from pydantic import BaseModel, field_validator

    class OutroBatch(BaseModel):
        outros: list[str]

        @field_validator("outros")
        @classmethod
        def check_shape(cls, v):
            if len(v) != count:
                raise ValueError(f"expected exactly {count} outros, got {len(v)}")
            seen = set()
            for line in v:
                key = line.strip().lower()
                if key in seen:
                    raise ValueError(f"duplicate outro line: {line!r}")
                seen.add(key)
            return v

    user_message = (
        f"Write exactly {count} short station-identification lines for {STATION_NAME}, the kind of "
        "generic on-air ID/branding drop that reminds listeners what station they're on.\n\n"
        "Rules:\n"
        "- Exactly ONE sentence each, spoken-word radio copy (heard once, not read).\n"
        f"- Generic and station-branded, not DJ-branded -- reference the station ({STATION_NAME} / "
        "Transmitter-One) rather than any specific DJ, segment, song, or topic.\n"
        f"- All {count} must be genuinely distinct from each other in wording and structure -- not the "
        "same sentence with one word swapped.\n"
        "- Stay inside the platform's established pirate-radio-from-a-ghost-ship frame, but keep these "
        "neutral and network-voiced rather than any one DJ's personality or catchphrases.\n"
        f"- No titles, no numbering, no stage directions -- just the {count} spoken lines."
    )

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_format=OutroBatch,
    )
    return response.parsed_output.outros, {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


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


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT,
                         help=f"How many unique outro lines to generate (default: {DEFAULT_COUNT})")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL_OUTROS", "claude-haiku-4-5"),
                         help="Anthropic model id (default: claude-haiku-4-5 -- generic/mechanical text, "
                              "same cheap-tier reasoning as Alan's topic-brief calls in auto_dj.py)")
    parser.add_argument("--dry-run", action="store_true", help="Print the assembled prompt and exit -- no API call")
    parser.add_argument("--kokoro-model", type=Path,
                         default=Path(os.environ.get("KOKORO_MODEL_PATH", DEFAULT_KOKORO_DIR / "kokoro-v1.0.onnx")))
    parser.add_argument("--kokoro-voices", type=Path,
                         default=Path(os.environ.get("KOKORO_VOICES_PATH", DEFAULT_KOKORO_DIR / "voices-v1.0.bin")))
    args = parser.parse_args()

    system_prompt = build_system_prompt()

    if args.dry_run:
        print("=== System prompt ===\n")
        print(system_prompt)
        print(f"\n=== Would request {args.count} outro lines -- no API call (--dry-run) ===")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[!] ANTHROPIC_API_KEY not set. Copy pipeline/dj-segment/.env.example to .env and fill it in,", file=sys.stderr)
        print("    or export it in your shell.", file=sys.stderr)
        raise SystemExit(1)

    print(f"[*] Generating {args.count} station outro lines for {STATION_NAME} via {args.model}...")
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            outros, usage = generate_outro_lines(system_prompt, args.count, args.model)
            break
        except Exception as exc:
            last_exc = exc
            print(f"[!] Attempt {attempt + 1} failed validation ({exc}) -- retrying..." if attempt == 0
                  else f"[!] Attempt {attempt + 1} failed validation ({exc}).", file=sys.stderr)
    else:
        raise SystemExit(f"[!] Giving up after 2 attempts: {last_exc}")
    print(f"    tokens: {usage['input_tokens']} in / {usage['output_tokens']} out")
    for i, line in enumerate(outros, start=1):
        print(f"    {i:2d}. {line}")

    out_dir = local_library() / "jingles" / "station-outro"
    if out_dir.exists():
        removed = list(out_dir.glob("*"))
        for p in removed:
            p.unlink()
        if removed:
            print(f"[*] Cleared {len(removed)} existing file(s) from {out_dir} before regenerating.")
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rendered = 0
    for i, line in enumerate(outros, start=1):
        stem = f"outro-{i:02d}-{slugify(line)}"
        mp3_path = out_dir / f"{stem}.mp3"
        ok = render_audio(
            line, mp3_path,
            voice=STATION_VOICE["voice"], lang=STATION_VOICE["lang"], speed=STATION_VOICE["speed"],
            model_path=args.kokoro_model, voices_path=args.kokoro_voices,
        )
        if not ok:
            print(f"[!] Audio render failed for outro {i} -- skipping it: {line!r}", file=sys.stderr)
            continue
        (out_dir / f"{stem}.script.json").write_text(json.dumps({
            "title": None,
            "script": line,
            "meta": {
                "station": STATION,
                "kind": "station-outro",
                "voice": STATION_VOICE,
                "model": args.model,
                "created_at": timestamp,
                "batch_size": args.count,
                "usage": usage,
            },
        }, indent=2), encoding="utf-8")
        rendered += 1

    print(f"[+] Wrote {rendered}/{args.count} station outro clips to {out_dir}")


if __name__ == "__main__":
    main()
