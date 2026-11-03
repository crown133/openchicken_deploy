#!/bin/sh
# Transfer only this local HAT configuration bundle. No release install or reboot.
# Usage: sh scripts/provision-board.sh --plan|--apply user@board
set -eu
mode="${1:-}"
host="${2:-}"
case "$mode" in
    --help|-h)
        printf '%s\n' 'usage: sh scripts/provision-board.sh --plan|--apply user@board'
        exit 0 ;;
    --plan|--apply) ;;
    *) printf '%s\n' 'choose --plan or --apply explicitly' >&2; exit 2 ;;
esac
[ "$#" = 2 ] || { printf '%s\n' 'exactly one SSH target is required' >&2; exit 2; }
case "$host" in
    ''|-*|*[!A-Za-z0-9_.@:-]*) printf '%s\n' 'invalid SSH target' >&2; exit 2 ;;
esac
for tool in ssh scp; do
    command -v "$tool" >/dev/null 2>&1 || { printf '%s\n' "missing $tool" >&2; exit 1; }
done
ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
extension="${OPENCHICKEN_EXT:-}"
case "$extension" in
    ''|none|spi3|uart9) ;;
    *) printf '%s\n' 'OPENCHICKEN_EXT must be none, spi3 or uart9' >&2; exit 2 ;;
esac
if [ "$mode" = --apply ]; then
    remote_uid=$(ssh "$host" id -u)
    [ "$remote_uid" = 0 ] || {
        printf '%s\n' '--apply requires a root SSH session; this entry does not elevate privileges' >&2
        exit 1
    }
fi
remote_dir=$(ssh "$host" 'mktemp -d -p . openchicken-stage.XXXXXXXX')
case "$remote_dir" in
    ./openchicken-stage.*|openchicken-stage.*) ;;
    *) printf '%s\n' 'unexpected remote staging path' >&2; exit 1 ;;
esac
case "$remote_dir" in
    *[!A-Za-z0-9_./-]*) printf '%s\n' 'unsafe remote staging path' >&2; exit 1 ;;
esac
ssh "$host" "mkdir -p '$remote_dir/scripts' '$remote_dir/deploy'"
scp "$ROOT/scripts/setup-board.sh" "$ROOT/scripts/provision.sh" "$host:$remote_dir/scripts/"
scp -r "$ROOT/deploy/openchicken" "$host:$remote_dir/deploy/"
scp -r "$ROOT/deploy/audio" "$host:$remote_dir/deploy/"
printf '%s\n' "Local OpenChicken source staged at $host:$remote_dir"
ssh "$host" "OPENCHICKEN_EXT='$extension' sh '$remote_dir/scripts/provision.sh' '$mode'"
