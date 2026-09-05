# OmaRadio DJ Segment Pipeline (MVP)

Generates one AI-written, locally-TTS'd DJ segment at a time, gated behind
Orchestrator approval, and gets it onto the live stream. This is the first,
smallest slice of OmaRadio's automation pipeline -- see
[`the plan`](../../CLAUDE.md) for what's deliberately *not* built yet (news
connectors, the Intern role).

Getting an approved segment into rotation is now automated:
`infra/transmitter/playlist-builder/build_playlist.py` runs unattended on
transmitter-one every 6 hours, picking from whatever's been approved+synced
for the DJ owning that time block. `on-air-place.sh` (below) still exists
for a deliberate one-off placement (e.g. an ad hoc Alan/Relay appearance),
but note that the playlist builder does a full `on-air/` replace every 6h
with no knowledge of manual placements -- they're temporary once its timer
is running.

## How it fits together

```
generate_segment.py --dj dj-mox --brief "..."
    -> Claude API writes the script (persona + The-Spirit-of-OmaRadio.md tone rules)
    -> kokoro-onnx renders it to audio, locally, no API cost
    -> both land in review/dj-segments/<dj>/<segment-id>/   (nothing on-air yet)

review_segment.py list / show / approve / reject
    -> Orchestrator listens + approves
    -> approved segment.mp3 is copied into
       $LOCAL_LIBRARY/dj-segments/<dj>/generated/<segment-id>.mp3

infra/transmitter/vault/sync-library.sh dj-segments   (existing script, unmodified)
    -> rsyncs it up to the vault on transmitter-one

infra/transmitter/playlist-builder/build_playlist.py's systemd timer (every 6h, UTC-anchored)
    -> automatically picks approved segments + shuffled music into the station's on-air/ farm
    -> restarts cliamp-server

(or a manual one-off placement via on-air-place.sh -- see below --
 which survives only until the next automated 6h rebuild)
```

## Setup

1. **Anthropic API key**: copy `.env.example` to `.env` at the repo root and
   set `ANTHROPIC_API_KEY`. `.env` is gitignored.

2. **Kokoro model weights** (~350MB, a one-time download -- `uv`/`pip` only
   install the `kokoro-onnx` *package*, not these binaries):

   ```bash
   wget -P ~/.cache/omaradio/kokoro https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/kokoro-v1.0.onnx
   wget -P ~/.cache/omaradio/kokoro https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files-v1.0/voices-v1.0.bin
   ```

   If you already have a copy (e.g. from the sibling `omaradio-numbers-station`
   checkout), skip the download and point at it instead:
   `--kokoro-model ~/Work/OmaRadio/omaradio-numbers-station/kokoro-v1.0.onnx --kokoro-voices ~/Work/OmaRadio/omaradio-numbers-station/voices-v1.0.bin`
   (or `KOKORO_MODEL_PATH`/`KOKORO_VOICES_PATH` env vars).

3. **ffmpeg** on PATH (mp3 transcode):
   `sudo apt install ffmpeg` (droplet/Ubuntu) or `sudo pacman -S ffmpeg` (Omarchy/Arch).

Both scripts are run via `uv run` -- no manual `pip install` needed, the
PEP 723 header at the top of each file handles dependencies.

## Usage

```bash
# Generate
uv run pipeline/dj-segment/generate_segment.py --dj dj-mox --brief "Omarchy 1.4.0 released -- hit the highlights"
uv run pipeline/dj-segment/generate_segment.py --dj dj-mox --brief "..." --dry-run   # prompt only, no API call
uv run pipeline/dj-segment/generate_segment.py --list-voices                        # all kokoro-onnx voices

# Review
uv run pipeline/dj-segment/review_segment.py list
uv run pipeline/dj-segment/review_segment.py show <segment-id>
uv run pipeline/dj-segment/review_segment.py approve <segment-id> --note "sounds good"
uv run pipeline/dj-segment/review_segment.py reject <segment-id> --note "off-brief"
```

Both scripts respect `LOCAL_LIBRARY` (same env var `sync-library.sh` already
uses; default `~/Work/OmaRadio/media_library/library`) for where the local
vault mirror lives, and `ANTHROPIC_MODEL` to override the default model
(`claude-sonnet-5`).

**Pronunciation:** `The-Spirit-of-OmaRadio.md` says "Omarchy" is pronounced
"OH-MAHH-CHEE" -- that rule reaches the script-writing prompt automatically
(the file is read live), but a TTS engine doesn't read prose pronunciation
notes, so it doesn't affect the rendered audio on its own. `generate_segment.py`
respells "Omarchy" -> "Omaachee" (case-preserving) right before the
kokoro-onnx call -- `script.json` keeps the normal spelling; only the audio
pass sees the respelling. "Omaachee" matches the spelling already
established in the sibling `omaradio-numbers-station` repo's TTS scripts.
Add more of these to `apply_pronunciation_fixes()` in `generate_segment.py`
if other words come up that kokoro mispronounces.

## Adding a DJ

Create `staff/stations/<station>/djs/<slug>/persona.toml` and `persona.md`
following `staff/stations/one/djs/dj-mox/` as a template. `--dj <slug>`
finds it automatically by globbing `staff/stations/*/djs/<slug>/`.

## Other staff going on-air (e.g. a Station Manager sign-on)

`--dj` isn't strictly DJ-only -- it also matches other staff personas by the
`slug` field inside their `persona.toml`, for roles that are singular per
station (`station-manager/`, `intern/`) and so don't have a `djs/`-style
slug subfolder of their own. `staff/stations/one/station-manager/persona.toml`
(Alan) and `staff/stations/one/intern/persona.toml` (Relay) are the working
examples: add `voice`/`lang`/`speed` fields the same as a DJ persona, and
`--dj alan` / `--dj relay` finds them via their `slug`, not their directory
name. Use this sparingly per that persona's own boundaries (Alan's
persona.md treats on-air appearances as the exception, not the norm) --
this is about making the occasional bit possible, not turning every staff
role into a regular DJ.

## Manual on-air runbook (one-off placements only)

For the automated path, see `infra/transmitter/playlist-builder/`'s own
docstring -- it handles regular scheduling on its own 6-hourly timer and
needs no manual steps once its systemd timer is enabled/started. The
runbook below is for a deliberate one-off placement outside that schedule
(e.g. an ad hoc Alan/Relay appearance) -- remember it only survives until
the next automated 6h rebuild.

**Confirmed (2026-09-02): cliamp-server does NOT pick up `on-air/` symlink
changes live -- it needs a restart.** `infra/transmitter/vault/on-air-place.sh`
handles both steps (symlink + restart) in one command:

```bash
# One-time setup (interactive, needs your sudo password once) -- grants the
# omaradio user passwordless sudo for exactly `systemctl restart
# cliamp-server`, so the script can restart it over a non-interactive SSH
# call. See the script's header comment for the exact commands.
ssh omaradio@transmitter-one.omaradio.stream
echo 'omaradio ALL=(root) NOPASSWD: /usr/bin/systemctl restart cliamp-server' \
  | sudo tee /etc/sudoers.d/omaradio-cliamp-restart
sudo chmod 440 /etc/sudoers.d/omaradio-cliamp-restart
sudo visudo -c

# Then, after review_segment.py approve + sync-library.sh dj-segments:
infra/transmitter/vault/on-air-place.sh one \
  /mnt/media_library/library/dj-segments/<dj>/generated/<segment-id>.mp3 \
  <next-number>-<dj>.mp3
```

Append at the end of the existing numeric sequence by default -- inserting
mid-sequence means renumbering everything after it, which is a scheduling
decision for a future Station Manager, not this pipeline. To swap out
an existing slot instead (same or new number), add
`--replace <existing-filename>`. `--dry-run`/`-n` previews either case
without making changes. Run `on-air-place.sh --help` for the full usage.

Without the sudoers setup above, the restart step fails with
`sudo: a password is required` (the script runs over non-interactive SSH,
which can't prompt) -- the symlink change itself still succeeds either way.

## Non-goals (deferred)

Twitter/X and Discord connectors (web/RSS-Atom is built -- see
`pipeline/news-intern/`), the Intern role's "Screening Rules" (referenced
in `The-Spirit-of-OmaRadio.md`, not yet written -- source-list membership
is the screening for now), song-credit announcements, genre-aware/
DJ-hosted-genre music selection, and automated Alan/Relay scheduling are
all out of scope for this pipeline (the playlist builder itself only knows
about Mox's, Nova's, and Nikon's owned shift blocks -- see
`infra/transmitter/playlist-builder/build_playlist.py`). `--brief` is
still required alongside `--news-item` and sets angle/tone/prominence --
Alan automatically routing news to DJs, and DJs having "ambient" news
awareness without explicit `--news-item` selection, are both future work.
