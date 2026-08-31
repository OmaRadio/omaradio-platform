# OmaRadio / transmitter-one deployment Makefile
#
# Two update rhythms, on purpose:
#   - cliamp-server : external Go dependency, pinned to a tagged release.
#                      Update deliberately with `make cliamp-update VERSION=vX.Y.Z`.
#   - platform      : this repo (AI pipeline / agents), pull + redeploy freely.
#
# Run from the omaradio-platform repo root as the `omaradio` user (sudo for
# service control / binary install steps).

VERSION      ?= v1.3.5
CLIAMP_REPO  := https://github.com/bjarneo/cliamp-server
CLIAMP_DIR   := vendor/cliamp-server
BIN_DIR      := bin
INSTALL_BIN  := /usr/local/bin/cliamp-server
SERVICE      ?= cliamp-server
SERVICE_UNIT := /etc/systemd/system/cliamp-server.service
SERVICE_SRC  := platform/deploy/cliamp-server.service

GEOIP_URL    := https://github.com/P3TERX/GeoLite.mmdb/releases/latest/download/GeoLite2-Country.mmdb
GEOIP_DB     := /opt/omaradio/geoip/GeoLite2-Country.mmdb

.PHONY: help deploy status logs \
        cliamp-fetch cliamp-build cliamp-install cliamp-update cliamp-service-install \
        geoip-fetch geoip-install \
        platform-pull platform-install platform-restart

help:
	@echo "Targets:"
	@echo "  make cliamp-service-install     - one-time: install + enable the systemd unit"
	@echo "  make deploy                     - pull + redeploy omaradio-platform"
	@echo "  make cliamp-update VERSION=vX.Y.Z - build+install a pinned cliamp-server release"
	@echo "  make geoip-install              - fetch + install latest GeoLite2-Country.mmdb"
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
cliamp-service-install:
	sudo cp $(SERVICE_SRC) $(SERVICE_UNIT)
	sudo systemctl daemon-reload
	sudo systemctl enable cliamp-server
	@echo "cliamp-server.service installed and enabled (not started yet)."

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
