# OmaRadio / transmitter-one deployment Makefile
#
# Two update rhythms, on purpose:
#   - cliamp-server : external Go dependency, pinned to a tagged release.
#                      Update deliberately with `make cliamp-update VERSION=vX.Y.Z`.
#   - platform      : this repo (AI pipeline / agents), pull + redeploy freely.
#
# CLIAMP_REPO currently points at github.com/choyer/cliamp-server (a
# personal fork, NOT the omaradio org), not bjarneo/cliamp-server upstream
# -- as of 2026-09-03 we're running a trial patch (branch `hot-reload`,
# tagged v1.4.3-omaradio.1+) that adds a SIGHUP-triggered live playlist
# reload, so a scheduled rebuild no longer disconnects listeners the way
# `systemctl restart` does. Intended to be proposed upstream as a PR once
# proven out; until then, `make cliamp-update VERSION=...` pulls tags from
# the fork, which mirrors upstream tags plus our own `-omaradio.N` ones.
# To go back to stock upstream, repoint CLIAMP_REPO and VERSION.
#
# Run as the `omaradio` user (sudo for service control / binary install
# steps) from /opt/omaradio -- the PARENT of this repo's checkout at
# /opt/omaradio/platform, not from inside platform/ itself. That's why
# SERVICE_SRC/CONFIG_SRC/etc. below are prefixed `platform/...`. Confirmed
# 2026-09-03 that this checkout is real but easy to lose track of -- it
# had gone stale for days before anyone noticed; `cd /opt/omaradio/platform
# && git pull` before relying on anything here.

VERSION      ?= v1.4.3-omaradio.2
CLIAMP_REPO  := https://github.com/choyer/cliamp-server
CLIAMP_DIR   := vendor/cliamp-server
BIN_DIR      := bin
INSTALL_BIN  := /usr/local/bin/cliamp-server
SERVICE      ?= cliamp-server
SERVICE_UNIT := /etc/systemd/system/cliamp-server.service
SERVICE_SRC  := platform/deploy/cliamp-server.service

GEOIP_URL    := https://github.com/P3TERX/GeoLite.mmdb/releases/latest/download/GeoLite2-Country.mmdb
GEOIP_DB     := /opt/omaradio/geoip/GeoLite2-Country.mmdb
STATS_DIR    := /opt/omaradio/stats

CONFIG_SRC   := platform/deploy/config.toml
CONFIG_DST   := /opt/omaradio/config.toml
SECRETS_DIR  := /opt/omaradio/secrets
SECRETS_ENV  := $(SECRETS_DIR)/cliamp.env

PLAYLIST_SRC_DIR      := platform/infra/transmitter/playlist-builder
PLAYLIST_DST_DIR       := /opt/omaradio/playlist-builder
PLAYLIST_SERVICE_SRC   := platform/deploy/omaradio-playlist-builder@.service
PLAYLIST_TIMER_SRC     := platform/deploy/omaradio-playlist-builder@.timer
PLAYLIST_SERVICE_UNIT  := /etc/systemd/system/omaradio-playlist-builder@.service
PLAYLIST_TIMER_UNIT    := /etc/systemd/system/omaradio-playlist-builder@.timer

.PHONY: help deploy status logs \
        cliamp-fetch cliamp-build cliamp-install cliamp-update cliamp-service-install \
        geoip-fetch geoip-install \
        platform-pull platform-install platform-restart \
        playlist-builder-service-install

help:
	@echo "Targets:"
	@echo "  make cliamp-service-install     - one-time: install + enable the systemd unit"
	@echo "  make deploy                     - pull + redeploy omaradio-platform"
	@echo "  make cliamp-update VERSION=vX.Y.Z - build+install a pinned cliamp-server release"
	@echo "  make geoip-install              - fetch + install latest GeoLite2-Country.mmdb"
	@echo "  make playlist-builder-service-install - one-time: install + enable the 6-hourly playlist builder timer"
	@echo "  make status                     - show status of managed services"
	@echo "  make logs SERVICE=cliamp-server - tail logs for a service"

# --- cliamp-server: external dependency, pinned to a release tag ---------

cliamp-fetch:
	@if [ -d $(CLIAMP_DIR) ]; then \
		cd $(CLIAMP_DIR) && git fetch --tags; \
	else \
		git clone $(CLIAMP_REPO) $(CLIAMP_DIR); \
	fi
	cd $(CLIAMP_DIR) && git checkout $(VERSION)

cliamp-build: cliamp-fetch
	mkdir -p $(BIN_DIR)
	cd $(CLIAMP_DIR) && go build -o ../../$(BIN_DIR)/cliamp-server .
	@echo "Built cliamp-server $(VERSION) -> $(BIN_DIR)/cliamp-server"

# One-time: install the systemd unit itself. Safe to re-run (idempotent).
# Also ensures the stats db's parent directory exists -- cliamp-server
# creates the .db file and schema itself on startup, but refuses to start
# if $(STATS_DIR) is missing -- and provisions the config + secrets
# locations the unit's ExecStart/EnvironmentFile point at.
cliamp-service-install:
	sudo cp $(SERVICE_SRC) $(SERVICE_UNIT)
	sudo systemctl daemon-reload
	sudo systemctl enable cliamp-server
	sudo install -o omaradio -g omaradio -m 755 -d $(STATS_DIR)
	sudo install -o omaradio -g omaradio -m 644 -D $(CONFIG_SRC) $(CONFIG_DST)
	sudo install -o omaradio -g omaradio -m 700 -d $(SECRETS_DIR)
	@if [ ! -f $(SECRETS_ENV) ]; then \
		printf 'ADMIN_PASSWORD=\n' | sudo tee $(SECRETS_ENV) >/dev/null; \
		sudo chown omaradio:omaradio $(SECRETS_ENV); \
		sudo chmod 600 $(SECRETS_ENV); \
		echo "Created empty $(SECRETS_ENV) -- set ADMIN_PASSWORD there, then restart cliamp-server."; \
	else \
		echo "$(SECRETS_ENV) already exists, leaving it untouched."; \
	fi
	@echo "cliamp-server.service installed and enabled (not started yet)."
	@echo "$(STATS_DIR) ready for the stats db; $(CONFIG_DST) installed from $(CONFIG_SRC)."

cliamp-install: cliamp-build
	-sudo systemctl stop cliamp-server
	sudo install -m 755 $(BIN_DIR)/cliamp-server $(INSTALL_BIN)
	sudo systemctl start cliamp-server
	sudo systemctl status cliamp-server --no-pager

# Explicit alias -- this is the command you actually run to upgrade:
#   make cliamp-update VERSION=v1.4.0
cliamp-update: cliamp-install
	@echo "cliamp-server now running $(VERSION)"

# --- geoip: GeoLite2-Country.mmdb, always tracks upstream's latest release -

geoip-fetch:
	mkdir -p $(BIN_DIR)
	curl -fL -o $(BIN_DIR)/GeoLite2-Country.mmdb $(GEOIP_URL)
	@echo "Fetched GeoLite2-Country.mmdb -> $(BIN_DIR)/GeoLite2-Country.mmdb"

geoip-install: geoip-fetch
	sudo install -o omaradio -g omaradio -m 644 -D $(BIN_DIR)/GeoLite2-Country.mmdb $(GEOIP_DB)
	@echo "GeoLite2-Country.mmdb installed -> $(GEOIP_DB)"

# --- playlist-builder: rebuilds on-air/ every 6h (00/06/12/18 UTC) --------
#
# One-time: install the script + templated service/timer units. Safe to
# re-run (idempotent). Enables the timer but deliberately does NOT start
# it -- the first unattended restart of the live stream should be an
# explicit human go-ahead, not a `make` side effect. Verify with the
# script's --dry-run/--plan-only/--no-restart modes first (see
# infra/transmitter/playlist-builder/build_playlist.py's own docstring),
# then: sudo systemctl start omaradio-playlist-builder@one.timer

playlist-builder-service-install:
	@command -v ffprobe >/dev/null || { echo "ffprobe not found -- run: sudo apt install ffmpeg"; exit 1; }
	sudo install -o omaradio -g omaradio -m 755 -d $(PLAYLIST_DST_DIR)
	sudo install -o omaradio -g omaradio -m 755 $(PLAYLIST_SRC_DIR)/build_playlist.py $(PLAYLIST_DST_DIR)/build_playlist.py
	sudo cp $(PLAYLIST_SERVICE_SRC) $(PLAYLIST_SERVICE_UNIT)
	sudo cp $(PLAYLIST_TIMER_SRC) $(PLAYLIST_TIMER_UNIT)
	sudo systemctl daemon-reload
	sudo systemctl enable omaradio-playlist-builder@one.timer
	@echo "omaradio-playlist-builder@one.timer installed and enabled (not started yet)."
	@echo "Verify with --dry-run/--plan-only/--no-restart first, then:"
	@echo "  sudo systemctl start omaradio-playlist-builder@one.timer"

# --- omaradio-platform: this repo, actively developed ---------------------

platform-pull:
	cd platform && git pull

platform-install: platform-pull
	# Add dependency install steps here as the pipeline solidifies, e.g.:
	#   pip install -r requirements.txt --break-system-packages
	@echo "platform deps installed"

platform-restart:
	sudo systemctl restart omaradio-pipeline

deploy: platform-install platform-restart
	@echo "omaradio-platform deployed."

# --- operational helpers ---------------------------------------------------

status:
	systemctl status cliamp-server omaradio-pipeline --no-pager

logs:
	journalctl -u $(SERVICE) -f
