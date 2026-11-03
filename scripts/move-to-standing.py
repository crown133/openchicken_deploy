#!/usr/bin/env python3
"""Fixed-robot bench test: explicitly enable 14 servos and approach original standing pose.

Default --plan is read-only. Motion requires BOTH --enable-and-stand and --robot-fixed.
Input-voltage alert bit0 is allowed only per the user's explicit 2S supply decision.
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
import termios
import time

spec = importlib.util.spec_from_file_location('standing_plan', Path(__file__).with_name('plan-standing-pose.py'))
plan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan)
dxl = plan.dxl


def frame(device_id, instruction, body):
    body = (bytes([instruction]) + body).replace(b'\xff\xff\xfd', b'\xff\xff\xfd\xfd')
    data = dxl.HEADER + bytes([device_id]) + struct.pack('<H', len(body)+2) + body
    return data + struct.pack('<H', dxl.crc16(data))


def write_packet(device_id, address, data, row):
    if device_id not in plan.POSE or row['id'] != device_id:
        raise ValueError('unexpected actuator ID')
    profiles = struct.pack('<II', 2, 10)
    original = struct.pack('<II', row['profile_acceleration'], row['profile_velocity'])
    allowed = ((address == 64 and data in (b'\x00', b'\x01')) or
               (address == 108 and data in (profiles, original)) or
               (address == 116 and data == struct.pack('<i', row['present_position'])))
    if not allowed:
        raise ValueError('write outside torque/limited-profile/current-position preparation')
    return frame(device_id, 3, struct.pack('<H', address)+data)


def sync_packet(address):
    if address not in (64, 116):
        raise ValueError('only emergency torque-off or exact standing targets allowed')
    size = 1 if address == 64 else 4
    body = struct.pack('<HH', address, size)
    for i, angle in plan.POSE.items():
        body += bytes([i]) + (b'\x00' if address == 64 else struct.pack('<i', plan.raw_target(angle)))
    return frame(254, 0x83, body)


def send(fd, data):
    termios.tcflush(fd, termios.TCIFLUSH)
    sent, deadline = 0, time.monotonic()+0.2
    while sent < len(data):
        if not select.select([], [fd], [], max(0, deadline-time.monotonic()))[1]:
            raise TimeoutError('transmit timeout')
        sent += os.write(fd, data[sent:])
    termios.tcdrain(fd)


def check_services():
    state = subprocess.run(['systemctl', 'is-active', 'robotd.service', 'padd.service',
                            'netpadd.service'], capture_output=True, text=True)
    if any(s in ('active', 'activating', 'deactivating') for s in state.stdout.splitlines()):
        raise ValueError('robot services active; refusing shared motor control')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument('--plan', action='store_true')
    modes.add_argument('--enable-and-stand', action='store_true')
    modes.add_argument('--relax', action='store_true', help='torque off only, no movement target')
    parser.add_argument('--robot-fixed', action='store_true', help='operator has explicitly confirmed physical support')
    args = parser.parse_args()
    if args.enable_and_stand and not args.robot_fixed:
        parser.error('motion requires explicit --robot-fixed confirmation')
    check_services()
    if not args.enable_and_stand and not args.relax:
        return plan.main()
    stats = dict.fromkeys(('requests', 'crc_errors', 'invalid_lengths'), 0)
    with dxl.serial_port() as fd:
        if args.relax:
            send(fd, sync_packet(64))
            for i in plan.POSE:
                if plan.read(fd, i, 63, 2, stats)[1] != 0:
                    raise ValueError(f'ID{i} torque-off readback failed')
            print(f'All {len(plan.POSE)} selected torque states verified OFF.', flush=True)
            return
        rows = plan.read_all(fd, stats)
        blockers = {r['id']: r['blockers'] for r in rows if r['blockers']}
        if blockers:
            raise ValueError(f'preflight blocked: {blockers}')
        for r in rows:
            if r['drive_mode'] & 4 or not r['minimum_position'] <= r['present_position'] <= r['maximum_position']:
                raise ValueError(f'ID{r["id"]}: unsupported profile mode or current position outside hold range')
            if r['profile_acceleration'] > 32767 or r['profile_velocity'] > 32767:
                raise ValueError('unexpected existing profile values')
        completed, touched = False, []
        try:
            # Explicit low speed: about 13.74 deg/s, acceleration about 42.9 deg/s^2.
            for r in rows:
                i = r['id']
                send(fd, write_packet(i, 108, struct.pack('<II', 2, 10), r))
                touched.append(r)
                time.sleep(0.01)
                if plan.read(fd, i, 108, 12, stats)[:8] != struct.pack('<II', 2, 10):
                    raise ValueError(f'ID{i}: profile readback failed')
                send(fd, write_packet(i, 116, struct.pack('<i', r['present_position']), r))
                time.sleep(0.01)
                if plan.read(fd, i, 63, 2, stats)[1] != 0:
                    raise ValueError('unexpected torque-on during position preload')
            # All old goals have been replaced by current positions before any enable.
            for r in rows:
                i = r['id']
                send(fd, write_packet(i, 64, b'\x01', r))
                time.sleep(0.01)
                if plan.read(fd, i, 63, 2, stats)[1] != 1:
                    raise ValueError(f'ID{i}: torque-enable readback failed')
                current = int.from_bytes(plan.read(fd, i, 124, 12, stats)[8:12], 'little', signed=True)
                if abs(current-r['present_position']) > 64:
                    raise ValueError(f'ID{i}: unexpected movement while enabling hold')
            print(f'All {len(plan.POSE)} selected servos enabled at their current positions; starting limited-speed standing transition.', flush=True)
            send(fd, sync_packet(116))
            started, settled = time.monotonic(), 0
            while time.monotonic()-started < 25:
                check_services()
                positions = {}
                for r in rows:
                    i = r['id']
                    if plan.read(fd, i, 68, 3, stats)[2] & ~0x01:
                        raise ValueError(f'ID{i}: non-voltage hardware fault')
                    if plan.read(fd, i, 63, 2, stats)[1] != 1:
                        raise ValueError(f'ID{i}: torque unexpectedly off')
                    positions[i] = int.from_bytes(plan.read(fd, i, 124, 12, stats)[8:12], 'little', signed=True)
                error = max(abs(positions[r['id']]-r['target_raw']) for r in rows)
                print(f'{time.monotonic()-started:.1f}s maximum position error={error} counts', flush=True)
                settled = settled+1 if error <= 12 else 0
                if settled >= 3:
                    completed = True
                    print('Standing targets reached. Torque remains ON; robot is supported, not balancing autonomously.', flush=True)
                    break
                time.sleep(0.15)
            if not completed:
                raise TimeoutError('standing targets not reached within 25 seconds')
        finally:
            # On interruption/fault, request immediate torque-off before any cleanup reads.
            if not completed:
                try:
                    send(fd, sync_packet(64))
                    print(f'Abort: torque-off requested for all {len(plan.POSE)} selected servos.', flush=True)
                except OSError as error:
                    print(f'Torque-off send failed: {error}; use physical power disconnect.', file=sys.stderr)
            for r in touched:
                try:
                    original = struct.pack('<II', r['profile_acceleration'], r['profile_velocity'])
                    send(fd, write_packet(r['id'], 108, original, r))
                    time.sleep(0.01)
                    if plan.read(fd, r['id'], 108, 12, stats)[:8] != original:
                        raise ValueError('profile restoration mismatch')
                except (OSError, ValueError) as error:
                    print(f'ID{r["id"]} profile restore failed: {error}', file=sys.stderr)


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGHUP, interrupted)
    try:
        main()
    except KeyboardInterrupt:
        print('Interrupted by operator.', file=sys.stderr)
        sys.exit(130)
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
