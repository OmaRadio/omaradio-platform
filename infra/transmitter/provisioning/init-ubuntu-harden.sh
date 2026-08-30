#!/usr/bin/env bash
#
# omaradio-hardening.sh
# Basic OS hardening for the OmaRadio droplet (Ubuntu 24.04 LTS)
# Run as root, once, right after first login. Docker is intentionally
# skipped here -- add it later when you're ready to deploy.
#
# What this does:
#   1. Creates a non-root sudo user "omaradio", copies root's SSH key to
#      it, sets a local password (for sudo only), and disables root +
#      password SSH login
#   2. Configures ufw: default-deny incoming, allow SSH/HTTP/HTTPS
#      (ufw mirrors rules across IPv4 and IPv6 automatically)
#   3. Enables unattended-upgrades for automatic security patches
#      (automatic reboot is left OFF -- see note near the end)
#   4. Installs fail2ban with the SSH jail enabled
#
# IMPORTANT: Before closing this session, open a NEW terminal and confirm
# you can SSH in as "omaradio" and that "sudo whoami" returns "root".
# If you get locked out, use the DigitalOcean web console to fix it.
 
set -euo pipefail
 
NEW_USER="omaradio"
 
# --- Colors (fall back to plain text if the terminal doesn't support them) ---
BOLD=$(tput bold 2>/dev/null || echo "")
RESET=$(tput sgr0 2>/dev/null || echo "")
GREEN=$(tput setaf 2 2>/dev/null || echo "")
YELLOW=$(tput setaf 3 2>/dev/null || echo "")
BLUE=$(tput setaf 4 2>/dev/null || echo "")
 
step() { echo -e "\n${BOLD}${BLUE}==>${RESET} ${BOLD}$*${RESET}"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $*"; }
warn() { echo -e "  ${YELLOW}!${RESET} $*"; }
info() { echo -e "  ${BLUE}i${RESET} $*"; }
# -------------------------------------------------------------------------
 
step "1. Creating user '${NEW_USER}'"
 
if id "${NEW_USER}" &>/dev/null; then
    warn "User ${NEW_USER} already exists, skipping creation."
else
    adduser --disabled-password --gecos "" "${NEW_USER}"
    usermod -aG sudo "${NEW_USER}"
    ok "Created ${NEW_USER} and added to the sudo group."
fi
 
# Copy root's authorized_keys (the SSH key you used to create the droplet)
# so key-based login keeps working once root login is disabled below.
mkdir -p /home/${NEW_USER}/.ssh
if [ -f /root/.ssh/authorized_keys ]; then
    cp /root/.ssh/authorized_keys /home/${NEW_USER}/.ssh/authorized_keys
    ok "Copied root's SSH key to ${NEW_USER}."
else
    warn "/root/.ssh/authorized_keys not found."
    warn "Add a public key to /home/${NEW_USER}/.ssh/authorized_keys manually,"
    warn "or you will lock yourself out once root login is disabled."
fi
chmod 700 /home/${NEW_USER}/.ssh
chmod 600 /home/${NEW_USER}/.ssh/authorized_keys 2>/dev/null || true
chown -R ${NEW_USER}:${NEW_USER} /home/${NEW_USER}/.ssh
 
# adduser --disabled-password means there's no password yet -- but sudo
# still needs one locally (SSH login stays key-only regardless).
info "Set a local password for ${NEW_USER} (used only for 'sudo', not SSH):"
passwd "${NEW_USER}"
 
step "2. Disabling root and password SSH login"
 
# Drop-in file, per Ubuntu 24.04's sshd_config.d convention, rather than
# editing sshd_config directly.
cat > /etc/ssh/sshd_config.d/99-hardening.conf << 'EOF'
PermitRootLogin no
PasswordAuthentication no
EOF
 
# Validate before restarting so a typo can't lock you out.
sshd -t
systemctl restart ssh
ok "Root SSH login and password auth disabled."
 
step "3. Configuring ufw"
 
ufw allow OpenSSH
ufw allow 80/tcp    # HTTP -- Let's Encrypt ACME challenge + Caddy
ufw allow 443/tcp   # HTTPS
 
# Confirm IPv6 rules are mirrored alongside IPv4 (default on Ubuntu,
# enforced explicitly here).
sed -i 's/^IPV6=.*/IPV6=yes/' /etc/default/ufw
 
ufw --force enable
ufw status verbose
ok "ufw enabled: SSH, HTTP, HTTPS allowed (IPv4 + IPv6)."
 
step "4. Enabling unattended-upgrades"
 
apt-get update -qq
apt-get install -y unattended-upgrades
 
cat > /etc/apt/apt.conf.d/20auto-upgrades << 'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
 
systemctl enable --now unattended-upgrades
ok "unattended-upgrades enabled for security patches."
info "Automatic reboot is left OFF -- a kernel patch that needs a reboot"
info "won't restart your live stream out from under you. Check for a"
info "pending reboot yourself with: cat /var/run/reboot-required 2>/dev/null"
info "To enable scheduled auto-reboot at a quiet hour instead, add to"
info "/etc/apt/apt.conf.d/50unattended-upgrades:"
info '  Unattended-Upgrade::Automatic-Reboot "true";'
info '  Unattended-Upgrade::Automatic-Reboot-Time "04:00";'
 
step "5. Installing fail2ban (SSH jail)"
 
apt-get install -y fail2ban
 
cat > /etc/fail2ban/jail.local << 'EOF'
[DEFAULT]
bantime  = 1h
findtime = 10m
maxretry = 5
backend  = systemd
 
[sshd]
enabled = true
EOF
 
systemctl enable --now fail2ban
sleep 1
fail2ban-client status sshd || true
ok "fail2ban installed and watching SSH (5 failed attempts / 10 min -> 1h ban)."
 
echo -e "\n${BOLD}${GREEN}=== Done ===${RESET}"
echo -e "Before closing this root session:"
echo -e "  1. Open a ${BOLD}NEW${RESET} terminal window"
echo -e "  2. Run: ${BOLD}ssh ${NEW_USER}@<your-droplet-ip>${RESET}"
echo -e "  3. Confirm login works and 'sudo whoami' returns 'root'"
echo -e "Only then close this session."
 
