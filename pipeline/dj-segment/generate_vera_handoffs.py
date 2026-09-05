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
DJ Vera Hand-off Tag Generator
=================================

Generates small pools of short hand-off tags, spoken in Vera's own voice,
naming who (or what) actually comes on after her news rundown -- e.g.
"That's the rundown -- Mox, it's yours" or "That covers it, back to the
music." A pre-rendered mp3 can't dynamically speak a name, so instead of
one variable line, this produces a handful of pre-written variants PER
POSSIBLE TARGET, and build_playlist.py's plan_block() deterministically
picks the right one at schedule time -- it already knows, at the moment
it splices Vera's periodic segment in, which DJ (if any) owns the block
she's landing in (see owning_dj() / STATION_SHIFTS).

Targets are hardcoded to what's actually reachable given today's
schedule, not every DJ that exists: STATION_PERIODIC_SEGMENTS puts Vera
at hours 0/8/16/18 UTC, which land in the 00:00 (dj-mox), 06:00
(open/music), 12:00 (dj-nova), and 18:00 (dj-nikon) blocks respectively --
every block now gets exactly one occurrence (hour 18 was added
2026-09-05 specifically to close what had been a gap; see
build_playlist.py's STATION_PERIODIC_SEGMENTS comment for the full
history). If STATION_PERIODIC_SEGMENTS/STATION_SHIFTS ever change again,
update HANDOFF_TARGETS here to match.

Vera's own rundown script stays generic/non-naming (see her persona.md)
precisely because these tags exist -- the dynamic part happens here, not
in her main segment generation.

Storage: $LOCAL_LIBRARY/dj-segments/dj-vera/handoff/<target>/*.mp3
(paired with .script.json, mirroring the rest of this pipeline). Not
gated behind review_segment.py, same reasoning as generate_outros.py --
generic, reusable, brief-independent.

Usage:
    uv run generate_vera_handoffs.py                    # all 4 targets, 5 lines each
    uv run generate_vera_handoffs.py --target dj-mox     # just one target
    uv run generate_vera_handoffs.py --count 6
    uv run generate_vera_handoffs.py --dry-run           # print prompts, no API call
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SPIRIT_DOC = REPO_ROOT / "The-Spirit-of-OmaRadio.md"
VERA_DIR = REPO_ROOT / "staff" / "stations" / "one" / "djs" / "dj-vera"
STATION_DJS_DIR = REPO_ROOT / "staff" / "stations" / "one" / "djs"

# See module docstring for why exactly these four and not more.
HANDOFF_TARGETS = ["dj-mox", "dj-nova", "dj-nikon", "music"]

DEFAULT_COUNT = 5  # smaller than generate_outros.py's pool -- each target is used far less often

LOUDNORM_TARGET_LUFS = -12
LOUDNORM_TRUE_PEAK = -1.0
LOUDNORM_RANGE = 11

DEFAULT_KOKORO_DIR = Path.home() / ".cache" / "omaradio" / "kokoro"
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"


def local_library() -> Path:
    return Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))


def load_vera_persona() -> tuple[dict, str]:
    with (VERA_DIR / "persona.toml").open("rb") as f:
        persona = tomllib.load(f)
    persona_md = (VERA_DIR / "persona.md").read_text(encoding="utf-8")
    return persona, persona_md


def target_display_name(target: str) -> str:
    if target == "music":
        return "music"
    with (STATION_DJS_DIR / target / "persona.toml").open("rb") as f:
        return tomllib.load(f)["name"]


def build_system_prompt(persona: dict, persona_md: str) -> str:
    spirit = SPIRIT_DOC.read_text(encoding="utf-8")
    return "\n\n".join([
        "You are writing very short spoken-word radio hand-off lines for OmaRadio, "
        "to be read aloud by a text-to-speech voice and broadcast as-is.",
        "=== The Spirit of OmaRadio (platform-wide) ===",
        spirit,
        f"=== DJ persona: {persona['name']} ({persona['slug']}) ===",
        persona_md,
    ])


def generate_handoff_lines(system_prompt: str, target: str, count: int, model: str) -> tuple[list[str], dict]:
    import anthropic
    from pydantic import BaseModel, field_validator

    class HandoffBatch(BaseModel):
        lines: list[str]

        @field_validator("lines")
        @classmethod
        def check_shape(cls, v):
            if len(v) != count:
                raise ValueError(f"expected exactly {count} lines, got {len(v)}")
            seen = set()
            for line in v:
                key = line.strip().lower()
                if key in seen:
                    raise ValueError(f"duplicate line: {line!r}")
                seen.add(key)
            return v

    if target == "music":
        target_instruction = (
            "The rundown is being followed by plain music, no DJ coming on -- hand off to \"the music\" "
            "generically, without naming anyone."
        )
    else:
        name = target_display_name(target)
        target_instruction = (
            f"The rundown is immediately followed by {name} coming on air -- name {name} directly in the "
            f"hand-off (e.g. \"back to {name}\", \"{name}, it's yours\"), naturally, in Vera's own voice."
        )

    user_message = (
        f"Write exactly {count} short hand-off lines for Vera to close her news rundown with.\n\n"
        f"{target_instruction}\n\n"
        "Rules:\n"
        "- Exactly ONE sentence each, spoken-word radio copy (heard once, not read).\n"
        "- These replace Vera's usual generic sign-off for this specific case -- they should read as a "
        "complete, natural close to a rundown, not a fragment tacked onto one.\n"
        f"- All {count} must be genuinely distinct from each other in wording and structure -- not the "
        "same sentence with one word swapped.\n"
        "- Match Vera's established voice and delivery exactly as described in her persona above -- clear, "
        "measured, no music-DJ bits, professional but still warm.\n"
        f"- No titles, no numbering, no stage directions -- just the {count} spoken lines."
    )

    client = anthropic.Anthropic()
    response = client.messages.parse(
        model=model,
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_format=HandoffBatch,
    )
    return response.parsed_output.lines, {
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


def apply_pronunciation_fixes(text: str) -> str:
    """Identical to generate_segment.py's fix -- duplicated, not imported,
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
    -- duplicated, not imported, per this pipeline's standalone-script
    convention."""
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


def generate_for_target(target: str, persona: dict, system_prompt: str, count: int, model: str,
                         dry_run: bool, kokoro_model: Path, kokoro_voices: Path) -> None:
    if dry_run:
        print(f"=== Target: {target} ===")
        print(f"(Would request {count} lines -- no API call, --dry-run)\n")
        return

    print(f"[*] Generating {count} hand-off lines for target '{target}' via {model}...")
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            lines, usage = generate_handoff_lines(system_prompt, target, count, model)
            break
        except Exception as exc:
            last_exc = exc
            print(f"[!] Attempt {attempt + 1} failed validation ({exc}) -- retrying..." if attempt == 0
                  else f"[!] Attempt {attempt + 1} failed validation ({exc}).", file=sys.stderr)
    else:
        raise SystemExit(f"[!] Giving up on target '{target}' after 2 attempts: {last_exc}")
    print(f"    tokens: {usage['input_tokens']} in / {usage['output_tokens']} out")
    for i, line in enumerate(lines, start=1):
        print(f"    {i}. {line}")

    out_dir = local_library() / "dj-segments" / "dj-vera" / "handoff" / target
    if out_dir.exists():
        removed = list(out_dir.glob("*"))
        for p in removed:
            p.unlink()
        if removed:
            print(f"[*] Cleared {len(removed)} existing file(s) from {out_dir} before regenerating.")
    out_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    rendered = 0
    for i, line in enumerate(lines, start=1):
        stem = f"handoff-{i:02d}-{slugify(line)}"
        mp3_path = out_dir / f"{stem}.mp3"
        ok = render_audio(
            line, mp3_path,
            voice=persona["voice"], lang=persona["lang"], speed=persona.get("speed", 1.0),
            model_path=kokoro_model, voices_path=kokoro_voices,
        )
        if not ok:
            print(f"[!] Audio render failed for line {i} -- skipping it: {line!r}", file=sys.stderr)
            continue
        (out_dir / f"{stem}.script.json").write_text(json.dumps({
            "title": None,
            "script": line,
            "meta": {
                "dj": "dj-vera",
                "kind": "handoff",
                "target": target,
                "model": model,
                "created_at": timestamp,
                "batch_size": count,
                "usage": usage,
            },
        }, indent=2), encoding="utf-8")
        rendered += 1

    print(f"[+] Wrote {rendered}/{count} hand-off clips to {out_dir}")


def main():
    try:
        from dotenv import load_dotenv
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--target", choices=HANDOFF_TARGETS, default=None,
                         help="Generate only this target's pool (default: all of them)")
    parser.add_argument("--count", type=int, default=DEFAULT_COUNT,
                         help=f"How many unique lines per target (default: {DEFAULT_COUNT})")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL_OUTROS", "claude-haiku-4-5"),
                         help="Anthropic model id (default: claude-haiku-4-5 -- short/mechanical text)")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be requested, no API call")
    parser.add_argument("--kokoro-model", type=Path,
                         default=Path(os.environ.get("KOKORO_MODEL_PATH", DEFAULT_KOKORO_DIR / "kokoro-v1.0.onnx")))
    parser.add_argument("--kokoro-voices", type=Path,
                         default=Path(os.environ.get("KOKORO_VOICES_PATH", DEFAULT_KOKORO_DIR / "voices-v1.0.bin")))
    args = parser.parse_args()

    persona, persona_md = load_vera_persona()
    system_prompt = build_system_prompt(persona, persona_md)

    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        print("[!] ANTHROPIC_API_KEY not set. Copy pipeline/dj-segment/.env.example to .env and fill it in,", file=sys.stderr)
        print("    or export it in your shell.", file=sys.stderr)
        raise SystemExit(1)

    targets = [args.target] if args.target else HANDOFF_TARGETS
    for target in targets:
        generate_for_target(target, persona, system_prompt, args.count, args.model,
                             args.dry_run, args.kokoro_model, args.kokoro_voices)


if __name__ == "__main__":
    main()
