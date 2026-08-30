#!/usr/bin/env bash
#
# omaradio-vault-skeleton.sh
#
# Creates the OmaRadio media vault directory skeleton on the mounted
# DigitalOcean block storage volume. Dated leaf directories (per-day
# schedule/segment folders, e.g. schedule/<station>/YYYY/MM or
# news-desk/YYYY-MM-DD) are intentionally NOT created here -- those get
# created on the fly by the daily playlist builder.
#
# Run with sudo if the mount point is root-owned, which is the default
# right after a DigitalOcean Volume is attached and mounted.

set -euo pipefail

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

trap 'err "Script aborted at line ${LINENO}. Check ${MOUNT_DIR:-the target directory} before re-running."' ERR
# -------------------------------------------------------------------------

# -------------------------------------------------------------------------
# Config -- edit these arrays as stations/DJs are added. Empty DJ_NAMES is
# fine; the loop below just skips it and creates the shared parent folder.
# -------------------------------------------------------------------------
STATIONS=("one")
DJ_NAMES=()   # e.g. DJ_NAMES=("dj-mox" "dj-relay")

DEFAULT_MOUNT="/mnt/media_library"

step "1. Target directory"

read -rp "$(echo -e "${BOLD}Media vault mount path${RESET} [${DEFAULT_MOUNT}]: ")" MOUNT_DIR
MOUNT_DIR="${MOUNT_DIR:-$DEFAULT_MOUNT}"
MOUNT_DIR="${MOUNT_DIR%/}"   # strip a trailing slash if the user typed one

if [[ "${MOUNT_DIR}" != /* ]]; then
    err "Path must be absolute (start with /). Got: ${MOUNT_DIR}"
    exit 1
fi

if [ ! -d "${MOUNT_DIR}" ]; then
    warn "${MOUNT_DIR} does not exist yet."
    read -rp "  Create it now? [y/N] " CREATE_ROOT
    if [[ "${CREATE_ROOT}" =~ ^[Yy]$ ]]; then
        mkdir -p "${MOUNT_DIR}" \
            || { err "Could not create ${MOUNT_DIR}. Check permissions (try sudo) or confirm the volume is attached & mounted."; exit 1; }
        ok "Created ${MOUNT_DIR}."
    else
        err "Aborting -- nothing to build on."
        exit 1
    fi
fi

if [ ! -w "${MOUNT_DIR}" ]; then
    err "${MOUNT_DIR} is not writable by $(whoami). Try re-running with sudo."
    exit 1
fi

if command -v mountpoint &>/dev/null && ! mountpoint -q "${MOUNT_DIR}"; then
    warn "${MOUNT_DIR} doesn't look like an active mount point right now."
    warn "Fine for a dry run -- just confirm the Volume is attached and"
    warn "mounted there before this becomes the real media vault."
fi

ok "Using ${BOLD}${MOUNT_DIR}${RESET} as the vault root."

step "2. Review"

echo -e "  Stations: ${BOLD}${STATIONS[*]:-none configured}${RESET}"
if [ "${#DJ_NAMES[@]}" -eq 0 ]; then
    echo -e "  DJs:      ${BOLD}none configured yet${RESET} (edit DJ_NAMES in this script later)"
else
    echo -e "  DJs:      ${BOLD}${DJ_NAMES[*]}${RESET}"
fi

read -rp "$(echo -e "\n${BOLD}Proceed with creating the skeleton in ${MOUNT_DIR}? [y/N] ${RESET}")" CONFIRM
if [[ ! "${CONFIRM}" =~ ^[Yy]$ ]]; then
    warn "Aborted -- no directories were created."
    exit 0
fi

step "3. Creating library/"

mkdir -p "${MOUNT_DIR}/library/music"
ok "library/music"

for station in "${STATIONS[@]}"; do
    mkdir -p "${MOUNT_DIR}/library/jingles/${station}"/{idents,sweepers,intros,outros}
    ok "library/jingles/${station}/{idents,sweepers,intros,outros}"
done

mkdir -p "${MOUNT_DIR}/library/dj-segments"
ok "library/dj-segments"
if [ "${#DJ_NAMES[@]}" -gt 0 ]; then
    for dj in "${DJ_NAMES[@]}"; do
        mkdir -p "${MOUNT_DIR}/library/dj-segments/${dj}"/{interludes,generated}
        ok "library/dj-segments/${dj}/{interludes,generated}"
    done
fi

mkdir -p "${MOUNT_DIR}/library/news-desk"
ok "library/news-desk"

mkdir -p "${MOUNT_DIR}/library/shoutouts"
ok "library/shoutouts"

mkdir -p "${MOUNT_DIR}/library/ads"
ok "library/ads"

step "4. Creating schedule/"

for station in "${STATIONS[@]}"; do
    mkdir -p "${MOUNT_DIR}/schedule/${station}"
    ok "schedule/${station}"
done

step "5. Creating stations/ (cliamp-server on-air paths)"

for station in "${STATIONS[@]}"; do
    mkdir -p "${MOUNT_DIR}/stations/${station}/on-air"
    mkdir -p "${MOUNT_DIR}/stations/${station}/ads"
    mkdir -p "${MOUNT_DIR}/stations/${station}/number-messages"
    ok "stations/${station}/{on-air,ads,number-messages}"
done

step "6. Ownership"

TARGET_USER="omaradio"
if id "${TARGET_USER}" &>/dev/null; then
    if chown -R "${TARGET_USER}:${TARGET_USER}" "${MOUNT_DIR}" 2>/dev/null; then
        ok "Ownership set to ${TARGET_USER}:${TARGET_USER}."
    else
        warn "Could not chown to ${TARGET_USER} (probably not running as root)."
        warn "Run: sudo chown -R ${TARGET_USER}:${TARGET_USER} ${MOUNT_DIR}"
    fi
else
    warn "User '${TARGET_USER}' doesn't exist on this host yet -- skipping chown."
    warn "Run the hardening script first, or chown manually once it does."
fi

step "Done"

echo -e "${BOLD}${GREEN}Skeleton created at ${MOUNT_DIR}${RESET}"
if command -v tree &>/dev/null; then
    tree -L 4 "${MOUNT_DIR}"
else
    find "${MOUNT_DIR}" -maxdepth 4 -type d | sort
    info "(install 'tree' for nicer output next time: apt-get install tree)"
fi

echo ""
info "Dated leaf folders (schedule/<station>/YYYY/MM, dj-segments/*/generated/YYYY-MM-DD,"
info "news-desk/YYYY-MM-DD, shoutouts/YYYY-MM-DD) are intentionally not created here --"
info "those get created by the daily playlist builder."
