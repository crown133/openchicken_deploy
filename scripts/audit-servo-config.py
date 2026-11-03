#!/usr/bin/env python3
"""Read-only configuration audit of 14 XL330s against pinned OpenChicken defaults.

No writes, automatic correction, torque enable, reboot, files or motion.
"""
import importlib.util
import json
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
READS = ((0, 64), (64, 56), (120, 28))
FIELDS = (
    ('model_number', 0, 2, False), ('firmware', 6, 1, False),
    ('id_register', 7, 1, False), ('baud_register', 8, 1, False),
    ('return_delay', 9, 1, False), ('drive_mode', 10, 1, False),
    ('operating_mode', 11, 1, False), ('secondary_id', 12, 1, False),
    ('protocol', 13, 1, False), ('homing_offset', 20, 4, True),
    ('moving_threshold', 24, 4, False), ('temperature_limit', 31, 1, False),
    ('max_voltage_limit_raw', 32, 2, False), ('min_voltage_limit_raw', 34, 2, False),
    ('pwm_limit', 36, 2, False), ('current_limit', 38, 2, False),
    ('velocity_limit', 44, 4, False), ('max_position', 48, 4, False),
    ('min_position', 52, 4, False), ('startup_configuration', 60, 1, False),
    ('pwm_slope', 62, 1, False), ('shutdown', 63, 1, False),
    ('torque', 64, 1, False), ('led', 65, 1, False),
    ('status_return_level', 68, 1, False), ('registered_instruction', 69, 1, False),
    ('hardware_error', 70, 1, False), ('velocity_i_gain', 76, 2, False),
    ('velocity_p_gain', 78, 2, False), ('position_d_gain', 80, 2, False),
    ('position_i_gain', 82, 2, False), ('position_p_gain', 84, 2, False),
    ('feedforward_2nd', 88, 2, False), ('feedforward_1st', 90, 2, False),
    ('bus_watchdog', 98, 1, True), ('goal_pwm', 100, 2, True),
    ('goal_current', 102, 2, True), ('goal_velocity', 104, 4, True),
    ('profile_acceleration', 108, 4, False), ('profile_velocity', 112, 4, False),
    ('goal_position', 116, 4, True), ('present_current', 126, 2, True),
    ('present_velocity', 128, 4, True), ('present_position', 132, 4, True),
    ('input_voltage_raw', 144, 2, False), ('temperature', 146, 1, False),
    ('backup_ready', 147, 1, False),
)


def read_packet(device_id, address, size):
    if device_id not in dxl.JOINTS or (address, size) not in READS:
        raise ValueError('outside explicit configuration audit read scope')
    body = (b'\x02' + struct.pack('<HH', address, size)).replace(b'\xff\xff\xfd', b'\xff\xff\xfd\xfd')
    data = dxl.HEADER + bytes([device_id]) + struct.pack('<H', len(body)+2) + body
    return data + struct.pack('<H', dxl.crc16(data))


def read(fd, device_id, address, size, stats):
    data = read_packet(device_id, address, size)
    termios.tcflush(fd, termios.TCIFLUSH)
    sent, deadline = 0, time.monotonic()+0.2
    while sent < len(data):
        if not select.select([], [fd], [], max(0, deadline-time.monotonic()))[1]:
            raise TimeoutError('serial transmit timeout')
        sent += os.write(fd, data[sent:])
    termios.tcdrain(fd)
    stats['requests'] += 1
    buffer, deadline = bytearray(), time.monotonic()+0.2
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
                raise ValueError(f'invalid status from ID{device_id}: {body.hex()}')
            stats['replies'] += 1
            stats['alert_packets'] += bool(body[1] & 0x80)
            return body[2:]
    stats['timeouts'] += 1
    raise TimeoutError(f'ID{device_id}, address{address}: no valid response')


def decode(device_id, image):
    if len(image) != 148:
        raise ValueError('incomplete register image')
    fields = {name: int.from_bytes(image[address:address+size], 'little', signed=signed)
              for name, address, size, signed in FIELDS}
    fixed = {'id_register': device_id, 'baud_register': 3, 'protocol': 2,
             'return_delay': 0, 'pwm_slope': 255, 'shutdown': 52,
             'homing_offset': -512 if device_id in (13, 23) else 0}
    init = {'position_p_gain': 200, 'position_i_gain': 0, 'position_d_gain': 0}
    return {'id': device_id, 'joint': dxl.JOINTS[device_id], 'fields': fields,
            'fixed_mismatches': {k: {'actual': fields[k], 'expected': v} for k, v in fixed.items() if fields[k] != v},
            'init_gain_differences': {k: {'actual': fields[k], 'expected': v} for k, v in init.items() if fields[k] != v},
            'non_voltage_fault_bits': fields['hardware_error'] & ~1}


def main():
    state = subprocess.run(['systemctl', 'is-active', 'robotd.service', 'padd.service',
                            'netpadd.service'], capture_output=True, text=True)
    if any(s in ('active', 'activating', 'deactivating') for s in state.stdout.splitlines()):
        raise ValueError('robot services active; refusing shared serial access')
    stats = dict.fromkeys(('requests', 'replies', 'crc_errors', 'invalid_lengths', 'timeouts', 'alert_packets'), 0)
    results = []
    with dxl.serial_port() as fd:
        for device_id in dxl.JOINTS:
            image = b''.join(read(fd, device_id, address, size, stats) for address, size in READS)
            results.append(decode(device_id, image))
    print('RESULT_JSON='+json.dumps({'upstream_commit': '942e58e3aa6dbb28e7037203fff11866122c7567',
          'comparison_phase': 'original init: P200 I0 D0', 'statistics': stats,
          'results': results, 'motor_writes_sent': False, 'board_records_written': False},
          separators=(',', ':')), flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
