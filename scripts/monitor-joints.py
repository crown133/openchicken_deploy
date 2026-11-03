#!/usr/bin/env python3
"""Read-only 50 ms joint/voltage display; zero degrees is feedback count 2048.

Run on the board, or use --ssh openchicken1@192.168.3.181 from this workspace.
The SSH mode streams this script without installing files on the board.
"""
import argparse
import importlib.util
import os
from pathlib import Path
import select
import signal
import struct
import subprocess
import sys
import time

PERIOD = 0.05


def load_diagnostic():
    spec = importlib.util.spec_from_file_location(
        'monitor_dxl', Path(__file__).with_name('test-dxl-communication.py'))
    dxl = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(dxl)
    # This private module instance permits only the feedback block, never writes.
    dxl.READS = {(132, 15)}
    return dxl


def decode(data):
    if len(data) != 15:
        raise ValueError('incomplete position/voltage feedback')
    position = int.from_bytes(data[:4], 'little', signed=True)
    voltage = int.from_bytes(data[12:14], 'little') / 10.0
    return position, (position - 2048) * 360.0 / 4096, voltage


def read_feedback(dxl, fd, device_id, stats):
    packet = dxl.packet(device_id, 2, struct.pack('<HH', 132, 15))
    deadline = time.monotonic() + 0.15
    sent = 0
    while sent < len(packet):
        if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
            raise TimeoutError('serial transmit timeout')
        sent += os.write(fd, packet[sent:])
    stats['requests'] += 1
    # Waiting for the reply also waits for transmission; tcdrain adds UART latency.
    buffer = bytearray()
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        buffer.extend(os.read(fd, 512))
        while True:
            parsed = dxl.take_frame(buffer, stats)
            if parsed is None:
                break
            returned_id, body = parsed
            if not body or body[0] != 0x55:
                stats['echo_or_instruction_frames'] += 1
                continue
            if returned_id != device_id:
                stats['unexpected_id_frames'] += 1
                continue
            if len(body) != 17 or body[1] & 0x7f:
                raise ValueError(f'ID{device_id}: invalid feedback status')
            stats['status_packets'] += 1
            return body[2:], bool(body[1] & 0x80)
    stats['timeouts'] += 1
    raise TimeoutError(f'ID{device_id}: no valid feedback')


def sample(dxl, fd, stats):
    rows = []
    for device_id, joint in dxl.JOINTS.items():
        data, alert = read_feedback(dxl, fd, device_id, stats)
        rows.append((device_id, joint, *decode(data), alert))
    return rows


def render(rows, cycle_ms, read_ms, stats):
    voltages = [row[4] for row in rows]
    cycle = f'{cycle_ms:.1f} ms' if cycle_ms is not None else 'first frame'
    lines = [
        'OpenChicken1 | READ ONLY | Ctrl-C to exit (torque unchanged)',
        f'Target: 50 ms | actual cycle: {cycle} | reads: {read_ms:.1f} ms',
        f'Servo supply: {min(voltages):.1f} - {max(voltages):.1f} V (servo feedback)',
        'Angle = (feedback - 2048) * 360 / 4096; existing homing offsets included.',
        '',
        ' ID  JOINT                COUNTS    ANGLE(deg)  VOLTAGE  ALERT',
    ]
    for device_id, joint, position, angle, voltage, alert in rows:
        lines.append(f'{device_id:3d}  {joint:19s} {position:7d} {angle:+13.2f} '
                     f'{voltage:7.1f}V  {"YES" if alert else "-"}')
    lines += ['', f'CRC errors: {stats["crc_errors"]} | timeouts: {stats["timeouts"]}',
              'Sequential reads, not simultaneous. No samples saved.']
    return '\n'.join(lines)


def interrupted(signum, frame):
    raise KeyboardInterrupt


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ssh', metavar='USER@HOST', help='run over SSH without copying files')
    parser.add_argument('--frames', type=int, default=0, help='stop after N displays; default continuous')
    parser.add_argument('--terminal', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.frames < 0:
        parser.error('--frames must be nonnegative')
    if args.ssh:
        command = ('cd /home/openchicken1/openchicken_deploy/scripts && '
                   f'python3 -B -u - --frames {args.frames}')
        if sys.stdout.isatty():
            command += ' --terminal'
        try:
            return subprocess.run(
                ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8',
                 '-o', 'StrictHostKeyChecking=yes', '--', args.ssh, command],
                input=Path(__file__).read_bytes()).returncode
        finally:
            if sys.stdout.isatty():
                print('\033[?25h', end='', flush=True)

    state = subprocess.run(['systemctl', 'is-active', 'robotd.service', 'padd.service',
                            'netpadd.service'], capture_output=True, text=True)
    if any(s in ('active', 'activating', 'deactivating') for s in state.stdout.splitlines()):
        raise ValueError('robot services active; refusing shared actuator bus access')
    dxl = load_diagnostic()
    stats = dict.fromkeys(('requests', 'status_packets', 'crc_errors', 'invalid_lengths',
                          'echo_or_instruction_frames', 'unexpected_id_frames', 'timeouts'), 0)
    terminal = args.terminal or sys.stdout.isatty()
    previous, count = None, 0
    with dxl.serial_port() as fd:
        if terminal:
            print('\033[2J\033[H\033[?25l', end='', flush=True)
        try:
            while True:
                started = time.monotonic()
                rows = sample(dxl, fd, stats)
                read_ms = (time.monotonic() - started) * 1000
                cycle_ms = (started - previous) * 1000 if previous is not None else None
                previous = started
                display = render(rows, cycle_ms, read_ms, stats)
                print(('\033[H' if terminal else '') + display +
                      ('\033[J' if terminal else '\n'), flush=True)
                count += 1
                if args.frames and count >= args.frames:
                    return 0
                time.sleep(max(0, PERIOD - (time.monotonic() - started)))
        except (OSError, ValueError):
            # Stop instead of presenting old angles as live calibration feedback.
            if terminal:
                print('\033[2J\033[H', end='', flush=True)
            print('MONITOR STOPPED: feedback unavailable; previous values are stale.', flush=True)
            raise
        finally:
            if terminal:
                print('\033[?25h', end='', flush=True)


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print('\nMonitor stopped; no torque commands sent.', file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
