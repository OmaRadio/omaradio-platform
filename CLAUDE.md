# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

OmaRadio is an Omarchy-Linux-focused pirate radio platform (in-universe: broadcasting via "Transmitter-One" from the Falkland Islands), streaming at https://OmaRadio.stream. It aims to be a fully-autonomous platform: AI DJ staff producing on-air segments, Creative Commons music, and community news, with an underlying streaming server and infra to actually get it on the air.

This repo (`omaradio-platform`) is currently **early-stage and mostly scaffolding**: the infra/deploy side (getting `cliamp-server` running on the transmitter host) is real and working, and `pipeline/dj-segment/` (see below) is a first, small slice of the AI production pipeline. `scheduler/`, `www/`, and most of `staff/` are still placeholder directories with no code yet. Don't assume implementation exists just because a directory does — check before referencing.

Read `README.md` for the directory layout and `The-Spirit-of-OmaRadio.md` for the full editorial/tone guidelines and platform role definitions (Orchestrator, Station Manager, DJ, Intern, IT Guy) before doing any content/staff-persona work. Two rules from there worth internalizing directly:
- Resources are tight — be efficient, resourceful, and frugal in what you build/spin up.
- Any destructive action or any action with real-world cost (cloud resources, etc.) must be surfaced to the Orchestrator (the human) for an explicit go/no-go **before** it's enacted — this repo's own guidelines reinforce the general Claude Code confirmation norms, don't skip it here.

## No build/test/lint commands (yet)

There is no repo-wide package.json, go.mod, or CI config. `scheduler/src`, `scheduler/db/migrations`, and `www/` are still empty — don't invent commands for them; set up tooling as part of whatever work first adds real code there.

The one piece of real, runnable infrastructure besides the pipeline below is `cliamp-server` (the streaming server), an **external** Go project (`bjarneo/cliamp-server`, vendored/pinned by tag) — its build lives in this repo's `deploy/Makefile`, not as source here.

### `pipeline/dj-segment/` — AI DJ segment pipeline (MVP)

The first real application code in this repo: generates one AI-written, locally-TTS'd DJ segment at a time, gated behind Orchestrator approval, and documents getting it on air. Python, run via `uv run <script>.py` — each script carries its own PEP 723 inline dependency block, so there's no repo-level `requirements.txt`/`pyproject.toml` and no manual install step. Full walkthrough (setup, usage, the manual on-air runbook): `pipeline/dj-segment/README.md`.

- `generate_segment.py --dj <slug> --brief "..."` — builds a prompt from `The-Spirit-of-OmaRadio.md` + the persona (`staff/stations/<station>/djs/<slug>/persona.{toml,md}`), calls the Anthropic API (`claude-sonnet-5` by default) for a structured `{title, est_seconds, script}`, then renders it to MP3 via local `kokoro-onnx` (no TTS API cost) + ffmpeg. Needs `ANTHROPIC_API_KEY` in a repo-root `.env` (see `.env.example`) and the Kokoro model weights (~350MB, gitignored, fetched separately — see README). `--dry-run` sanity-checks the assembled prompt with no API call.
  - `--dj` isn't DJ-exclusive: it also matches other staff personas by the `slug` field inside their `persona.toml`, for singular-per-station roles (`station-manager/`, `intern/`) that don't have a `djs/`-style slug subfolder — see `find_dj_dir()`. Used sparingly, per that persona's own boundaries.
- `review_segment.py {list,show,approve,reject}` — the approval gate. Nothing `generate_segment.py` writes reaches the media library (and therefore can never sync or air) until `approve` copies it into `$LOCAL_LIBRARY/dj-segments/<dj>/generated/`; `reject` never touches the library.
- Two DJs exist so far: `staff/stations/one/djs/{dj-mox,dj-nova}/` — add more by following that directory's `persona.toml`/`persona.md` shape. `staff/stations/one/station-manager/` (Alan) and `staff/stations/one/intern/` (Relay) are working examples of non-DJ staff personas wired in the same way — Relay's `persona.md` also documents the (not-yet-built) news-connector pipeline intent: Orchestrator-defined news sites + a future Discord bot/Twitter API, screened and summarized for DJs to draw on via the Station Manager.
- Deferred/non-goals (see the pipeline README for the full list): news connectors, the Intern role and its still-undefined "Screening Rules", song-credit announcements, genre-aware/DJ-hosted-genre music selection. On-air placement is now automated for Mox's/Nova's owned shift blocks — see `infra/transmitter/playlist-builder/` below; `on-air-place.sh` remains for one-off manual placements only.

## Streaming infra architecture (`deploy/`, `infra/transmitter/`)

The stack running on the transmitter host ("transmitter-one"):

```
Caddy (:80/:443, Cloudflare DNS-01 TLS) --reverse_proxy--> cliamp-server (:8000) --reads--> station on-air/ symlink farms
```

- **`deploy/Makefile`** — deploys both the vendored `cliamp-server` binary and this platform repo on the transmitter host. Two independent, intentionally different update rhythms:
  - `make cliamp-update VERSION=vX.Y.Z` — deliberate, pinned upgrade of the external `cliamp-server` dependency (cloned into `vendor/cliamp-server`, built, installed to `/usr/local/bin/cliamp-server`, service restarted).
  - `make deploy` (platform-pull + platform-restart) — pull-and-redeploy this repo freely.
  - `make cliamp-service-install` — one-time systemd unit install; also provisions `/opt/omaradio/stats` (cliamp-server refuses to start without it), installs `config.toml`, and creates the `/opt/omaradio/secrets/cliamp.env` file (holds `ADMIN_PASSWORD`, never committed).
  - **Path gotcha:** the Makefile's `SERVICE_SRC`/`CONFIG_SRC` targets are prefixed `platform/deploy/...` — it expects to be run from a parent directory where this repo is checked out into a subdirectory literally named `platform` (sibling to `vendor/`), not from this repo's own root.
  - `make geoip-install` fetches/installs `GeoLite2-Country.mmdb` from the upstream GeoLite mirror (always latest, not pinned).
- **`deploy/config.toml`** — cliamp-server config: one `[stations.<id>]` block per station, each pointing at a hand-built, numerically-ordered symlink farm (`.../stations/<id>/on-air/`). `shuffle = false` and `recursive = false` are load-bearing — the ordering scheme depends on both staying off/flat. When copy-pasting a new `[stations.<id>]` block, double-check the header key itself was renamed, not just the fields inside it — TOML silently last-key-wins on a duplicate table header, so a missed rename overwrites the earlier station instead of erroring.
- **`deploy/cliamp-server.service`** — systemd unit; admin password comes from `EnvironmentFile=-/opt/omaradio/secrets/cliamp.env` (leading `-` = optional, so the service still starts, with admin auth disabled, if the secret isn't provisioned yet).
- **`deploy/Caddyfile`** — proxies `tx.omaradio.stream` on both `:80` and `:443` to cliamp-server; both ports are listed explicitly to disable Caddy's automatic http→https redirect, because non-browser stream clients (VLC, cliamp, etc.) need plain `:80` to keep working, while the web player needs `:443`. Per-station path routing (`/one/stream`, `/enigma/stream`) is handled inside cliamp-server itself, not by Caddy.
- **`infra/transmitter/provisioning/init-ubuntu-harden.sh`** — one-time, run-as-root droplet hardening (Ubuntu 24.04): creates the `omaradio` sudo user from the root SSH key, disables root/password SSH login, configures ufw (SSH/80/443 only), enables unattended-upgrades (auto-reboot deliberately left off — a live stream shouldn't restart out from under itself), installs fail2ban.
- **`infra/transmitter/vault/init-omaradio-vault.sh`** — interactive, run-once-per-volume: builds the media vault directory skeleton (`library/`, `schedule/<station>/`, `stations/<station>/on-air/`) on the mounted DO block storage volume. Station list and DJ list are edited directly in the script's `STATIONS`/`DJ_NAMES` arrays. Dated leaf folders (e.g. `schedule/<station>/YYYY/MM`) are deliberately *not* created here — `infra/transmitter/playlist-builder/build_playlist.py` (below) owns those now.
- **`infra/transmitter/vault/sync-library.sh`** — rsyncs local media into the vault's `library/` tree only (never touches the `on-air/` symlink farm, which the playlist builder rebuilds separately). `./sync-library.sh [--dry-run|-n] [category ...]`; categories: `music jingles dj-segments news-desk shoutouts ads number-messages`. Defaults `LOCAL_LIBRARY` to `~/Work/OmaRadio/media_library/library`, overridable via env var.
- **`infra/transmitter/playlist-builder/build_playlist.py`** — stdlib-only Python, runs natively on transmitter-one (not a dev-machine tool) via a systemd timer (`deploy/omaradio-playlist-builder@.timer`, `OnCalendar=*-*-* 00,06,12,18:00:00 UTC`) rather than `uv`/PEP 723 — no third-party deps needed, and that host runs no other Python. Every 6h, rebuilds a station's `on-air/` from already-approved/synced vault content and restarts `cliamp-server`. Shift ownership is a hardcoded `STATION_SHIFTS` table in the script (station "one": Mox owns 00:00–06:00 UTC, Nova owns 12:00–18:00 UTC, other blocks are music-only) — 100% of scheduling logic lives here because cliamp-server itself has none (confirmed via its source: plain lexicographic path sort, infinite loop, no hot-reload). Writes an audit-trail schedule JSON to `schedule/<station>/YYYY/MM/DD-HHMM.json` before ever touching `on-air/`, and keeps exactly one prior `on-air/` generation as `on-air.prev/` for one-command rollback (see the script's own docstring). Install via `make playlist-builder-service-install` in `deploy/Makefile` (mirrors `cliamp-service-install`'s pattern) — enables but does not start the timer; starting it is a deliberate separate step. **Gotcha**: its systemd service deliberately omits `NoNewPrivileges=true` (present on `cliamp-server.service`) because it shells out to `sudo systemctl restart cliamp-server` via the same NOPASSWD sudoers rule `on-air-place.sh` uses — that flag would silently break the restart. Does a **full replace** of `on-air/` every run, so any manual `on-air-place.sh` placement survives only until the next 6h boundary.

## Repo layout beyond infra

- **`staff/`** — one directory per platform role (`it-guy/`, `stations/<station>/{station-manager,intern,djs}/`). Only station `one` exists so far; `it-guy/` is still empty. `djs/{dj-mox,dj-nova}/`, `station-manager/` (Alan), and `intern/` (Relay) all have real personas (see the pipeline section above).
- **`pipeline/`** — AI production pipeline tooling (as opposed to `staff/`, which holds persona/identity data). `dj-segment/` is the only piece built so far — see above.
- **`scheduler/`** — intended task dispatch / staff orchestration service (`src/`, `db/migrations/`); no code yet.
- **`www/`** — intended public marketing site + web player; empty.
- **`branding/`** — logo assets (per-platform and per-station).
- **`operations/`** — non-code operational tracking docs for the Orchestrator, e.g. `expenses.md` (append-only expense ledger — see the format notes at the top of that file before editing it).

When a new station is added, it follows the existing `one` pattern under both `staff/stations/<station>/` and `infra/transmitter/deploy/stations/<station>/` (per README).
