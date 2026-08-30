#!/usr/bin/env bash
#
# sync-library.sh
#
# Pushes local media files into the OmaRadio vault's library/ tree on
# transmitter-one via rsync over SSH. Populates library/ only -- the
# stations/<station>/on-air/ symlink farm is rebuilt separately by the
# daily playlist builder and is never touched by this script.
#
# Usage:
#   ./sync-library.sh                  # sync every category
#   ./sync-library.sh music jingles    # sync only the named categories
#   ./sync-library.sh --dry-run        # preview what would transfer, no changes
#   ./sync-library.sh -n music         # dry-run a single category
#
# Config below, or override at call time, e.g.:
#   LOCAL_LIBRARY=~/Music/omaradio ./sync-library.sh
#
# Requires: rsync, and an SSH key already trusted by the omaradio user
# (set up by infra/transmitter/provisioning/hardening.sh).

set -euo pipefail

# --- Configuration ---------------------------------------------------------
REMOTE_USER="omaradio"
REMOTE_HOST="transmitter-one.omaradio.stream"        # add an alias to ~/.ssh/config, or replace with the droplet's IP/DNS name
REMOTE_VAULT="/mnt/media-library/library"
LOCAL_LIBRARY="${LOCAL_LIBRARY:-$HOME/Work/OmaRadio/media_library}"

CATEGORIES=(music jingles dj-segments news-desk shoutouts ads)
# ---------------------------------------------------------------------------

# --- Colors (fall back to plain text if the terminal doesn't support them) ---
BOLD=$(tput bold 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
BLUE=$(tput setaf 4 2>/dev/null || echo "")
RED=$(tput setaf 1 2>/dev/null || echo "")

step() { echo -e "\n${BOLD}${BLUE}==>${RESET} ${BOLD}$*${RESET}"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; }
info() { echo -e "  ${BLUE}i${RESET} $*"; }
err()  { echo -e "  ${RED}✗ ERROR:${RESET} $*" >&2; }
# -----------------------------------------------------------------------------

if ! command -v rsync &>/dev/null; then
    err "rsync not found. Install it first (e.g. 'sudo apt install rsync' or 'brew install rsync')."
    exit 1
fi

DRY_RUN=""
SELECTED=()

for arg in "$@"; do
    case "$arg" in
        -n|--dry-run)
            DRY_RUN="--dry-run"
            ;;
        -h|--help)
            echo "Usage: $0 [--dry-run] [category ...]"
            echo "Categories: ${CATEGORIES[*]}"
            exit 0
            ;;
        *)
            SELECTED+=("$arg")
            ;;
    esac
done

if [ "${#SELECTED[@]}" -eq 0 ]; then
    SELECTED=("${CATEGORIES[@]}")
fi

step "Syncing to ${BOLD}${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_VAULT}${RESET}"
[ -n "$DRY_RUN" ] && warn "Dry-run mode -- no files will actually be transferred."

RSYNC_OPTS=(-az --human-readable --info=progress2 --stats)
[ -n "$DRY_RUN" ] && RSYNC_OPTS+=("$DRY_RUN")

for category in "${SELECTED[@]}"; do
    local_path="${LOCAL_LIBRARY}/${category}/"
    remote_path="${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_VAULT}/${category}/"

    step "Category: ${category}"

    if [ ! -d "$local_path" ]; then
        warn "No local folder at ${local_path} -- skipping."
        continue
    fi

    if [ -z "$(ls -A "$local_path" 2>/dev/null)" ]; then
        warn "${local_path} is empty -- skipping."
        continue
    fi

    if rsync "${RSYNC_OPTS[@]}" "$local_path" "$remote_path"; then
        ok "Synced ${category}."
    else
        err "rsync failed for ${category}."
        exit 1
    fi
done

step "Done"
ok "Library sync complete."
info "This only updates library/. Run the daily playlist builder on"
info "transmitter-one afterward if you want on-air/ to reflect new content."
