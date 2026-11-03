#!/usr/bin/env python3
"""Read-only preflight for the pinned OpenChicken standing pose, excluding mouth ID34.

No torque, goal, profile, EEPROM or reboot writes. No board files are generated.
"""
import importlib.util
import json
import math
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import termios
import time

spec = importlib.util.spec_from_file_location('dxl_diagnostic',
                                             Path(__file__).with_name('test-dxl-communication.py'))
dxl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dxl)
POSE = {10: 0.0, 11: 0.0873, 12: 0.4579, 13: 0.0049, 14: -0.4530,
        20: 0.0, 21: -0.0873, 22: -0.4579, 23: -0.0049, 24: 0.4530,
        30: 0.3491, 31: 0.3491, 32: 0.0, 33: 0.0}
READS = ((0, 14), (20, 4), (31, 5), (38, 2), (44, 12),
         (62, 1), (63, 2), (68, 3), (80, 6), (108, 12), (124, 12), (144, 3))


def raw_target(radians):
    # Matches rustypot conversion documented in openchicken-control/src/bus.rs.
    return int(4096 * (math.pi + radians) / (2 * math.pi))


def read_packet(device_id, address, size):
    if device_id not in POSE or (address, size) not in READS:
        raise ValueError('outside standing preflight read scope')
    body = b'\x02' + struct.pack('<HH', address, size)
    body = body.replace(b'\xff\xff\xfd', b'\xff\xff\xfd\xfd')
    data = dxl.HEADER + bytes([device_id]) + struct.pack('<H', len(body) + 2) + body
    return data + struct.pack('<H', dxl.crc16(data))


def read(fd, device_id, address, size, stats):
    data = read_packet(device_id, address, size)
    termios.tcflush(fd, termios.TCIFLUSH)
    sent, deadline = 0, time.monotonic() + 0.2
    while sent < len(data):
        if not select.select([], [fd], [], max(0, deadline-time.monotonic()))[1]:
            raise TimeoutError('transmit timeout')
        sent += os.write(fd, data[sent:])
    termios.tcdrain(fd)
    stats['requests'] += 1
    buffer, deadline = bytearray(), time.monotonic() + 0.2
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline-time.monotonic()))[0]:
            break
        buffer.extend(os.read(fd, 512))
        while True:
            frame = dxl.take_frame(buffer, stats)
            if frame is None:
                break
            returned_id, body = frame
            if returned_id != device_id or not body or body[0] != 0x55:
                continue
            if len(body) < 2 or body[1] & 0x7f or len(body[2:]) != size:
                raise ValueError(f'invalid status for ID{device_id}: {body.hex()}')
            return body[2:]
    raise TimeoutError(f'no valid response from ID{device_id}, address{address}')


def decode(device_id, blocks):
    identity, limits = blocks[0], blocks[44]
    target = raw_target(POSE[device_id])
    row = {'id': device_id, 'joint': dxl.JOINTS[device_id],
           'model': int.from_bytes(identity[:2], 'little'), 'configured_id': identity[7],
           'baud': identity[8], 'return_delay': identity[9], 'drive_mode': identity[10],
           'operating_mode': identity[11], 'secondary_id': identity[12], 'protocol': identity[13],
           'homing_offset': int.from_bytes(blocks[20], 'little', signed=True),
           'current_limit': int.from_bytes(blocks[38], 'little'),
           'velocity_limit': int.from_bytes(limits[:4], 'little'),
           'maximum_position': int.from_bytes(limits[4:8], 'little'),
           'minimum_position': int.from_bytes(limits[8:12], 'little'),
           'shutdown': blocks[63][0], 'torque': blocks[63][1],
           'pwm_slope': blocks[62][0],
           'position_d_gain': int.from_bytes(blocks[80][:2], 'little'),
           'position_i_gain': int.from_bytes(blocks[80][2:4], 'little'),
           'position_p_gain': int.from_bytes(blocks[80][4:6], 'little'),
           'status_return_level': blocks[68][0], 'registered_instruction': blocks[68][1],
           'hardware_error': blocks[68][2],
           'profile_acceleration': int.from_bytes(blocks[108][:4], 'little'),
           'profile_velocity': int.from_bytes(blocks[108][4:8], 'little'),
           'existing_goal': int.from_bytes(blocks[108][8:12], 'little', signed=True),
           'present_current': int.from_bytes(blocks[124][2:4], 'little', signed=True),
           'present_velocity': int.from_bytes(blocks[124][4:8], 'little', signed=True),
           'present_position': int.from_bytes(blocks[124][8:12], 'little', signed=True),
           'voltage_v': int.from_bytes(blocks[144][:2], 'little') / 10,
           'temperature_c': blocks[144][2], 'target_radians': POSE[device_id], 'target_raw': target}
    row.update(dxl.decode_limits(blocks[31]))
    blockers = []
    for bad, reason in ((row['model'] != 1200 or row['configured_id'] != device_id, 'identity mismatch'),
                        (row['baud'] != 3 or row['protocol'] != 2, 'bus configuration mismatch'),
                        (row['operating_mode'] != 3, 'requires position mode 3'),
                        (row['torque'] != 0, 'torque already on'),
                        (row['drive_mode'] & 0x08 != 0, 'goal update auto-enables torque'),
                        (row['homing_offset'] != (-512 if device_id in (13, 23) else 0), 'homing offset differs from original'),
                        (row['pwm_slope'] != 255, 'PWM slope differs from original'),
                        ((row['position_p_gain'], row['position_i_gain'], row['position_d_gain']) != (200, 0, 0), 'init PID differs from original'),
                        (not row['minimum_position'] <= target <= row['maximum_position'], 'target outside position limits'),
                        (row['hardware_error'] & ~0x01 != 0, 'non-voltage hardware fault active'),
                        (row['voltage_v'] <= 0, 'voltage reading invalid'),
                        (row['temperature_c'] >= row['temperature_limit_c'], 'temperature limit reached')):
        if bad:
            blockers.append(reason)
    row['blockers'] = blockers
    row['input_voltage_alarm_ignored_by_user_request'] = bool(row['hardware_error'] & 0x01)
    return row


def read_all(fd, stats):
    return [decode(i, {address: read(fd, i, address, size, stats) for address, size in READS}) for i in POSE]


def main():
    state = subprocess.run(['systemctl', 'is-active', 'robotd.service', 'padd.service',
                            'netpadd.service'], capture_output=True, text=True)
    if any(s in ('active', 'activating', 'deactivating') for s in state.stdout.splitlines()):
        raise ValueError('robot services active; refusing shared access')
    stats = dict.fromkeys(('requests', 'crc_errors', 'invalid_lengths'), 0)
    with dxl.serial_port() as fd:
        rows = read_all(fd, stats)
    print('RESULT_JSON=' + json.dumps({'purpose': 'read-only standing preparation',
          'ready_for_enable': all(not r['blockers'] for r in rows),
          'torque_or_motion_commands_sent': False, 'statistics': stats,
          'results': rows}, separators=(',', ':')), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
