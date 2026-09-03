#!/usr/bin/env bash
#
# on-air-place.sh
#
# Symlinks an already-synced media file into a station's on-air/ rotation
# on transmitter-one, then tells cliamp-server to pick up the change.
#
# Default is `systemctl reload` (SIGHUP) -- a LIVE swap with NO listener
# disconnection. This only works on the github.com/choyer/cliamp-server
# hot-reload fork we're currently running (see deploy/Makefile's header
# comment); confirmed empirically (2026-09-02) that a stock upstream
# cliamp-server binary does NOT notice on-air/ changes at all without a
# full restart, which DOES disconnect listeners. Pass --restart to force
# the old restart-based behavior (useful if the fork misbehaves).
#
# IMPORTANT once infra/transmitter/playlist-builder/build_playlist.py's
# systemd timer is running (every 6h, at 00/06/12/18 UTC): that job does a
# FULL replace of on-air/, with no knowledge of anything placed here. A
# manual placement via this script survives only until the next scheduled
# boundary, then gets silently wiped. Fine for a quick one-off (e.g. an ad
# hoc Alan/Relay appearance) as long as you know it's temporary.
#
# Usage:
#   ./on-air-place.sh <station> <remote-source-path> <on-air-filename>
#   ./on-air-place.sh <station> <remote-source-path> <on-air-filename> --replace <existing-filename>
#   ./on-air-place.sh ... --restart          (force restart instead of reload)
#   ./on-air-place.sh [--dry-run|-n] ...     (preview only, no changes)
#
# <remote-source-path> is the file's path on transmitter-one (i.e. under
# /mnt/media_library/library/...), NOT a local path -- run
# infra/transmitter/vault/sync-library.sh first so it's actually there.
#
# Examples:
#   # Append as the next track:
#   ./on-air-place.sh one /mnt/media_library/library/dj-segments/dj-mox/generated/20260902T231820Z-dj-mox-mox-welcome-to-omaradio-one.mp3 003-mox_welcome.mp3
#
#   # Replace an existing slot (removes the old symlink first, same or new number):
#   ./on-air-place.sh one /mnt/media_library/library/dj-segments/dj-mox/generated/<id>.mp3 001-mox_welcome.mp3 --replace 001-num_init_testing.mp3
#
# Requires passwordless sudo for the exact commands `systemctl reload
# cliamp-server` and `systemctl restart cliamp-server`, for the omaradio
# user on transmitter-one -- this is NOT set up by default (the hardening
# script only adds omaradio to the sudo group, which still prompts for a
# password). One-time setup, run interactively on transmitter-one (needs
# your sudo password once):
#
#   ssh omaradio@transmitter-one.omaradio.stream
#   printf 'omaradio ALL=(root) NOPASSWD: /usr/bin/systemctl restart cliamp-server\n%s\n' \
#     'omaradio ALL=(root) NOPASSWD: /usr/bin/systemctl reload cliamp-server' \
#     | sudo tee /etc/sudoers.d/omaradio-cliamp-restart
#   sudo chmod 440 /etc/sudoers.d/omaradio-cliamp-restart
#   sudo visudo -c        # validates syntax -- must report "parsed OK"
#
# Without that, the apply step below fails with "sudo: a password is
# required" (this script runs over non-interactive SSH, which can't prompt).

set -euo pipefail

REMOTE_USER="omaradio"
REMOTE_HOST="transmitter-one.omaradio.stream"
ON_AIR_BASE="/mnt/media_library/stations"

# --- Colors (fall back to plain text if the terminal doesn't support them) ---
BOLD=$(tput bold 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
BLUE=$(tput setaf 4 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")

step() { echo -e "\n${BOLD}${BLUE}==>${RESET} ${BOLD}$*${RESET}"; }
ok()   { echo -e "  ${GREEN}\xe2\x9c\x93${RESET} $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; }
err()  { echo -e "  ${RED}\xe2\x9c\x97 ERROR:${RESET} $*" >&2; }
# -----------------------------------------------------------------------------

DRY_RUN=""
REPLACE=""
APPLY_VERB="reload"
POSITIONAL=()

while [ $# -gt 0 ]; do
    case "$1" in
        -n|--dry-run) DRY_RUN="1"; shift ;;
        --replace) REPLACE="$2"; shift 2 ;;
        --restart) APPLY_VERB="restart"; shift ;;
        -h|--help)
            sed -n '2,38p' "$0" | sed 's/^# \{0,1\}//'
            exit 0
            ;;
        *) POSITIONAL+=("$1"); shift ;;
    esac
done
set -- "${POSITIONAL[@]}"

if [ $# -ne 3 ]; then
    err "Usage: $0 <station> <remote-source-path> <on-air-filename> [--replace <existing-filename>] [--dry-run]"
    exit 1
fi

STATION="$1"
SOURCE_PATH="$2"
TARGET_NAME="$3"
ON_AIR_DIR="${ON_AIR_BASE}/${STATION}/on-air"
TARGET_PATH="${ON_AIR_DIR}/${TARGET_NAME}"

step "Checking source file exists on ${REMOTE_HOST}"
if ! ssh "${REMOTE_USER}@${REMOTE_HOST}" "test -f '${SOURCE_PATH}'"; then
    err "${SOURCE_PATH} doesn't exist on ${REMOTE_HOST}. Did you run sync-library.sh first?"
    exit 1
fi
ok "Found ${SOURCE_PATH}"

if [ -n "$REPLACE" ]; then
    step "Will replace ${ON_AIR_DIR}/${REPLACE} -> ${TARGET_NAME}"
else
    step "Will add ${TARGET_NAME} to ${ON_AIR_DIR}"
fi

if [ -n "$DRY_RUN" ]; then
    warn "Dry-run mode -- no changes will be made, cliamp-server will not be ${APPLY_VERB}ed."
    [ -n "$REPLACE" ] && echo "  rm ${ON_AIR_DIR}/${REPLACE}"
    echo "  ln -s ${SOURCE_PATH} ${TARGET_PATH}"
    echo "  sudo systemctl ${APPLY_VERB} cliamp-server"
    exit 0
fi

REMOTE_CMD=""
if [ -n "$REPLACE" ]; then
    REMOTE_CMD="rm -f '${ON_AIR_DIR}/${REPLACE}' && "
fi
REMOTE_CMD="${REMOTE_CMD}ln -s '${SOURCE_PATH}' '${TARGET_PATH}'"

step "Updating on-air/"
if ! ssh "${REMOTE_USER}@${REMOTE_HOST}" "$REMOTE_CMD"; then
    err "Failed to update the on-air/ symlink."
    exit 1
fi
ok "on-air/ updated."

step "${APPLY_VERB^}ing cliamp-server"
if ! ssh "${REMOTE_USER}@${REMOTE_HOST}" "sudo systemctl ${APPLY_VERB} cliamp-server"; then
    err "${APPLY_VERB^} failed -- if this is 'sudo: a password is required', see this script's"
    err "header comment for the one-time NOPASSWD sudoers setup."
    exit 1
fi
ok "cliamp-server ${APPLY_VERB}ed."

step "Current on-air/ listing"
ssh "${REMOTE_USER}@${REMOTE_HOST}" "ls -la '${ON_AIR_DIR}'"

echo ""
ok "Done. Confirm at https://OmaRadio.stream or tx.omaradio.stream/${STATION}/stream"
