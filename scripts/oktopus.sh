#!/usr/bin/env bash
# Runs Oktopus (an open source USP controller, github.com/OktopUSP/oktopus)
# next to the lab and plugs it into the agent as another controller.
#
#   scripts/oktopus.sh up     fetch the pinned release, start it, plug it in
#   scripts/oktopus.sh down   unplug it and stop it (its data is kept)
#
# Oktopus runs as its own compose project from a checkout in .oktopus/, with
# oktopus/compose.override.yml applied. The lab must already be running: the
# override attaches Oktopus's broker to the lab's network.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIR="$ROOT/.oktopus"
REPO=https://github.com/OktopUSP/oktopus.git
COMMIT=e1f07d71a93c4169421f2e94ce6605746ece37ad
LAB="http://localhost:${VDEV_HTTP_HOST_PORT:-8080}"
UI="http://127.0.0.1:${VDEV_OKTOPUS_PORT:-8090}"

export OKTOPUS_TAG="${COMMIT:0:7}"
export VDEV_LAB_DIR="$ROOT"
# Only what an MQTT-connected agent needs: no STOMP, WebSocket, CWMP or Portainer
export COMPOSE_PROFILES=nats,controller,mqtt,adapter,frontend

compose() {
    docker compose -p oktopus \
        -f "$DIR/deploy/compose/docker-compose.yaml" \
        -f "$ROOT/oktopus/compose.override.yml" "$@"
}

fetch() {
    if [ "$(git -C "$DIR" rev-parse HEAD 2>/dev/null)" = "$COMMIT" ]; then
        return
    fi
    echo "oktopus: fetching $REPO @ ${COMMIT:0:7}"
    mkdir -p "$DIR"
    git -C "$DIR" init --quiet
    git -C "$DIR" fetch --quiet --depth 1 "$REPO" "$COMMIT"
    git -C "$DIR" checkout --quiet --force FETCH_HEAD
}

# Oktopus learns of a device once, when its broker sees the agent subscribe;
# an agent that connects before Oktopus's adapters are listening is never
# listed. So plugging in waits for them. The log lines are those of the
# pinned commit.
logged() {          # container $1 has logged the line $2
    # Not grep -q: exiting at the first match would SIGPIPE docker logs, which
    # pipefail reports as a failure
    docker logs "$1" 2>&1 | grep -F -- "$2" >/dev/null
}
wait_ready() {
    local deadline=$((SECONDS + 120))
    while [ $SECONDS -lt $deadline ]; do
        if curl -sf "$UI/api/auth/admin/exists" >/dev/null 2>&1 \
            && logged oktopus-mqtt-adapter "Subscribed to oktopus/usp/+/status/+" \
            && logged oktopus-adapter "Listening for nats events"; then
            return 0
        fi
        sleep 2
    done
    echo "oktopus: not ready after 120 s - see: docker compose -p oktopus logs" >&2
    exit 1
}

up() {
    if ! docker network inspect vdev-lab >/dev/null 2>&1; then
        echo "oktopus: the lab is not running - start it first (make up or make dev)" >&2
        exit 1
    fi
    fetch
    compose up -d
    echo "oktopus: waiting for it to be ready"
    wait_ready
    # Plugging in replaces any earlier rows, so the agent always makes a fresh
    # connection that Oktopus sees
    echo "oktopus: plugging it into the agent"
    if ! curl -sf -X POST "$LAB/api/controllers" -H 'Content-Type: application/json' \
            --data @"$ROOT/controllers/oktopus.json" >/dev/null; then
        echo "oktopus: the lab at $LAB did not accept the controller - is the agent up?" >&2
        exit 1
    fi
    echo
    echo "Oktopus UI:  $UI   (first visit: create an admin account)"
    echo 'unplug:      make oktopus-down, or set Device.MQTT.Client.[Alias=="oktopus"].Enable to false in Browse'
}

down() {
    curl -sf -X DELETE "$LAB/api/controllers/oktopus" >/dev/null \
        && echo "oktopus: unplugged from the agent" \
        || echo "oktopus: was not plugged in (or the lab is not running)"
    if [ -f "$DIR/deploy/compose/docker-compose.yaml" ]; then
        compose down
    fi
}

case "${1:-}" in
    up) up ;;
    down) down ;;
    *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
