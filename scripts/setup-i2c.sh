#!/bin/sh
# Prepare the HAT I2C development tools and one user's group membership.
# No I2C scan, peripheral register access, motor command or kernel upgrade.
set -eu
mode="${1:-}"
user="${2:-}"
die() { printf '%s\n' "$*" >&2; exit 1; }
case "$mode" in --plan|--apply) ;; *) die 'usage: sh scripts/setup-i2c.sh --plan|--apply USER' ;; esac
[ "$#" = 2 ] || die 'exactly one Linux username is required'
case "$user" in ''|-*|*[!a-zA-Z0-9_.-]*) die 'invalid username' ;; esac
id "$user" >/dev/null || die "unknown user: $user"
installed() { dpkg-query -W -f='${Status}' i2c-tools 2>/dev/null | grep -qx 'install ok installed'; }
member() { id -nG "$user" | tr ' ' '\n' | grep -Fxq i2c; }
if [ "$mode" = --plan ]; then
    if installed; then printf '%s\n' 'i2c-tools already installed'
    else apt-get --simulate install --no-install-recommends i2c-tools
    fi
    getent group i2c || true
    id "$user"
    printf '%s\n' "Apply will add $user to i2c if needed and reload i2c-dev udev rules. No bus transactions."
    exit 0
fi
[ "$(id -u)" = 0 ] || die '--apply requires an existing root session'
[ "$(uname -m)" = aarch64 ] || die 'apply this board profile on the aarch64 target'
[ -f /etc/udev/rules.d/99-openchicken-hat.rules ] || die 'install the reviewed HAT udev rules first'
if ! installed; then
    apt-get install -y --no-install-recommends i2c-tools
fi
getent group i2c >/dev/null || groupadd --system i2c
if ! member; then
    backup="/var/lib/openchicken/backups/$(date -u +%Y%m%dT%H%M%S)-i2c"
    mkdir -p "$backup/etc"
    chmod 0700 "$backup" "$backup/etc"
    cp -p /etc/group "$backup/etc/group"
    if [ -f /etc/gshadow ]; then cp -p /etc/gshadow "$backup/etc/gshadow"; fi
    usermod -a -G i2c "$user"
    printf '%s\n' "Group database backup: $backup"
fi
modprobe i2c-dev
udevadm control --reload-rules
udevadm trigger --subsystem-match=i2c-dev
udevadm settle --timeout=10
id "$user"
printf '%s\n' 'I2C tools and access prepared; use a new login session for updated groups. No bus scan performed.'
