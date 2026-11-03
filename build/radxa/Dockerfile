# Build-only environment: native host tests and ARM64 Linux release binaries.
ARG RUST_IMAGE=rust:1.94.1-bookworm@sha256:6ae102bdbf528294bc79ad6e1fae682f6f7c2a6e6621506ba959f9685b308a55
FROM ${RUST_IMAGE}
RUN dpkg --add-architecture arm64 \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
       gcc-aarch64-linux-gnu g++-aarch64-linux-gnu pkg-config file python3 \
       libudev-dev
COPY sysroot.sh /opt/build/sysroot.sh
RUN sh /opt/build/sysroot.sh && rm -rf /var/lib/apt/lists/*
RUN rustup target add aarch64-unknown-linux-gnu
ENV CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_LINKER=aarch64-linux-gnu-gcc \
    CC_aarch64_unknown_linux_gnu=aarch64-linux-gnu-gcc \
    CXX_aarch64_unknown_linux_gnu=aarch64-linux-gnu-g++ \
    AR_aarch64_unknown_linux_gnu=aarch64-linux-gnu-ar \
    PKG_CONFIG_ALLOW_CROSS=1 \
    PKG_CONFIG_LIBDIR_aarch64_unknown_linux_gnu=/opt/target-sysroot/usr/lib/aarch64-linux-gnu/pkgconfig:/opt/target-sysroot/usr/share/pkgconfig \
    PKG_CONFIG_SYSROOT_DIR_aarch64_unknown_linux_gnu=/opt/target-sysroot \
    CARGO_TARGET_AARCH64_UNKNOWN_LINUX_GNU_RUSTFLAGS="-L native=/opt/target-sysroot/usr/lib/aarch64-linux-gnu -L native=/opt/target-sysroot/lib/aarch64-linux-gnu -C link-arg=-Wl,-rpath-link,/opt/target-sysroot/usr/lib/aarch64-linux-gnu -C link-arg=-Wl,-rpath-link,/opt/target-sysroot/lib/aarch64-linux-gnu"
WORKDIR /src
