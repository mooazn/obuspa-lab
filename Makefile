.PHONY: help venv proto build up dev down logs test clean reset flash eject docker-running oktopus oktopus-down

PYTHON ?= python3
VENV   := .venv
PIP    := $(VENV)/bin/pip
PY     := $(VENV)/bin/python

help:
	@echo "make venv    - create the local venv and generate USP protobuf bindings"
	@echo "make up      - pull the prebuilt images and start the stack (seconds)"
	@echo "make dev     - build the images from this tree and start the stack (minutes)"
	@echo "make test    - run the test suite against a running stack"
	@echo "              (pytest arguments via ARGS, e.g. make test ARGS=\"-x -k clock\")"
	@echo "make logs    - follow logs from all services"
	@echo "make down    - stop the stack (keeps agent database)"
	@echo "make reset   - stop the stack and factory reset the agent database"
	@echo "make flash SRC=~/obuspa      - build that obuspa tree onto the SD card"
	@echo "make flash REF=v10.0.0-master - build an upstream ref onto the SD card"
	@echo "make flash PLUGINS=\"examples/disk-monitor\" - your plug-in(s), against the built-in obuspa"
	@echo "                               (combine with SRC/REF to build them against that tree)"
	@echo "make eject   - wipe the SD card"
	@echo "make oktopus - run the Oktopus controller next to the lab and plug it in"
	@echo "make oktopus-down - unplug and stop it"
	@echo
	@echo "Bringing your own code: docs/vendor-integration.md"

$(VENV)/bin/activate: controller/requirements.txt
	$(PYTHON) -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip
	$(PIP) install --quiet -r controller/requirements.txt
	@touch $(VENV)/bin/activate

venv: $(VENV)/bin/activate proto

proto: $(VENV)/bin/activate
	@PYTHON=$(PY) ./scripts/gen_proto.sh

# Host port for the web UI and API. Exported so that compose publishes on it
# and the test suite targets it: make up VDEV_HTTP_HOST_PORT=8081
VDEV_HTTP_HOST_PORT ?= 8080
export VDEV_HTTP_HOST_PORT

# `up` pulls the published images; `dev` builds them from this tree. Both use
# the same compose project, so down/logs/reset apply to either.
COMPOSE_DEV := docker compose -f docker-compose.yml -f docker-compose.dev.yml

# Fails early and plainly when the Docker daemon is not reachable, instead of
# surfacing as a pull or socket error further in
docker-running:
	@docker info >/dev/null 2>&1 || { \
	  echo "cannot reach the Docker daemon - is Docker running?"; exit 1; }

up: docker-running
	@docker compose pull --quiet || { \
	  echo; echo "could not pull the prebuilt images (not published yet, or private?)"; \
	  echo "build them from this tree instead:  make dev"; exit 1; }
	docker compose up -d
	@echo
	@echo "web UI:  http://localhost:$(VDEV_HTTP_HOST_PORT)"
	@echo "broker:  localhost:1883"
	@echo "watch:   make logs      (editing the source? use: make dev)"

dev: docker-running
	$(COMPOSE_DEV) up -d --build
	@echo
	@echo "web UI:  http://localhost:$(VDEV_HTTP_HOST_PORT)   (built from this tree)"

build:
	$(COMPOSE_DEV) build

down:
	docker compose down

reset:
	docker compose down -v

logs:
	docker compose logs -f

# Extra pytest arguments, e.g. make test ARGS="-x -k clock"
test: venv
	$(VENV)/bin/pytest tests/ -v $(ARGS)

clean:
	rm -rf $(VENV) controller/uspctl/proto/*_pb2.py
	find . -name __pycache__ -type d -prune -exec rm -rf {} +

# "Flash" an obuspa build onto the SD card. Either a local tree you have
# edited (SRC) or an upstream git ref (REF). LABEL names it in the UI.
FLASH_ARGS := $(if $(SRC),--src "$(SRC)") $(if $(REF),--ref "$(REF)") \
              $(foreach p,$(PLUGINS),--plugin "$(p)") $(if $(LABEL),--label "$(LABEL)")

flash: docker-running
	@if [ -z "$(SRC)$(REF)$(PLUGINS)" ]; then \
	  echo "usage: make flash [SRC=<obuspa checkout> | REF=<git ref>] [PLUGINS=\"dir ...\"] [LABEL=name]"; exit 2; fi
	./scripts/flash.sh $(FLASH_ARGS)

eject:
	rm -rf sdcard/obuspa sdcard/vdev_plugin.so sdcard/manifest.json sdcard/plugins
	@echo "SD card wiped"

# Oktopus, an open source USP controller, as a second controller for the agent
# (scripts/oktopus.sh, oktopus/compose.override.yml, controllers/oktopus.json)
oktopus: docker-running
	@./scripts/oktopus.sh up

oktopus-down: docker-running
	@./scripts/oktopus.sh down
