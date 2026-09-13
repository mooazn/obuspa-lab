#!/bin/sh
# Reconcile only the platform-owned ballast on the configured tmpfs mount.
set -u
# No `set -e`: reconcile() returns early with the status of whatever test
# failed last, and a steady-state pass must not be able to kill the loop.
RUN="${VDEV_RUN_DIR:-/run/vdev}"
DATA="${VDEV_DATA_PATH:-/data}"
DIR="$RUN/faults"
BALLAST="$DATA/.vdev-ballast"
mkdir -p "$DIR"
last=''
ack() {
    boot=$(jq -r '.bootedAt // ""' "$RUN/booted-from.json")
    jq -n --argjson ok "$1" --arg detail "$2" --arg error "$3" \
        --arg revision "$revision" --arg boot "$boot" --argjson at "$(date +%s)" \
        '{ok:$ok,detail:$detail,error:(if $error == "" then null else $error end),revision:$revision,boot:$boot,appliedAt:$at}' \
        > "$DIR/disk_fill.applied.tmp"
    mv "$DIR/disk_fill.applied.tmp" "$DIR/disk_fill.applied"
    echo "faultd: disk_fill: $2 $3"
}
reconcile() {
    # Check the actual mount, before either creating OR deleting ballast.
    if ! awk -v p="$DATA" '$2 == p && $3 == "tmpfs" {ok=1} END {exit !ok}' /proc/mounts; then
        if [ -f "$DIR/disk_fill.json" ]; then
            revision=$(jq -r '.revision' "$DIR/disk_fill.json")
            if [ "$last" != "$revision" ]; then
                ack false '' 'configured data path is not a tmpfs mount'
                last=$revision
            fi
        fi
        return 0
    fi
    if [ ! -f "$DIR/disk_fill.json" ]; then
        if [ -f "$BALLAST" ]; then rm -f "$BALLAST"; echo 'faultd: disk_fill cleared'; fi
        rm -f "$DIR/disk_fill.applied"
        last=''
        return 0
    fi
    request=$(cat "$DIR/disk_fill.json") || return 0
    revision=$(printf '%s' "$request" | jq -r '.revision')
    percent=$(printf '%s' "$request" | jq -er '.params.percent | select(type == "number" and . >= 1 and . <= 100 and . == floor)') || return 0
    [ "$revision" != "$last" ] || [ ! -f "$BALLAST" ] || return 0
    rm -f "$BALLAST"
    set -- $(df -Pk "$DATA" | awk 'END {print $2, $3}')
    target=$(( ($1 * percent / 100 - $2) * 1024 ))
    error=''
    if [ "$target" -gt 0 ]; then
        if ! fallocate -l "$target" "$BALLAST" 2>/dev/null; then
            rm -f "$BALLAST"
            # Allocate real pages; truncate alone would create a sparse file.
            dd if=/dev/zero of="$BALLAST" bs=1024 count=$((target / 1024)) 2>/dev/null || error='could not allocate requested ballast'
        fi
    else
        : > "$BALLAST"
    fi
    actual=$(df -Pk "$DATA" | awk 'END {print $5}')
    if [ -z "$error" ]; then ack true "$actual used" ''; else ack false "$actual used" "$error"; fi
    last=$revision
}
case "${1:-watch}" in
    apply-once) reconcile ;;
    watch) while :; do reconcile || true; sleep 1; done ;;
    *) echo 'usage: faultd.sh [apply-once|watch]' >&2; exit 2 ;;
esac
