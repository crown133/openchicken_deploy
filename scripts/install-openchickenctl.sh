#!/bin/sh
# Install the `openchickenctl` a branch last built, on Linux or macOS, with no Rust toolchain.
#
#   curl -fsSL the upstream resource | sh
#   curl -fsSL .../install-openchickenctl.sh | sh -s -- udp-pad
#
# The binary comes from the `daemon-dev-<branch>` prerelease that `.github/workflows/dev.yml`
# replaces on every push; the branch defaults to `main`. It goes to `$OPENCHICKENCTL_DIR`, by default
# `~/.local/bin`, which needs no sudo.
set -eu

REPO="${OPENCHICKEN_REPO:?Set OPENCHICKEN_REPO after local validation}"
branch="${1:-main}"
dir="${OPENCHICKENCTL_DIR:-$HOME/.local/bin}"

case "$(uname -s)" in
    Linux) os=linux ;;
    Darwin) os=macos ;;
    *) echo "install-openchickenctl: $(uname -s) is not covered here; Windows takes the .zip" >&2; exit 1 ;;
esac
case "$(uname -m)" in
    x86_64 | amd64) arch=x86_64 ;;
    arm64 | aarch64)
        if [ "$os" = macos ]; then arch=arm64; else arch=aarch64; fi ;;
    *) echo "install-openchickenctl: no build for $(uname -m)" >&2; exit 1 ;;
esac

url="https://github.com/${REPO}/releases/download/daemon-dev-${branch}/openchickenctl-${os}-${arch}.tar.gz"
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT

echo "openchickenctl from ${branch}: ${url}"
if ! curl -fsSL "$url" -o "$tmp/openchickenctl.tar.gz"; then
    echo "install-openchickenctl: no build of '${branch}' — has the branch been pushed since dev.yml" >&2
    echo "built openchickenctl, and has that run finished?" >&2
    exit 1
fi
tar -xzf "$tmp/openchickenctl.tar.gz" -C "$tmp"
mkdir -p "$dir"
mv "$tmp/openchickenctl" "$dir/openchickenctl"
chmod +x "$dir/openchickenctl"
echo "installed $dir/openchickenctl"

case ":${PATH}:" in
    *":${dir}:"*) ;;
    *) echo "$dir is not on your PATH — add it, or run $dir/openchickenctl" ;;
esac
