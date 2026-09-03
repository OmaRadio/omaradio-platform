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
OmaRadio DJ Segment Generator (MVP)
====================================

Generates one AI-written, TTS-voiced DJ segment for review:

  1. Builds a prompt from the platform-wide tone rules
     (The-Spirit-of-OmaRadio.md) + the named DJ's persona, and calls the
     Anthropic API for a short spoken-word script.
  2. Renders that script to audio locally via kokoro-onnx (no TTS API
     cost), transcoded to mp3 via ffmpeg.
  3. Writes both into a per-attempt review folder -- nothing here ever
     touches the media library or goes on-air by itself. Use
     review_segment.py to list/listen/approve/reject what this produces.

Usage:
    uv run generate_segment.py --dj dj-mox --brief "Omarchy 1.4.0 released -- hit the highlights"
    uv run generate_segment.py --dj dj-mox --brief "..." --dry-run     # print the prompt, no API call
    uv run generate_segment.py --dj dj-mox --brief "..." --max-words 200
    uv run generate_segment.py --list-voices                          # show all kokoro-onnx voices

Setup:
    Copy pipeline/dj-segment/.env.example to .env (repo root) and set
    ANTHROPIC_API_KEY.

    Kokoro model weights (~350MB, not installed by uv/pip) -- one-time:
        wget -P ~/.cache/omaradio/kokoro https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
        wget -P ~/.cache/omaradio/kokoro https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
    Or point --kokoro-model/--kokoro-voices at an existing copy (e.g. a
    sibling omaradio-numbers-station checkout) to avoid downloading twice.

    ffmpeg on PATH (mp3 transcode):
        Ubuntu / the droplet:  sudo apt install ffmpeg
        Omarchy / Arch:        sudo pacman -S ffmpeg

See README.md in this directory for the full walkthrough, including how an
approved segment gets synced to the vault and placed on-air.
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
DJS_GLOB = "staff/stations/*/djs/{slug}"
# Roles other than DJ are singular per station (one Station Manager, one
# Intern) so their directory isn't slug-named the way djs/<slug>/ is --
# find_dj_dir() falls back to matching these by the `slug` field inside
# persona.toml instead, for personas who occasionally do on-air bits too
# (e.g. a Station Manager sign-on) without forcing an unnecessary subfolder.
SINGULAR_ROLE_GLOB = "staff/stations/*/{role}"
SINGULAR_ROLES = ("station-manager", "intern")
SPIRIT_DOC = REPO_ROOT / "The-Spirit-of-OmaRadio.md"
PROMPT_PREAMBLE = Path(__file__).resolve().parent / "prompts" / "system_prompt_template.md"

DEFAULT_KOKORO_DIR = Path.home() / ".cache" / "omaradio" / "kokoro"
DEFAULT_LOCAL_LIBRARY = Path.home() / "Work" / "OmaRadio" / "media_library" / "library"

# Same voice table as the sibling omaradio-numbers-station script, kept in
# sync manually -- see persona.toml's `voice` field comment.
VOICES_BY_LANG = {
    "American English": ("a", [
        "af_alloy", "af_aoede", "af_bella", "af_heart", "af_jessica", "af_kore",
        "af_nicole", "af_nova", "af_river", "af_sarah", "af_sky",
        "am_adam", "am_echo", "am_eric", "am_fenrir", "am_liam", "am_michael",
        "am_onyx", "am_puck", "am_santa",
    ]),
    "British English": ("b", [
        "bf_alice", "bf_emma", "bf_isabella", "bf_lily",
        "bm_daniel", "bm_fable", "bm_george", "bm_lewis",
    ]),
    "European Spanish": ("e", ["ef_dora", "em_alex", "em_santa"]),
    "French": ("f", ["ff_siwis"]),
    "Hindi": ("h", ["hf_alpha", "hf_beta", "hm_omega", "hm_psi"]),
    "Italian": ("i", ["if_sara", "im_nicola"]),
    "Japanese": ("j", ["jf_alpha", "jf_gongitsune", "jf_nezumi", "jf_tebukuro", "jm_kumo"]),
    "Brazilian Portuguese": ("p", ["pf_dora", "pm_alex", "pm_santa"]),
    "Mandarin Chinese": ("z", [
        "zf_xiaobei", "zf_xiaoni", "zf_xiaoxiao", "zf_xiaoyi",
        "zm_yunjian", "zm_yunxi", "zm_yunxia", "zm_yunyang",
    ]),
}


def print_voice_list():
    print("Kokoro v1.0 voices -- 54 total across 9 languages")
    print("Prefix legend: 1st letter = language, 2nd = gender (f/m)\n")
    for lang_name, (prefix, voices) in VOICES_BY_LANG.items():
        print(f"{lang_name}  (prefix '{prefix}')")
        print("  " + ", ".join(voices))
        print()
    print("OmaRadio's copy is English -- stick to af_*/am_*/bf_*/bm_* unless")
    print("a DJ is deliberately doing a multilingual bit.")


def find_dj_dir(slug: str) -> Path:
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


def build_system_prompt(persona: dict, persona_md: str) -> str:
    preamble = PROMPT_PREAMBLE.read_text(encoding="utf-8")
    spirit = SPIRIT_DOC.read_text(encoding="utf-8")
    parts = [
        preamble,
        "=== The Spirit of OmaRadio (platform-wide, applies to every segment) ===",
        spirit,
        f"=== DJ persona: {persona['name']} ({persona['slug']}) ===",
        persona_md or f"(No persona.md written yet for {persona['slug']} -- write in a neutral, on-brand OmaRadio voice.)",
    ]
    return "\n\n".join(parts)


def slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def generate_script(system_prompt: str, brief: str, max_words: int, model: str) -> dict:
    import anthropic
    from pydantic import BaseModel

    class SegmentScript(BaseModel):
        title: str
        est_seconds: int
        script: str

    client = anthropic.Anthropic()
    user_message = (
        f"Brief: {brief}\n\n"
        f"Target length: approximately {max_words} words "
        f"(~{round(max_words / 2.3)} seconds spoken at talk-radio pace)."
    )

    response = client.messages.parse(
        model=model,
        max_tokens=4096,
        system=system_prompt,
        messages=[{"role": "user", "content": user_message}],
        output_format=SegmentScript,
    )
    result = response.parsed_output
    return {
        "title": result.title,
        "est_seconds": result.est_seconds,
        "script": result.script,
        "usage": {
            "input_tokens": response.usage.input_tokens,
            "output_tokens": response.usage.output_tokens,
        },
    }


def _check_kokoro_files(model_path: Path, voices_path: Path) -> bool:
    missing = [p for p in (model_path, voices_path) if not p.exists()]
    if not missing:
        return True
    print(f"[!] Missing Kokoro model file(s): {', '.join(str(p) for p in missing)}", file=sys.stderr)
    print("    One-time ~350MB download:", file=sys.stderr)
    print(f"      wget -P {model_path.parent} https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx", file=sys.stderr)
    print(f"      wget -P {voices_path.parent} https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin", file=sys.stderr)
    print("    Or point --kokoro-model/--kokoro-voices at an existing copy.", file=sys.stderr)
    return False


# TTS engines sound out words using their own default pronunciation rules --
# they don't read The-Spirit-of-OmaRadio.md's pronunciation note, so that
# rule alone doesn't affect the rendered audio. Respelling phonetically
# before synthesis is the standard fix. Hyphenated "Oh-mah-chee" is the
# spelling confirmed (2026-09-03, by ear) to render correctly across voices
# -- the closed-up "Omaachee" spelling (matching the sibling
# omaradio-numbers-station repo's TTS scripts) collapsed to sounding like
# "Ahmahchee" on the American kokoro voices (am_onyx, af_nova), even though
# it was fine on the British bm_george. Applied only to the audio-rendering
# pass; script.json keeps the naturally-spelled text.
def apply_pronunciation_fixes(text: str) -> str:
    def _respell(match: re.Match) -> str:
        word = match.group(0)
        if word.isupper():
            return "OH-MAH-CHEE"
        if word[0].isupper():
            return "Oh-mah-chee"
        return "oh-mah-chee"

    return re.sub(r"\bOmarchy\b", _respell, text, flags=re.IGNORECASE)


def render_audio(text: str, out_mp3: Path, voice: str, lang: str, speed: float,
                  model_path: Path, voices_path: Path) -> bool:
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
           "-codec:a", "libmp3lame", "-b:a", "192k", str(out_mp3)]
    result = subprocess.run(cmd, capture_output=True, text=True)
    tmp_wav.unlink(missing_ok=True)
    if result.returncode != 0:
        print(f"[!] ffmpeg failed: {result.stderr.strip()}", file=sys.stderr)
        return False
    return True


def main():
    try:
        from dotenv import load_dotenv
        # Repo root is the documented location; also check next to this
        # script, since .env.example lives here too and it's an easy place
        # to accidentally `cp` a real .env into instead. Must run BEFORE
        # the argparse defaults below are built -- they read os.environ.
        load_dotenv(REPO_ROOT / ".env")
        load_dotenv(Path(__file__).resolve().parent / ".env")
    except ImportError:
        pass

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dj", help="On-air persona slug -- a DJ (dj-mox) or another staff persona doing an "
                                      "on-air bit (e.g. alan, the Station Manager)")
    parser.add_argument("--brief", help="Topic/angle for this segment (stands in for a future Station Manager's direction)")
    parser.add_argument("--max-words", type=int, default=300, help="Target script length in words (default: 300)")
    parser.add_argument("--model", default=os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5"),
                         help="Anthropic model id (default: claude-sonnet-5, or $ANTHROPIC_MODEL)")
    parser.add_argument("--dry-run", action="store_true", help="Print the assembled prompt and exit -- no API call")
    parser.add_argument("--kokoro-model", type=Path,
                         default=Path(os.environ.get("KOKORO_MODEL_PATH", DEFAULT_KOKORO_DIR / "kokoro-v1.0.onnx")),
                         help="Path to Kokoro's .onnx model file")
    parser.add_argument("--kokoro-voices", type=Path,
                         default=Path(os.environ.get("KOKORO_VOICES_PATH", DEFAULT_KOKORO_DIR / "voices-v1.0.bin")),
                         help="Path to Kokoro's voices .bin file")
    parser.add_argument("--review-dir", type=Path, default=None,
                         help="Override the review output root (default: sibling of $LOCAL_LIBRARY, "
                              f"i.e. {DEFAULT_LOCAL_LIBRARY.parent / 'review'})")
    parser.add_argument("--list-voices", action="store_true", help="Print all kokoro-onnx voices and exit")
    args = parser.parse_args()

    if args.list_voices:
        print_voice_list()
        return

    if not args.dj or not args.brief:
        parser.error("--dj and --brief are required (unless --list-voices)")

    dj_dir = find_dj_dir(args.dj)
    persona, persona_md = load_persona(dj_dir)
    system_prompt = build_system_prompt(persona, persona_md)

    if args.dry_run:
        print("=== System prompt ===\n")
        print(system_prompt)
        print(f"\n=== User message ===\n\nBrief: {args.brief}\nTarget length: ~{args.max_words} words")
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("[!] ANTHROPIC_API_KEY not set. Copy pipeline/dj-segment/.env.example to .env and fill it in,", file=sys.stderr)
        print("    or export it in your shell.", file=sys.stderr)
        raise SystemExit(1)

    print(f"[*] Generating script for {persona['name']} ({persona['slug']}) via {args.model}...")
    result = generate_script(system_prompt, args.brief, args.max_words, args.model)
    usage = result.pop("usage")
    print(f"[+] Got script: \"{result['title']}\" (~{result['est_seconds']}s)")
    print(f"    tokens: {usage['input_tokens']} in / {usage['output_tokens']} out")

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    segment_id = f"{timestamp}-{persona['slug']}-{slugify(result['title'])}"

    local_library = Path(os.environ.get("LOCAL_LIBRARY", DEFAULT_LOCAL_LIBRARY))
    review_root = args.review_dir or (local_library.parent / "review")
    out_dir = review_root / "dj-segments" / persona["slug"] / segment_id
    out_dir.mkdir(parents=True, exist_ok=True)

    script_json = {
        **result,
        "meta": {
            "dj": persona["slug"],
            "station": persona["station"],
            "brief": args.brief,
            "model": args.model,
            "created_at": timestamp,
            "segment_id": segment_id,
            "usage": usage,
        },
    }
    (out_dir / "script.json").write_text(json.dumps(script_json, indent=2), encoding="utf-8")
    print(f"[+] Wrote {out_dir / 'script.json'}")

    print("[*] Rendering audio via kokoro-onnx...")
    mp3_path = out_dir / "segment.mp3"
    ok = render_audio(
        result["script"], mp3_path,
        voice=persona["voice"], lang=persona["lang"], speed=persona.get("speed", 1.0),
        model_path=args.kokoro_model, voices_path=args.kokoro_voices,
    )
    if ok:
        print(f"[+] Wrote {mp3_path}")
        print(f"\nReview with: uv run review_segment.py show {segment_id}")
    else:
        print(f"[!] Audio render failed -- script.json is saved at {out_dir}, retry audio manually once fixed.", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
