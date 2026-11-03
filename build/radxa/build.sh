#!/bin/sh
# Build only. This script never connects to a robot or installs system services.
set -eu
cd /src
export CARGO_HOME=/work/cargo
export CARGO_TARGET_DIR=/work/target
export CARGO_BUILD_JOBS="${CARGO_BUILD_JOBS:-6}"
export CARGO_INCREMENTAL=0
mkdir -p /work/logs /work/output "$CARGO_HOME"
rustc --version
cargo --version
dpkg-query -W -f='${binary:Package}\t${Version}\n' > /work/packages.tsv
rustup target list --installed > /work/rust-targets.txt
cp /opt/target-debs/SHA256SUMS /work/target-debs.sha256
cp /opt/target-debs/package-metadata.txt /work/target-package-metadata.txt
cp /opt/target-debs/download-uris.txt /work/target-download-uris.txt
cargo fetch --locked > /work/logs/cargo-fetch.log 2>&1
echo 'Dependencies fetched; running native offline checks.'
cargo test --offline --locked -p openchicken-control safety::tests \
    > /work/logs/safety-tests.log 2>&1
cargo test --offline --locked -p openchicken-ipc-proto -p robotd-params -p kinematics \
    > /work/logs/core-tests.log 2>&1
echo 'Native checks passed; building ARM64 release binaries.'
cargo build --offline --locked --release --target aarch64-unknown-linux-gnu --bins \
    > /work/logs/arm64-build.log 2>&1
python3 build/radxa/package.py
echo 'Build and package complete. No robot deployment performed.'
