#!/usr/bin/env bash
# "Flashes" an SD card: builds obuspa from a source tree (or a git ref) and/or
# vendor plug-ins, and writes the binaries plus a manifest into ./sdcard, which
# the agent container boots from when the device has the card seated.
#
#   scripts/flash.sh --src ~/obuspa                      # a local checkout you edited
#   scripts/flash.sh --ref v10.0.0-master                # any git ref of upstream
#   scripts/flash.sh --plugin examples/disk-monitor      # your plug-in, built-in obuspa
#   scripts/flash.sh --src ~/obuspa --plugin ./my-plugin # both, plug-in built against your tree
#   ... --label "fix-1"                                   # name it for the UI
#
# Plug-ins are compiled against whichever obuspa tree the card will boot: the
# flashed one if --src/--ref is given, otherwise the built-in one. That is what
# catches vendor-API drift between obuspa releases before it reaches a device.
#
# Flashing always rewrites the whole card.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CARD="$ROOT/sdcard"
SRC=""
REF=""
LABEL=""
PLUGINS=()
UPSTREAM="https://github.com/BroadbandForum/obuspa.git"

while [ $# -gt 0 ]; do
    case "$1" in
        --src)    SRC="$2"; shift 2 ;;
        --ref)    REF="$2"; shift 2 ;;
        --label)  LABEL="$2"; shift 2 ;;
        --plugin) PLUGINS+=("$2"); shift 2 ;;
        -h|--help) sed -n '2,16p' "$0"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
done

if [ -z "$SRC" ] && [ -z "$REF" ] && [ ${#PLUGINS[@]} -eq 0 ]; then
    echo "usage: $0 [--src <obuspa checkout> | --ref <git ref>] [--plugin <dir>]... [--label <name>]" >&2
    exit 2
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

# --- the obuspa tree, if any -----------------------------------------------------
if [ -n "$REF" ]; then
    SRC="$TMP/obuspa"
    echo "flash: cloning obuspa @ $REF"
    git clone --quiet --depth 1 --branch "$REF" "$UPSTREAM" "$SRC"
fi

COMMIT=""; VERSION=""; DIRTY=""
if [ -n "$SRC" ]; then
    SRC="${SRC/#\~/$HOME}"           # zsh does not expand ~ in make VAR=~/x
    SRC="$(cd "$SRC" && pwd)"
    [ -f "$SRC/configure.ac" ] || { echo "flash: $SRC does not look like an obuspa tree" >&2; exit 1; }
    COMMIT="$(git -C "$SRC" rev-parse --short HEAD 2>/dev/null || echo unknown)"
    if [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ]; then DIRTY="-dirty"; fi
    # The release obuspa reports as Device.LocalAgent.SoftwareVersion. AC_INIT
    # in configure.ac is not maintained upstream (it stays at 1.0.0).
    VERSION="$(sed -n 's/^#define AGENT_SOFTWARE_VERSION *"\([^"]*\)".*/\1/p' "$SRC/src/core/version.h" 2>/dev/null | head -1)"
    [ -n "$VERSION" ] || VERSION="$(sed -n 's/^AC_INIT(\[[^]]*\],\[\([^]]*\)\].*/\1/p' "$SRC/configure.ac" | head -1)"
    TARGET=card
else
    SRC="$TMP/no-obuspa"; mkdir -p "$SRC"       # unused context, must still exist
    TARGET=card-plugins
fi

# --- plug-in sources, staged under one build context ------------------------------
PLUG_STAGE="$TMP/plugins"
mkdir -p "$PLUG_STAGE"
PLUGIN_NAMES=()
# ${arr[@]+"${arr[@]}"} expands to nothing for an empty array without tripping
# `set -u` on bash 3.2 (macOS); plain "${arr[@]}" does.
for dir in ${PLUGINS[@]+"${PLUGINS[@]}"}; do
    dir="${dir/#\~/$HOME}"
    [ -d "$dir" ] || { echo "flash: plug-in dir not found: $dir" >&2; exit 1; }
    name="$(basename "$(cd "$dir" && pwd)")"
    cp -R "$(cd "$dir" && pwd)" "$PLUG_STAGE/$name"
    rm -f "$PLUG_STAGE/$name"/*.so "$PLUG_STAGE/$name"/*.o     # never ship stale host builds
    PLUGIN_NAMES+=("$name")
done

if [ -z "$LABEL" ]; then
    if [ -n "$COMMIT" ]; then LABEL="obuspa-${REF:-local}-${COMMIT}${DIRTY}"; else LABEL="plugins"; fi
    [ ${#PLUGIN_NAMES[@]} -gt 0 ] && LABEL="$LABEL+$(IFS=+; echo "${PLUGIN_NAMES[*]}")"
fi

echo "flash: building $LABEL"
[ -n "$COMMIT" ] && echo "flash:   obuspa $VERSION @ $COMMIT$DIRTY from ${REF:-$SRC}"
[ ${#PLUGIN_NAMES[@]} -gt 0 ] && echo "flash:   plug-ins: ${PLUGIN_NAMES[*]}"

# --- build --------------------------------------------------------------------------
docker build --quiet --target build-stage -t obuspa_sim-buildenv -f "$ROOT/agent/Dockerfile" "$ROOT" >/dev/null

mkdir -p "$CARD"
rm -rf "$CARD/obuspa" "$CARD/vdev_plugin.so" "$CARD/manifest.json" "$CARD/plugins"

docker build \
    -f "$ROOT/agent/Dockerfile.flash" \
    --target "$TARGET" \
    --build-context obuspa-src="$SRC" \
    --build-context plugins="$PLUG_STAGE" \
    --output "type=local,dest=$CARD" \
    "$ROOT"

[ -f "$CARD/obuspa" ] && chmod +x "$CARD/obuspa"

# --- manifest -----------------------------------------------------------------------
# `stat -f %z` is file size on macOS but *filesystem* status on GNU/Linux,
# where it succeeds with a multi-line dump; wc is the same everywhere.
size_of() { wc -c < "$1" | tr -d ' '; }

PLUGIN_JSON="[]"
if [ -d "$CARD/plugins" ] && ls "$CARD/plugins"/*.so >/dev/null 2>&1; then
    PLUGIN_JSON="["
    first=1
    for so in "$CARD/plugins"/*.so; do
        name="$(basename "$so" .so)"
        [ $first -eq 1 ] || PLUGIN_JSON="$PLUGIN_JSON,"
        first=0
        PLUGIN_JSON="$PLUGIN_JSON{\"name\":\"$name\",\"size\":$(size_of "$so")}"
    done
    PLUGIN_JSON="$PLUGIN_JSON]"
else
    rm -rf "$CARD/plugins"
fi

if [ -n "$COMMIT" ]; then
    OBUSPA_JSON="\"obuspaVersion\": \"$VERSION\", \"commit\": \"$COMMIT$DIRTY\", \"source\": \"${REF:-$SRC}\", \"size\": $(size_of "$CARD/obuspa")"
else
    OBUSPA_JSON='"obuspaVersion": null, "commit": null, "source": null, "size": null'
fi

# Written last and atomically: the device reads the card through a bind mount
# and treats "manifest present" as "image present", so it must never see a
# half-written one.
cat > "$CARD/.manifest.json.tmp" <<JSON
{
  "label": "$LABEL",
  $OBUSPA_JSON,
  "plugins": $PLUGIN_JSON,
  "builtAt": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
JSON
mv -f "$CARD/.manifest.json.tmp" "$CARD/manifest.json"

echo
echo "flash: card written"
cat "$CARD/manifest.json"
echo
echo "Seat the card in the 3D view (or POST /api/sdcard {\"action\":\"insert\"}) and press reset."
