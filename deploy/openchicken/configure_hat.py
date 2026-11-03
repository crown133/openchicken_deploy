#!/usr/bin/env python3
"""Plan or apply the Z3W-A1 environment; never start a daemon or send bus traffic."""
import argparse
import difflib
import grp
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
from datetime import datetime, timezone

HERE = Path(__file__).resolve().parent
OVERLAY = 'openchicken-z3w-a1'
AUDIO_OVERLAY = 'aic3104-i2c4'
LEGACY_RULE = Path('/etc/udev/rules.d/99-robot-i2c-pihat.rules')
OLD_HAT = {'i2c3-pihat', 'aic3104-i2c3', 'i2c-gpio-pihat', 'aic3104-pihat'}
OWN_OVERLAYS = {OVERLAY, OVERLAY + '-ext-spi3', OVERLAY + '-ext-uart9', AUDIO_OVERLAY}
SYMBOLS = {
    'uart5': '/serial@fe690000', 'i2c4': '/i2c@fe5d0000',
    'pwm8': '/pwm@fe6f0000', 'pwm9': '/pwm@fe6f0010',
    'i2s3_2ch': '/i2s@fe430000',
}


def env_values(text):
    values = {}
    for line in text.splitlines():
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key in values:
            raise ValueError(f'duplicate boot setting: {key}')
        values[key] = value
    return values


def short_overlay(word):
    for prefix in ('rk3568-', 'rk35xx-'):
        if word.startswith(prefix):
            return word[len(prefix):]
    return word


def select_extension(requested, text):
    if requested is not None:
        return requested
    words = env_values(text).get('user_overlays', '').split()
    modes = [mode for mode in ('spi3', 'uart9') if OVERLAY + '-ext-' + mode in words]
    if len(modes) > 1:
        raise ValueError('both J7 modes are configured; select one explicitly')
    return modes[0] if modes else 'none'


def select_audio(requested, text):
    if requested is not None:
        return requested
    words = env_values(text).get('user_overlays', '').split()
    return 'external-12mhz' if AUDIO_OVERLAY in words else 'off'


def plan_environment(text, extension='none', audio=None):
    if extension not in ('none', 'spi3', 'uart9'):
        raise ValueError('J7 must be exactly one of none, spi3, uart9')
    values = env_values(text)
    audio = select_audio(audio, text)
    if audio not in ('off', 'external-12mhz'):
        raise ValueError('audio must be off or external-12mhz')
    updated = {}
    for key in ('overlays', 'user_overlays'):
        words = values.get(key, '').split()
        kept = []
        for word in words:
            name = short_overlay(word)
            if name in OLD_HAT or name in OWN_OVERLAYS:
                continue
            # Existing ownership must be reviewed rather than silently stolen.
            if name.startswith(('uart4', 'i2c5', 'spi3', 'uart9', 'usb2-host2', 'usb2_host2')):
                raise ValueError(f'conflicting overlay needs review: {word}')
            if name.startswith(('uart5-', 'i2c4-', 'pwm8-', 'pwm9-', 'i2s3-')):
                raise ValueError(f'overlapping controller overlay needs review: {word}')
            kept.append(word)
        updated[key] = kept
    updated['user_overlays'].append(OVERLAY)
    if extension != 'none':
        updated['user_overlays'].append(OVERLAY + '-ext-' + extension)
    if audio == 'external-12mhz':
        updated['user_overlays'].append(AUDIO_OVERLAY)
    lines = []
    seen = set()
    for line in text.splitlines():
        key = line.split('=', 1)[0]
        if key in updated:
            lines.append(key + '=' + ' '.join(updated[key]))
            seen.add(key)
        else:
            lines.append(line)
    for key in ('overlays', 'user_overlays'):
        if key not in seen and updated[key]:
            lines.append(key + '=' + ' '.join(updated[key]))
    return '\n'.join(lines) + '\n'


def plan_legacy_rule(text):
    lines = []
    for line in text.splitlines(True):
        if line.lstrip().startswith('#'):
            lines.append(line)
            continue
        match = re.search(r'SYMLINK\s*\+=\s*"([^"]*)"', line)
        if not match or 'i2c-pihat' not in match[1].split():
            lines.append(line)
            continue
        if not any(marker in line for marker in ('fe5c0000.i2c', 'i2c-gpio-pihat')):
            raise ValueError('custom i2c-pihat rule needs manual review: ' + line.strip())
        aliases = [word for word in match[1].split() if word != 'i2c-pihat']
        if aliases:
            lines.append(line[:match.start(1)] + ' '.join(aliases) + line[match.end(1):])
        else:
            lines.append('# Superseded by OpenChicken I2C4: ' + line)
    return ''.join(lines)


def run(*args):
    return subprocess.check_output(args, text=True, stderr=subprocess.PIPE).strip()


def validate_and_compile(dtb, new_env, extension, work, user_dir):
    """Check actual board symbols and the complete configured overlay chain before writing."""
    compatible = run('fdtget', '-t', 's', str(dtb), '/', 'compatible')
    if 'rockchip,rk3566' not in compatible or 'radxa,zero' not in compatible:
        raise ValueError('selected DTB is not a Radxa RK3566 ZERO board')
    required = dict(SYMBOLS)
    if extension != 'none':
        required.update(spi3='/spi@fe640000', uart9='/serial@fe6d0000')
    for symbol, expected in required.items():
        actual = run('fdtget', '-t', 's', str(dtb), '/__symbols__', symbol)
        if actual != expected:
            raise ValueError(f'{symbol}: {actual!r}, expected {expected!r}')
    # These symbols are needed by the custom pin groups and the UART4 exclusion.
    for symbol in ('pinctrl', 'uart4'):
        run('fdtget', '-t', 's', str(dtb), '/__symbols__', symbol)

    names = [OVERLAY]
    if extension != 'none':
        names.append(OVERLAY + '-ext-' + extension)
    if AUDIO_OVERLAY in env_values(new_env).get('user_overlays', '').split():
        names.append(AUDIO_OVERLAY)
    compiled = {}
    for name in names:
        output = work / (name + '.dtbo')
        source = (HERE.parent / 'audio' if name == AUDIO_OVERLAY else HERE) / (name + '.dts')
        run('dtc', '-@', '-I', 'dts', '-O', 'dtb', '-o', str(output), str(source))
        compiled[name] = output
    values = env_values(new_env)
    chain = []
    prefix = values.get('overlay_prefix', '')
    for word in values.get('overlays', '').split():
        filename = (prefix + '-' if prefix else '') + word + '.dtbo'
        chain.append(dtb.parent / 'overlay' / filename)
    for word in values.get('user_overlays', '').split():
        chain.append(compiled.get(word, user_dir / (word + '.dtbo')))
    for file in chain:
        if not file.is_file():
            raise ValueError(f'configured overlay is missing: {file}')
    applied = work / 'combined.dtb'
    run('fdtoverlay', '-i', str(dtb), '-o', str(applied), *(str(p) for p in chain))
    # A later overlay must not redirect one of our controllers behind our back.
    for symbol, node in SYMBOLS.items():
        if run('fdtget', '-t', 's', str(applied), node, 'status') != 'okay':
            raise ValueError(f'{symbol} is not enabled in the combined device tree')
    if run('fdtget', '-t', 's', str(applied), '/serial@fe680000', 'status') != 'disabled':
        raise ValueError('UART4 must be disabled while PWM8/9 own Pin16/18')
    if AUDIO_OVERLAY in compiled:
        codec = SYMBOLS['i2c4'] + '/codec@18'
        if run('fdtget', '-t', 's', str(applied), codec, 'compatible') != 'ti,tlv320aic3x':
            raise ValueError('I2C4 codec binding does not match the selected driver')
        if run('fdtget', '-t', 'x', str(applied), codec, 'reg') != '18':
            raise ValueError('codec must use I2C address 0x18')
        if run('fdtget', '-t', 'u', str(applied), '/aic3104-mclk', 'clock-frequency') != '12000000':
            raise ValueError('codec MCLK must match the confirmed external 12 MHz oscillator')
        if run('fdtget', '-t', 's', str(applied), '/sound-aic3104', 'simple-audio-card,name') != 'aic3104':
            raise ValueError('unexpected ALSA card name')
    return compiled


def controller_for(entry):
    """Find the OF node in device ancestry; numbering and gpiochip offsets are not identities."""
    device = (entry / 'device').resolve()
    for parent in (device, *device.parents):
        node = parent / 'of_node'
        if node.exists():
            return node.resolve().name
    return None


def discover_devices(sys_root=Path('/sys'), dev_root=Path('/dev')):
    expected = {
        'serial@fe690000': ('dxl_uart5', 'tty'),
        'i2c@fe5d0000': ('hat_i2c4', 'i2c-dev'),
        'pwm@fe6f0000': ('pwm8_pin16', 'pwm'),
        'pwm@fe6f0010': ('pwm9_pin18', 'pwm'),
        'serial@fe6d0000': ('extension_uart9', 'tty'),
        'spi@fe640000': ('extension_spi3', 'spi_master'),
    }
    result = {name: None for name, _ in expected.values()}
    for node, (name, subsystem) in expected.items():
        for entry in sorted((sys_root / 'class' / subsystem).glob('*')):
            if controller_for(entry) == node:
                result[name] = str(dev_root / entry.name) if subsystem in ('tty', 'i2c-dev') else str(entry)
                break
    return result


def write_changed(path, data, backup):
    old = path.read_bytes() if path.exists() else None
    if old == data:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    if old is not None:
        saved = backup / path.relative_to('/')
        saved.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, saved)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
        temp = Path(f.name)
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    temp.chmod(mode)
    os.replace(temp, path)
    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true', help='validate and print the proposed boot diff only')
    modes.add_argument('--apply', action='store_true', help='write validated board environment as root')
    modes.add_argument('--verify', action='store_true', help='only report live OF-based device mapping')
    parser.add_argument('--env', type=Path, default=Path('/boot/armbianEnv.txt'))
    parser.add_argument('--dtb', type=Path)
    parser.add_argument('--extension', choices=['none', 'spi3', 'uart9'])
    parser.add_argument('--audio', choices=['off', 'external-12mhz'],
                        help='enable the confirmed external 12 MHz codec; preserve existing mode if omitted')
    args = parser.parse_args()
    old = args.env.read_text() if args.env.exists() else ''
    args.extension = select_extension(args.extension, old)
    args.audio = select_audio(args.audio, old)
    if args.verify:
        devices = discover_devices()
        aliases = {'/dev/openchicken-dxl': devices['dxl_uart5'],
                   '/dev/i2c-openchicken': devices['hat_i2c4']}
        if args.extension == 'uart9':
            aliases['/dev/openchicken-ext-uart9'] = devices['extension_uart9']
        aliases_ok = all(target is not None and Path(alias).exists()
                         and Path(alias).resolve() == Path(target).resolve()
                         for alias, target in aliases.items())
        extension_ok = (args.extension != 'spi3' or
                        bool(devices['extension_spi3']) and not devices['extension_uart9'])
        extension_ok &= args.extension != 'uart9' or not devices['extension_spi3']
        print(json.dumps({'devices': devices, 'aliases_match_of_nodes': aliases_ok,
                          'extension_mode': args.extension, 'extension_matches': extension_ok,
                          'audio_mode': args.audio,
                          'motion_ready': False, 'imu_communication': 'deferred'}, indent=2))
        return 0 if aliases_ok and extension_ok and all(
            devices[k] for k in ('pwm8_pin16', 'pwm9_pin18')) else 1

    if not args.env.is_file():
        raise ValueError(f'boot environment not found: {args.env}')
    new = plan_environment(old, args.extension, args.audio)
    legacy_rule = LEGACY_RULE
    legacy_text = legacy_rule.read_text() if legacy_rule.exists() else ''
    new_legacy_text = plan_legacy_rule(legacy_text)
    disable_legacy = new_legacy_text != legacy_text
    values = env_values(old)
    dtb = args.dtb
    if dtb is None:
        fdtfile = values.get('fdtfile')
        if not fdtfile or Path(fdtfile).is_absolute() or '..' in Path(fdtfile).parts:
            raise ValueError('pass --dtb /boot/dtb/rockchip/<actual-board>.dtb or set a valid fdtfile')
        dtb = Path('/boot/dtb') / fdtfile
    if not dtb.is_file():
        raise ValueError(f'base DTB not found: {dtb}')
    # Keep the symlinked /boot/dtb layout when choosing the paired overlay directory.
    user_dir = args.env.parent / 'overlay-user'
    for tool in ('dtc', 'fdtget', 'fdtoverlay'):
        if not shutil.which(tool):
            raise ValueError(f'{tool} is required (device-tree-compiler package)')
    with tempfile.TemporaryDirectory(prefix='openchicken-hat-') as tmp:
        compiled = validate_and_compile(dtb, new, args.extension, Path(tmp), user_dir)
        print(''.join(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                         fromfile=str(args.env), tofile=str(args.env) + '.proposed')))
        print('Overlay preflight passed; UART2 console and FUSB302 nodes are unchanged by this adaptation.')
        if disable_legacy:
            print(f'Also supersede the legacy I2C3 alias rule, with backup: {legacy_rule}')
        if not args.apply:
            print('Plan only. No boot, udev or system configuration was written.')
            return 0
        if os.geteuid() != 0:
            raise ValueError('--apply requires root')
        for group in ('i2c', 'dialout'):
            grp.getgrnam(group)
        backup = Path('/var/lib/openchicken/backups') / datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        boot_changed = False
        for name, file in compiled.items():
            boot_changed |= write_changed(user_dir / (name + '.dtbo'), file.read_bytes(), backup)
        write_changed(Path('/etc/udev/rules.d/99-openchicken-hat.rules'),
                      (HERE / '99-openchicken-hat.rules').read_bytes(), backup)
        if disable_legacy:
            write_changed(legacy_rule, new_legacy_text.encode(), backup)
        write_changed(Path('/etc/modules-load.d/openchicken-i2c.conf'), b'i2c-dev\n', backup)
        # Boot settings are the last write: no reboot can select an uninstalled overlay.
        boot_changed |= write_changed(args.env, new.encode(), backup)
        run('modprobe', 'i2c-dev')
        run('udevadm', 'control', '--reload-rules')
        run('udevadm', 'trigger', '--subsystem-match=i2c-dev')
        run('udevadm', 'trigger', '--subsystem-match=tty')
        run('udevadm', 'settle', '--timeout=10')
        print(f'Environment applied; backups of changed existing files: {backup}')
        return 10 if boot_changed else 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (ValueError, KeyError, OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, 'stderr', None)
        print(f'error: {exc}' + (f'\n{detail}' if detail else ''), file=__import__('sys').stderr)
        raise SystemExit(1)
