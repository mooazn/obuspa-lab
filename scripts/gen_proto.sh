#!/usr/bin/env bash
# Generates Python bindings for the USP protobuf schemas.
#
# The schemas are the Broadband Forum originals from BroadbandForum/usp, renamed
# only because Python cannot import module names containing dashes.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PROTO_DIR="$ROOT/controller/usp_proto"
OUT_DIR="$ROOT/controller/uspctl/proto"

mkdir -p "$OUT_DIR"
touch "$OUT_DIR/__init__.py"

PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
if [ ! -x "$PYTHON" ]; then
    PYTHON="$(command -v python3)"
fi

"$PYTHON" -m grpc_tools.protoc \
    -I "$PROTO_DIR" \
    --python_out="$OUT_DIR" \
    "$PROTO_DIR/usp_msg.proto" \
    "$PROTO_DIR/usp_record.proto"

echo "generated bindings in $OUT_DIR"
