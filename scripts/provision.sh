#!/bin/sh
# OpenChicken board-side entry: use the local profile, never refetch upstream scripts.
set -eu
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec sh "$SCRIPT_DIR/setup-board.sh" "$@"
