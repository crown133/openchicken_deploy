#!/usr/bin/env python3
"""Read-only XL330 Protocol 2.0 check, UART5 at 1 Mbps; stdout only.

Only Ping and whitelisted Read packets can be built. No torque/position/config writes.
Reference: https://emanual.robotis.com/docs/en/dxl/protocol2/
"""
import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import select
import struct
import subprocess
import sys
import termios
import time

JOINTS = dict(zip((10, 11, 12, 13, 14, 20, 21, 22, 23, 24, 30, 31, 32, 33),
                  ('right_hip_yaw', 'right_hip_roll', 'right_hip_pitch', 'right_knee',
                   'right_ankle', 'left_hip_yaw', 'left_hip_roll', 'left_hip_pitch',
                   'left_knee', 'left_ankle', 'neck_pitch', 'head_pitch', 'head_yaw', 'head_roll')))
HEADER = b'\xff\xff\xfd\x00'
READS = {(0, 10), (31, 5), (63, 1), (64, 1), (70, 1), (144, 3)}


def decode_limits(data):
    temperature, maximum, minimum = struct.unpack('<BHH', data)
    return {'temperature_limit_c': temperature,
            'max_voltage_limit_v': maximum / 10.0, 'min_voltage_limit_v': minimum / 10.0}


def crc16(data):
    crc = 0
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ (0x8005 if crc & 0x8000 else 0)) & 0xffff
    return crc


def packet(device_id, instruction, parameters=b''):
    if not 0 <= device_id <= 252:
        raise ValueError('only individual device IDs are allowed')
    if not (instruction == 1 and not parameters or instruction == 2
            and len(parameters) == 4 and struct.unpack('<HH', parameters) in READS):
        raise ValueError('only Ping and approved diagnostic Read instructions are allowed')
    body = (bytes([instruction]) + parameters).replace(b'\xff\xff\xfd', b'\xff\xff\xfd\xfd')
    data = HEADER + bytes([device_id]) + struct.pack('<H', len(body) + 2) + body
    return data + struct.pack('<H', crc16(data))


def take_frame(buffer, stats):
    while True:
        start = buffer.find(HEADER)
        if start < 0:
            if len(buffer) > 3:
                del buffer[:-3]
            return None
        del buffer[:start]
        if len(buffer) < 7:
            return None
        length = int.from_bytes(buffer[5:7], 'little')
        if not 3 <= length <= 128:
            del buffer[0]
            stats['invalid_lengths'] += 1
            continue
        size = 7 + length
        if len(buffer) < size:
            return None
        frame = bytes(buffer[:size])
        if crc16(frame[:-2]) != int.from_bytes(frame[-2:], 'little'):
            stats['crc_errors'] += 1
            del buffer[0]
            continue
        del buffer[:size]
        body = frame[7:-2].replace(b'\xff\xff\xfd\xfd', b'\xff\xff\xfd')
        return frame[4], body


@contextmanager
def serial_port():
    port = Path('/dev/openchicken-dxl').resolve(strict=True)
    node = Path('/sys/class/tty') / port.name / 'device/of_node'
    if node.resolve().name != 'serial@fe690000':
        raise ValueError(f'{port} is not the configured UART5 controller')
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)
    original, exclusive = None, False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.ioctl(fd, termios.TIOCEXCL)
        exclusive = True
        original = termios.tcgetattr(fd)
        settings = original.copy()
        settings[6] = original[6].copy()
        settings[0] = settings[1] = settings[3] = 0
        settings[2] = termios.CLOCAL | termios.CREAD | termios.CS8
        settings[4] = settings[5] = termios.B1000000
        settings[6][termios.VMIN] = settings[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, settings)
        termios.tcflush(fd, termios.TCIOFLUSH)
        yield fd
    finally:
        try:
            if original is not None:
                termios.tcsetattr(fd, termios.TCSANOW, original)
        finally:
            try:
                if exclusive:
                    fcntl.ioctl(fd, termios.TIOCNXCL)
            finally:
                os.close(fd)


def exchange(fd, device_id, instruction, parameters, expected_size, stats):
    data = packet(device_id, instruction, parameters)
    termios.tcflush(fd, termios.TCIFLUSH)
    sent, deadline = 0, time.monotonic() + 0.2
    while sent < len(data):
        if not select.select([], [fd], [], max(0, deadline - time.monotonic()))[1]:
            raise TimeoutError('serial transmit timeout')
        sent += os.write(fd, data[sent:])
    termios.tcdrain(fd)
    stats['requests'] += 1
    buffer, deadline = bytearray(), time.monotonic() + 0.15
    while time.monotonic() < deadline:
        if not select.select([fd], [], [], max(0, deadline - time.monotonic()))[0]:
            break
        buffer.extend(os.read(fd, 512))
        while True:
            parsed = take_frame(buffer, stats)
            if parsed is None:
                break
            returned_id, body = parsed
            if not body or body[0] != 0x55:
                stats['echo_or_instruction_frames'] += 1
                continue
            if returned_id != device_id:
                stats['unexpected_id_frames'] += 1
                continue
            if len(body) < 2:
                raise ValueError('truncated status packet')
            if body[1] & 0x7f:
                raise ValueError(f'device status error 0x{body[1]:02x}')
            if len(body[2:]) != expected_size:
                raise ValueError('unexpected status payload length')
            stats['status_packets'] += 1
            return body[2:], bool(body[1] & 0x80)
    stats['timeouts'] += 1
    raise TimeoutError(f'no valid response from ID {device_id}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--ids', type=int, nargs='+', default=list(JOINTS))
    parser.add_argument('--rounds', type=int, choices=range(1, 11), default=3)
    args = parser.parse_args()
    if len(set(args.ids)) != len(args.ids) or any(i not in JOINTS for i in args.ids):
        parser.error('--ids must be unique IDs from 10-14, 20-24, 30-33')
    services = subprocess.run(['systemctl', 'is-active', 'robotd.service', 'padd.service',
                               'netpadd.service'], capture_output=True, text=True)
    if any(state in ('active', 'activating', 'deactivating') for state in services.stdout.splitlines()):
        raise ValueError('robot services are active; refusing to share the actuator bus')
    stats = dict.fromkeys(('requests', 'status_packets', 'crc_errors', 'invalid_lengths',
                          'echo_or_instruction_frames', 'unexpected_id_frames', 'timeouts'), 0)
    results = []
    with serial_port() as fd:
        for device_id in args.ids:
            item = {'id': device_id, 'joint': JOINTS[device_id], 'ping_ok': 0,
                    'ping_attempts': args.rounds, 'alerts': 0, 'errors': []}
            for _ in range(args.rounds):
                try:
                    data, alert = exchange(fd, device_id, 1, b'', 3, stats)
                    item.update(model_number=int.from_bytes(data[:2], 'little'), firmware=data[2])
                    item['ping_ok'] += 1
                    item['alerts'] += alert
                except (TimeoutError, ValueError) as error:
                    item['errors'].append(str(error))
                time.sleep(0.01)
            if item['ping_ok']:
                try:
                    blocks = {}
                    for address, size in ((0, 10), (31, 5), (63, 1), (64, 1), (70, 1), (144, 3)):
                        data, alert = exchange(fd, device_id, 2, struct.pack('<HH', address, size), size, stats)
                        blocks[address] = data
                        item['alerts'] += alert
                    item.update(configured_id=blocks[0][7], baud_register=blocks[0][8],
                                return_delay_register=blocks[0][9], torque_enabled=blocks[64][0],
                                hardware_error=blocks[70][0],
                                shutdown_register=blocks[63][0],
                                voltage_v=int.from_bytes(blocks[144][:2], 'little') / 10.0,
                                temperature_c=blocks[144][2], **decode_limits(blocks[31]))
                    if item['configured_id'] != device_id or item['baud_register'] != 3:
                        item['errors'].append('ID or 1 Mbps baud register readback mismatch')
                except (TimeoutError, ValueError) as error:
                    item['errors'].append(str(error))
            item['communication_passed'] = item['ping_ok'] == args.rounds and not item['errors']
            results.append(item)
            print(json.dumps(item, separators=(',', ':')), flush=True)
    report = {'port': '/dev/openchicken-dxl', 'baud': 1000000, 'protocol': '2.0',
              'instructions_sent': ['PING 0x01', 'READ 0x02'], 'records_written_on_board': False,
              'results': results, 'statistics': stats,
              'all_communication_passed': all(r['communication_passed'] for r in results)}
    print('RESULT_JSON=' + json.dumps(report, separators=(',', ':')), flush=True)
    return 0 if report['all_communication_passed'] else 1


if __name__ == '__main__':
    try:
        sys.exit(main())
    except (OSError, ValueError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
