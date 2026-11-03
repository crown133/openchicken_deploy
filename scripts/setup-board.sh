#!/bin/sh
# OpenChicken Z3W-A1 board-interface configuration.
# Adapted from OpenChicken daemon-v0.17.8, commit 942e58e3aa6dbb28e7037203fff11866122c7567.
# The complete official setup source is preserved in provenance/upstream/setup-board.sh.
# --plan previews configuration; --apply changes the board only when explicitly invoked.
# Software dependencies, audio codec setup and motion deployment are separate phases.
set -eu

MOTOR_PORT=/dev/openchicken-dxl
ENV_TXT="${OPENCHICKEN_ENV_TXT:-/boot/armbianEnv.txt}"
CMDLINE="${CMDLINE:-/proc/cmdline}"
SELF=/usr/local/share/openchicken/setup-board.sh
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
HAT_DIR="$SCRIPT_DIR/../deploy/openchicken"
[ -d "$HAT_DIR" ] || HAT_DIR=/usr/local/share/openchicken/hat
EXTENSION="${OPENCHICKEN_EXT:-}"
needs_reboot=0

say()  { printf '\033[1m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }

run_hat() {
    mode="$1"
    set -- "$HAT_DIR/configure_hat.py" "$mode" --env "$ENV_TXT"
    if [ -n "$EXTENSION" ]; then
        set -- "$@" --extension "$EXTENSION"
    fi
    if [ -n "${OPENCHICKEN_DTB:-}" ]; then
        set -- "$@" --dtb "$OPENCHICKEN_DTB"
    fi
    python3 "$@"
}

persist_self() {
    # Preserve local sidecars across reboot; all remain non-executable source files.
    destination=/usr/local/share/openchicken/hat
    mkdir -p "$destination"
    for file in configure_hat.py configure_audio.py 99-openchicken-hat.rules \
        openchicken-z3w-a1.dts openchicken-z3w-a1-ext-spi3.dts \
        openchicken-z3w-a1-ext-uart9.dts; do
        if [ "$(readlink -f "$HAT_DIR/$file")" != "$(readlink -f "$destination/$file")" ]; then
            install -m 0644 "$HAT_DIR/$file" "$destination/$file"
        fi
    done
    mkdir -p /usr/local/share/openchicken/audio
    for file in aic3104-i2c4.dts aic3104-speaker-init.sh aic3104-mic-init.sh openchicken-speaker-init.service; do
        source="$HAT_DIR/../audio/$file"
        target="/usr/local/share/openchicken/audio/$file"
        if [ "$(readlink -f "$source")" != "$(readlink -f "$target")" ]; then
            install -m 0644 "$source" "$target"
        fi
    done
    if [ "$(readlink -f "$0")" != "$(readlink -f "$SELF")" ]; then
        install -m 0644 "$0" "$SELF"
    fi
}

check_environment() {
    [ "$(id -u)" = 0 ] || die "--apply requires a root session"
    [ "$(uname -m)" = aarch64 ] || die "this profile targets aarch64 ZERO 3W"
    case "$(uname -r)" in
        6.1.*-vendor-rk35xx) ;;
        *) die "profile targets 6.1 vendor-rk35xx; review another kernel family first" ;;
    esac
    [ -f "$ENV_TXT" ] || die "missing $ENV_TXT"
    [ -f "$HAT_DIR/configure_hat.py" ] || die "the local deploy/openchicken sidecars are required"
    for tool in python3 dtc fdtget fdtoverlay udevadm modprobe install; do
        command -v "$tool" >/dev/null 2>&1 || die "missing prerequisite: $tool (see adaptation README)"
    done
    getent group i2c >/dev/null || die "missing i2c group; install the reviewed i2c-tools prerequisite"
    getent group dialout >/dev/null || die "missing dialout group"
    for unit in robotd.service padd.service netpadd.service; do
        if systemctl is-active --quiet "$unit"; then
            die "$unit is running; interface reconfiguration requires a separate maintenance step"
        fi
    done
}

configure_overlay() {
    status=0
    run_hat --apply || status=$?
    case "$status" in
        0) say "OpenChicken boot environment already current" ;;
        10) needs_reboot=1 ;;
        *) die "OpenChicken DTB/overlay validation or application failed ($status)" ;;
    esac
}

free_motor_port() {
    # UART2 on Pin8/10 stays a debug console. Only an OF-verified UART5 may be freed.
    if [ ! -e "$MOTOR_PORT" ]; then
        say "UART5 stable alias absent; validate mapping after the overlay reboot"
        return 0
    fi
    path=$(udevadm info --query=path --name="$MOTOR_PORT")
    case "$path" in
        */fe690000.serial/*) ;;
        *) die "$MOTOR_PORT does not map to the UART5 controller: $path" ;;
    esac
    tty=$(basename "$(readlink -f "$MOTOR_PORT")")
    for arg in $(cat "$CMDLINE"); do
        case "$arg" in
            console="$tty"|console="$tty",*)
                die "UART5 is a running kernel console; review that explicit console argument" ;;
        esac
    done
    unit="serial-getty@${tty}.service"
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    systemctl mask "$unit" >/dev/null 2>&1 || die "could not mask $unit"
    say "UART5 getty disabled; UART2 getty and console= are unchanged"
}

configure_audio() {
    say "Speaker setup has a separate entry: sh scripts/setup-audio.sh --plan|--apply|--verify"
    say "An existing aic3104-i2c4 overlay is preserved; this entry does not enable audio itself"
    return 0
}

configure_tof() {
    # configure_hat.py owns both aliases, by the I2C4 controller's OF identity.
    # Do not re-install the upstream fe5c0000/i2c3 rule over this one.
    say "HAT I2C: /dev/i2c-openchicken and /dev/i2c-pihat follow I2C4, not adapter number"
    return 0
}

report() {
    say "OpenChicken Z3W-A1 environment status"
    mapping_status=0
    run_hat --verify || mapping_status=$?
    configure_audio
    say "I2C4 interface prepared; IMU communication is deferred to a later step"
    if [ "$needs_reboot" = 1 ]; then
        say "boot configuration changed; an operator must schedule the reboot"
        say "after reboot, re-run the same reviewed --apply command (persistent source: $SELF)"
    elif [ "$mapping_status" != 0 ]; then
        die "live UART/I2C/PWM mappings are not verified; inspect the report above"
    else
        say "interface configuration prepared; physical HAT tests and runtime adaptation remain"
    fi
}

main() {
    case "${1:-}" in
        --plan)
            [ "$#" = 1 ] || die "usage: sh setup-board.sh --plan|--apply"
            run_hat --plan
            return 0 ;;
        --apply)
            [ "$#" = 1 ] || die "usage: sh setup-board.sh --plan|--apply" ;;
        *)
            say "usage: sh setup-board.sh --plan|--apply"
            say "--plan validates the actual DTB and prints a diff without changing the board"
            return 0 ;;
    esac
    check_environment
    # Validate the complete proposed overlay chain before persisting any system files.
    run_hat --plan
    persist_self
    configure_overlay
    free_motor_port
    configure_tof
    report
}

main "$@"
