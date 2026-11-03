#!/bin/sh
# Separate speaker entry; no package downloads, runtime install or motor operations.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HELPER="$SCRIPT_DIR/../deploy/openchicken/configure_audio.py"
[ -f "$HELPER" ] || HELPER=/usr/local/share/openchicken/hat/configure_audio.py
exec python3 "$HELPER" "$@"
