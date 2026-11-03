#!/usr/bin/env python3
"""Playback and MK1 setup for the owner's confirmed external 12 MHz AIC3104 clock."""
import argparse
from datetime import datetime, timezone
import difflib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys

import configure_hat as hat

HERE = Path(__file__).resolve().parent
AUDIO = HERE.parent / 'audio'
SHARE = Path('/usr/local/share/openchicken')
UNIT = 'openchicken-speaker-init.service'
MODULES = ('snd-soc-tlv320aic3x', 'snd-soc-tlv320aic3x-i2c')
DRIVER_FILES = ('Makefile', 'dkms.conf', 'tlv320aic3x.c', 'tlv320aic3x.h', 'tlv320aic3x-i2c.c')
DKMS_VERSION = '6.1-rkr5.1'


def call(*args, allowed=(0,)):
    result = subprocess.run([str(a) for a in args], check=False)
    if result.returncode not in allowed:
        raise ValueError(f'{args[0]} failed with exit status {result.returncode}')
    return result.returncode


def module_available(name):
    return bool(shutil.which('modinfo')) and subprocess.run(
        ['modinfo', name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def require_tools(*names):
    missing = [name for name in names if not shutil.which(name)]
    if missing:
        raise ValueError('missing prerequisites (no packages installed automatically): ' + ', '.join(missing))


def check_target():
    if os.geteuid() != 0:
        raise ValueError('this write mode requires an existing root session')
    if platform.machine() != 'aarch64' or not re.fullmatch(
            r'6\.1\..*-vendor-rk35xx', platform.release()):
        raise ValueError('this profile requires aarch64 and the 6.1 vendor-rk35xx kernel')
    require_tools('systemctl')
    for unit in ('robotd.service', 'padd.service', 'netpadd.service', 'aic3104-init.service'):
        if subprocess.run(['systemctl', 'is-active', '--quiet', unit]).returncode == 0:
            raise ValueError(f'{unit} is active; review maintenance before audio reconfiguration')


def kernel_headers(kernel):
    build = Path('/lib/modules') / kernel / 'build'
    release = build / 'include/config/kernel.release'
    if not release.is_file() or release.read_text().strip() != kernel:
        raise ValueError(f'exact running-kernel headers required at {build}; no kernel upgrade attempted')
    return build


def install_driver():
    check_target()
    require_tools('dkms', 'gcc', 'make', 'depmod', 'modinfo')
    kernel = platform.release()
    kernel_headers(kernel)
    source = Path('/usr/src') / ('aic3x-' + DKMS_VERSION)
    for name in DRIVER_FILES:
        target = source / name
        if target.exists() and target.read_bytes() != (AUDIO / 'aic3x-dkms' / name).read_bytes():
            raise ValueError(f'existing different DKMS source needs review: {target}')
    source.mkdir(parents=True, exist_ok=True)
    for name in DRIVER_FILES:
        target = source / name
        if not target.exists():
            shutil.copyfile(AUDIO / 'aic3x-dkms' / name, target)
            target.chmod(0o644)
    status = hat.run('dkms', 'status', '-m', 'aic3x', '-v', DKMS_VERSION)
    if not status:
        call('dkms', 'add', '-m', 'aic3x', '-v', DKMS_VERSION)
    call('dkms', 'install', '-m', 'aic3x', '-v', DKMS_VERSION, '-k', kernel)
    call('depmod', kernel)
    if not all(module_available(name) for name in MODULES):
        raise ValueError('DKMS finished but one or more codec modules are unavailable')
    print('Codec modules installed for the running kernel only. No module loaded, no sound played.')


def audio_status(sys_root=Path('/sys'), proc_root=Path('/proc')):
    cards = []
    for card in sorted((sys_root / 'class/sound').glob('card[0-9]*')):
        card_id = card / 'id'
        if card_id.is_file() and card_id.read_text().strip() == 'aic3104':
            if hat.controller_for(card) == 'sound-aic3104':
                cards.append(card.name)
    codecs = []
    for device in (sys_root / 'bus/i2c/devices').glob('*-0018'):
        node = device / 'of_node'
        if node.exists() and node.resolve().parent.name == 'i2c@fe5d0000':
            driver = device / 'driver'
            if driver.exists() and driver.resolve().name == 'tlv320aic3x':
                codecs.append(device.name)
    listing = proc_root / 'asound/cards'
    return {'alsa_cards': cards, 'i2c4_bound_codecs': codecs,
            'proc_asound_cards': listing.read_text() if listing.exists() else '',
            'playback_tested': False, 'microphone_setup': 'deferred'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    for mode in ('plan', 'apply', 'verify', 'install-driver'):
        modes.add_argument('--' + mode, action='store_true')
    parser.add_argument('--env', type=Path, default=Path('/boot/armbianEnv.txt'))
    parser.add_argument('--dtb', type=Path)
    args = parser.parse_args()
    if args.install_driver:
        install_driver()
        return 0
    if args.verify:
        status = audio_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0 if status['alsa_cards'] and status['i2c4_bound_codecs'] else 1

    helper_args = ['--env', str(args.env), '--audio', 'external-12mhz']
    if args.dtb:
        helper_args += ['--dtb', str(args.dtb)]
    if args.apply:
        check_target()
    require_tools('dtc', 'fdtget', 'fdtoverlay')
    # The complete preserved overlay chain is compiled and merged before any writes.
    call(sys.executable, HERE / 'configure_hat.py', '--plan', *helper_args)
    print('Audio clock: external codec MCLK 12000000 Hz (owner confirmed 2026-10-25).')
    print('Codec modules:', {name: module_available(name) for name in MODULES})
    unit_path = Path('/etc/systemd/system') / UNIT
    old_unit = unit_path.read_text() if unit_path.exists() else ''
    new_unit = (AUDIO / UNIT).read_text()
    print(''.join(difflib.unified_diff(old_unit.splitlines(True), new_unit.splitlines(True),
                                     fromfile=str(unit_path), tofile=str(unit_path) + '.proposed')))
    if not args.apply:
        print('Plan only. Driver build, system writes, mixer initialization and playback were not run.')
        return 0

    require_tools('amixer', 'aplay', 'modinfo', 'udevadm', 'modprobe')
    if not all(module_available(name) for name in MODULES):
        raise ValueError('codec modules missing; review --install-driver with exact kernel headers first')
    if subprocess.run(['systemctl', 'is-enabled', '--quiet', 'aic3104-init.service']).returncode == 0:
        raise ValueError('official mic+speaker init is enabled; review it before enabling OpenChicken audio init')
    for group in ('i2c', 'dialout'):
        hat.grp.getgrnam(group)
    backup = Path('/var/lib/openchicken/backups') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    # Persist all sources needed by the common board entry after reboot.
    for file in HERE.iterdir():
        if file.is_file() and file.suffix in ('.py', '.dts', '.rules'):
            hat.write_changed(SHARE / 'hat' / file.name, file.read_bytes(), backup)
    for name in ('aic3104-i2c4.dts', 'aic3104-speaker-init.sh', 'aic3104-mic-init.sh', UNIT):
        hat.write_changed(SHARE / 'audio' / name, (AUDIO / name).read_bytes(), backup)
    hat.write_changed(unit_path, new_unit.encode(), backup)
    call('systemctl', 'daemon-reload')
    # Enable for the operator's next reboot; do not start a service here.
    call('systemctl', 'enable', UNIT)
    result = call(sys.executable, HERE / 'configure_hat.py', '--apply', *helper_args, allowed=(0, 10))
    print(f'Speaker configuration installed; changed existing files backed up under {backup}.')
    print('Schedule a reboot, then run --verify and initialize audio before playback.'
          if result == 10 else 'No boot change; run --verify and initialize audio before playback.')
    print('No automatic reboot, playback, recording or robot service start.')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        print(f'error: {exc}', file=sys.stderr)
        raise SystemExit(1)
