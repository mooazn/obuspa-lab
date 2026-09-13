#!/bin/sh
# Compiles vendor plug-ins against an obuspa source tree.
#
#   build-plugins.sh <obuspa tree> <output dir> [<plug-in sources dir>]
#
# Each subdirectory of the sources dir is one plug-in and becomes <name>.so:
#   - with a Makefile: `make OBUSPA_SRC=<tree> VHAL_SRC=<vhal>` must leave
#     exactly one *.so behind (the Makefile owns its flags)
#   - without one: every *.c in the directory is compiled into a single .so
#     with the flags a plug-in needs (see plugin/Makefile for the same set)
#
# Runs inside the build container (agent/Dockerfile.flash), never on the host.

set -e

TREE="$1"
OUT="$2"
SRC="${3:-/usr/local/src/vendor-plugins}"
VHAL_SRC="${VHAL_SRC:-/usr/local/src/vdev-plugin-flash/vhal}"

[ -n "$TREE" ] && [ -n "$OUT" ] || { echo "usage: $0 <obuspa tree> <out dir> [<sources dir>]" >&2; exit 2; }
[ -d "$TREE/src/include" ] || { echo "build-plugins: $TREE is not an obuspa tree" >&2; exit 1; }

mkdir -p "$OUT"

INCLUDES="-I$TREE/src/include -I$TREE/src/vendor -I$TREE/src/core"
VHAL_FLAGS=""
VHAL_SOURCES=""
if [ -f "$VHAL_SRC/vhal.c" ]; then
    VHAL_FLAGS="-I$VHAL_SRC -DHAVE_VHAL"
    VHAL_SOURCES="$VHAL_SRC/vhal.c"
fi

count=0
for dir in "$SRC"/*/; do
    [ -d "$dir" ] || continue
    name="$(basename "$dir")"
    echo "build-plugins: $name"

    if [ -f "$dir/Makefile" ]; then
        make -C "$dir" OBUSPA_SRC="$TREE" VHAL_SRC="$VHAL_SRC"
        built="$(find "$dir" -maxdepth 2 -name '*.so' -type f)"
        n="$(printf '%s\n' "$built" | grep -c . || true)"
        if [ "$n" -ne 1 ]; then
            echo "build-plugins: $name: expected exactly one .so after make, found $n" >&2
            exit 1
        fi
        cp "$built" "$OUT/$name.so"
    else
        sources="$(find "$dir" -maxdepth 1 -name '*.c' -type f | sort)"
        if [ -z "$sources" ]; then
            echo "build-plugins: $name: no Makefile and no .c files" >&2
            exit 1
        fi
        # shellcheck disable=SC2086
        cc -shared -fPIC -Wall -O2 -DENABLE_UDS $INCLUDES $VHAL_FLAGS \
            $sources $VHAL_SOURCES -o "$OUT/$name.so" -lpthread
    fi

    count=$((count + 1))
done

echo "build-plugins: $count plug-in(s) in $OUT"
