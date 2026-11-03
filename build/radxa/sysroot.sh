#!/bin/sh
# Called only by the build Dockerfile. Extract ARM libraries; never execute ARM packages.
set -eu
mkdir -p /opt/target-sysroot /opt/target-debs
cd /opt/target-debs
packages="libgstreamer1.0-dev libgstreamer1.0-0
libgstreamer-plugins-base1.0-dev libgstreamer-plugins-base1.0-0
libgstreamer-plugins-bad1.0-dev libgstreamer-plugins-bad1.0-0
libglib2.0-dev libglib2.0-0
libpcre2-dev libpcre2-8-0 libffi-dev libffi8 liborc-0.4-dev liborc-0.4-0
zlib1g-dev zlib1g libmount-dev libmount1 libblkid-dev libblkid1
libselinux1-dev libselinux1 libsepol-dev libsepol2 libudev-dev libudev1
libunwind8 libunwind-dev libdw1 libdw-dev libelf1 libelf-dev
liblzma5 liblzma-dev libzstd1 libzstd-dev libbz2-1.0 libbz2-dev libcap2 libssl3"
set --
for package in $packages; do
    version=$(apt-cache policy "$package:arm64" | awk '/Candidate:/ {print $2}')
    [ -n "$version" ] && [ "$version" != '(none)' ] || { echo "No ARM64 package: $package" >&2; exit 1; }
    apt-cache show "$package:arm64=$version" >> package-metadata.txt
    set -- "$@" "$package:arm64=$version"
done
apt-get --print-uris download "$@" > download-uris.txt
apt-get download "$@"
sha256sum ./*.deb > SHA256SUMS
for deb in ./*.deb; do
    dpkg-deb -x "$deb" /opt/target-sysroot
done
PKG_CONFIG_SYSROOT_DIR=/opt/target-sysroot \
PKG_CONFIG_LIBDIR=/opt/target-sysroot/usr/lib/aarch64-linux-gnu/pkgconfig:/opt/target-sysroot/usr/share/pkgconfig \
    pkg-config --cflags --libs gstreamer-1.0 gstreamer-app-1.0 gstreamer-video-1.0 \
        gstreamer-webrtc-1.0 gstreamer-sdp-1.0 libudev
