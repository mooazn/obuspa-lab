#!/bin/sh
# Boots the agent the way a gateway's bootloader would:
#
#   1. honour a pending factory reset (wipe the agent database)
#   2. pick the image: the SD card if one is seated and carries an image,
#      otherwise the built-in one
#   3. report what was booted, so the device UI can show it
#   4. wait for the device to be serving its data model, then start obuspa
#
# The device writes boot.json and the factory-reset marker into the shared
# run directory; this script is the only thing that reads them.

set -e

RUN_DIR="${VDEV_RUN_DIR:-/run/vdev}"
SDCARD="${VDEV_SDCARD_DIR:-/sdcard}"
SOCK="${VDEV_SOCK:-$RUN_DIR/vdev.sock}"
DB="${VDEV_DB:-$RUN_DIR/usp.db}"
IFACE="${VDEV_IFACE:-eth0}"
RESET_FILE="${VDEV_RESET_FILE:-/etc/obuspa/factory_reset.txt}"
VERBOSITY="${VDEV_LOG_LEVEL:-4}"

INTERNAL_BIN=/usr/local/bin/obuspa
INTERNAL_PLUGIN=/usr/local/lib/vdev_plugin.so

log() { echo "entrypoint: $*"; }

# --- 0. serial console ----------------------------------------------------------
# Everything this container prints - bootloader lines, obuspa, vendor plug-ins,
# the fault daemon - goes through a FIFO into `tee`, which writes it both to
# the container's stdout (so `docker compose logs` still works) and to a file
# on the shared volume that the device tails as a serial console. A FIFO
# rather than a pipeline so that `exec obuspa` below still replaces the shell.
LOG="$RUN_DIR/agent.log"
mkdir -p "$RUN_DIR"
[ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"
rm -f /tmp/console
mkfifo /tmp/console
tee -a "$LOG" </tmp/console &
exec >/tmp/console 2>&1

log "==== boot $(date -u +%Y-%m-%dT%H:%M:%SZ) ===="

# --- 1. wait for the hardware ---------------------------------------------------
# Everything below reads state the device owns (the factory-reset marker,
# boot.json, the fault list). It has to be read *after* the device is up: if
# the device reboots while we are waiting here - a card ejected and reset
# pressed during a boot - the decision must reflect the new state, not one
# captured before the socket went away.
i=0
while [ ! -S "$SOCK" ]; do
    i=$((i + 1))
    if [ "$i" -gt 240 ]; then
        log "timed out waiting for device service socket at $SOCK" >&2
        exit 1
    fi
    sleep 0.5
done
log "device is up"

# --- 2. factory reset ---------------------------------------------------------
if [ -f "$RUN_DIR/factory-reset" ]; then
    log "factory reset pending ($(cat "$RUN_DIR/factory-reset")): wiping agent database"
    rm -f "$DB" "$DB-journal"
    rm -f "$RUN_DIR/factory-reset"
fi

# A database created before the lab controller existed lacks Controller.2, and
# obuspa ignores endpoints it has no Controller row for. Add the rows from the
# factory file, written directly to the database before obuspa opens it.
# A fresh database gets them from the factory file in the usual way.
if [ -f "$DB" ]; then
    LAB_ID="$("$INTERNAL_BIN" -f "$DB" -c dbget Device.LocalAgent.Controller.2.EndpointID 2>/dev/null \
        | sed -n 's/^Device.LocalAgent.Controller.2.EndpointID => //p')"
    if [ -z "$LAB_ID" ]; then
        log "adding the lab controller (Controller.2) to the existing agent database"
        grep '^Device\.LocalAgent\.Controller\.2\.' "$RESET_FILE" | while read -r path value; do
            value="${value#\"}"; value="${value%\"}"
            "$INTERNAL_BIN" -f "$DB" -c dbset "$path" "$value" >/dev/null 2>&1 \
                || log "could not set $path in the agent database"
        done
    elif [ "$LAB_ID" != "self::vdev-lab" ]; then
        log "Controller.2 is $LAB_ID, not the lab controller - the web UI's Browse view will get no answers"
    fi
fi

# --- 3. choose the image and the plug-ins --------------------------------------
# Two independent decisions when the card is seated: boot its obuspa binary if
# it has one, and load its vendor plug-ins if it has any. A card may carry
# plug-ins alone, in which case the built-in binary boots with them.
BOOT_FROM=internal
BIN="$INTERNAL_BIN"
PLUGIN="$INTERNAL_PLUGIN"
CARD_SEATED=false
PLUGIN_LIST=""          # JSON array body for the boot report

if [ -f "$RUN_DIR/boot.json" ] && grep -q '"sdcard"' "$RUN_DIR/boot.json"; then
    CARD_SEATED=true
    mkdir -p /tmp/sdboot

    if [ -f "$SDCARD/obuspa" ]; then
        # Copy off the (read-only, possibly noexec) mount so we can run it
        cp "$SDCARD/obuspa" /tmp/sdboot/obuspa && chmod +x /tmp/sdboot/obuspa
        BIN=/tmp/sdboot/obuspa
        BOOT_FROM=sdcard
        if [ -f "$SDCARD/vdev_plugin.so" ]; then
            cp "$SDCARD/vdev_plugin.so" /tmp/sdboot/vdev_plugin.so
            PLUGIN=/tmp/sdboot/vdev_plugin.so
        fi
        log "booting from SD card"
    fi

    # Vendor plug-ins load after the data-model proxy, in name order, each as
    # its own -x. obuspa initialises plug-ins in command-line order.
    if ls "$SDCARD"/plugins/*.so >/dev/null 2>&1; then
        mkdir -p /tmp/sdboot/plugins
        for so in "$SDCARD"/plugins/*.so; do
            name="$(basename "$so")"
            cp "$so" "/tmp/sdboot/plugins/$name"
            set -- "$@" -x "/tmp/sdboot/plugins/$name"
            PLUGIN_LIST="$PLUGIN_LIST${PLUGIN_LIST:+,}\"/tmp/sdboot/plugins/$name\""
            log "loading vendor plug-in $name from SD card"
        done
    fi

    if [ "$BOOT_FROM" = internal ] && [ -z "$PLUGIN_LIST" ]; then
        log "SD card is seated but carries nothing - booting the built-in image"
        BOOT_FROM=internal-fallback
    fi
fi

# --- 4. report ------------------------------------------------------------------
MANIFEST=null
if [ "$CARD_SEATED" = true ] && [ -f "$SDCARD/manifest.json" ]; then
    MANIFEST="$(cat "$SDCARD/manifest.json")"
fi

# The firmware image's identity, served as Device.DeviceInfo.SoftwareVersion.
# Distinct from Device.LocalAgent.SoftwareVersion (the obuspa release): two
# builds of one release are different images. A card image is named by its
# label and commit; plug-ins on top of the built-in image append the label.
IMAGE="builtin-${VDEV_BUILTIN_IMAGE:-unknown}"
CARD_IMAGE="$(printf '%s' "$MANIFEST" | jq -r '[.label, .commit]
    | map(select(. != null and . != "" and . != "unknown")) | join("-")' 2>/dev/null)"
case "$BOOT_FROM" in
    sdcard)   IMAGE="${CARD_IMAGE:-card}" ;;
    internal) [ -n "$PLUGIN_LIST" ] && IMAGE="$IMAGE+$(printf '%s' "$MANIFEST" | jq -r '.label // "card"')" ;;
esac
export VDEV_SOFTWARE_VERSION="$IMAGE"

printf '{"from":"%s","binary":"%s","cardSeated":%s,"plugins":[%s],"manifest":%s,"softwareVersion":"%s","bootedAt":"%s"}\n' \
    "$BOOT_FROM" "$BIN" "$CARD_SEATED" "$PLUGIN_LIST" "$MANIFEST" "$IMAGE" "$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
    > "$RUN_DIR/booted-from.json"

/usr/local/bin/faultd.sh apply-once
/usr/local/bin/faultd.sh watch &

# --- 5. start ---------------------------------------------------------------------
log "starting $BIN ($BOOT_FROM)"

# The firmware keeps the lab's time, not the host's. libfaketime re-reads the
# device-written control file on every call; the plug-in asks the device to
# rewrite it during init so this process anchors to the current lab time.
export LD_PRELOAD=/usr/local/lib/libfaketimeMT.so.1
export FAKETIME_TIMESTAMP_FILE="$RUN_DIR/faketime"
export FAKETIME_NO_CACHE=1
export TZ=UTC

exec "$BIN" \
    -p -v "$VERBOSITY" \
    -r "$RESET_FILE" \
    -f "$DB" \
    -i "$IFACE" \
    -x "$PLUGIN" \
    "$@"
