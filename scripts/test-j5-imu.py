#!/usr/bin/env python3
"""J5 LSM6DSV16X diagnostic, Python standard library only; no robot services.

Reference: STMicroelectronics/lsm6dsv16x-pid @
23a6e592c4892bb4c8cf8b78a15de3b8ac4cea97, DS13510 Rev 4.
Default: identify only. --seconds enables temporary sampling and restores registers.
"""
import argparse
from contextlib import contextmanager
import csv
import ctypes
from datetime import datetime, timezone
import errno
import fcntl
import json
import math
import os
from pathlib import Path
import signal
import statistics
import struct
import sys
import tempfile
import time

REGISTERS = (0x10, 0x11, 0x12, 0x15, 0x17)
FIELDS = ('time_s', 'temp_raw', 'gx_raw', 'gy_raw', 'gz_raw',
          'ax_raw', 'ay_raw', 'az_raw', 'temp_c',
          'gx_dps', 'gy_dps', 'gz_dps', 'ax_g', 'ay_g', 'az_g')


class Message(ctypes.Structure):
    _fields_ = [('addr', ctypes.c_uint16), ('flags', ctypes.c_uint16),
                ('len', ctypes.c_uint16), ('buf', ctypes.POINTER(ctypes.c_uint8))]


class Transfer(ctypes.Structure):
    _fields_ = [('msgs', ctypes.POINTER(Message)), ('nmsgs', ctypes.c_uint32)]


class I2C:
    def __init__(self, fd, address):
        # I2C_SLAVE rejects kernel-owned addresses; never use I2C_SLAVE_FORCE.
        fcntl.ioctl(fd, 0x0703, address)
        self.fd, self.address = fd, address
        self.libc = ctypes.CDLL(None, use_errno=True)

    def transfer(self, reg, size=0, value=None):
        payload = bytes([reg]) if value is None else bytes([reg, value])
        tx = (ctypes.c_uint8 * len(payload))(*payload)
        rx = (ctypes.c_uint8 * size)()
        messages = (Message * (2 if size else 1))()
        messages[0] = Message(self.address, 0, len(tx), tx)
        if size:
            messages[1] = Message(self.address, 1, size, rx)
        data = Transfer(messages, len(messages))
        result = self.libc.ioctl(self.fd, 0x0707, ctypes.byref(data))
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, os.strerror(error))
        if result != len(messages):
            raise OSError(errno.EIO, 'incomplete I2C transaction')
        return bytes(rx)

    def read(self, reg):
        return self.transfer(reg, size=1)[0]

    def write(self, reg, value):
        self.transfer(reg, value=value)


def identify(fd, address):
    found, probes = [], {}
    for candidate in ([address] if address is not None else [0x6a, 0x6b]):
        try:
            device = I2C(fd, candidate)
            who = device.read(0x0f)
        except OSError as error:
            # Only a NACK is an absent device; bus errors must remain visible.
            if error.errno not in (errno.ENXIO, errno.EREMOTEIO):
                raise
            probes[hex(candidate)] = 'no ACK'
            continue
        probes[hex(candidate)] = hex(who)
        if who == 0x70:
            found.append(candidate)
    if len(found) != 1:
        raise ValueError(f'expected exactly one LSM6DSV16X; probes={probes}; '
                         'use --address if two devices are present')
    return I2C(fd, found[0]), probes


def snapshot(device):
    # Refuse an alternate register bank; do not reset another application's IMU.
    if device.read(0x01) != 0:
        raise ValueError('FUNC_CFG_ACCESS is nonzero; refusing to change bank/configuration')
    values = {reg: device.read(reg) for reg in REGISTERS}
    if values[0x12] & 0x81:
        raise ValueError('reset/reboot in progress')
    return values


def hex_registers(values):
    return {f'0x{reg:02x}': f'0x{value:02x}' for reg, value in values.items()}


@contextmanager
def temporary_setup(device, before, report):
    if before[0x10] & 0x0f or before[0x11] & 0x0f:
        raise ValueError('sensor already running; refusing to replace an active configuration')
    # HP 120 Hz, gyro +/-500 dps, accel +/-2 g; retain unrelated register bits.
    planned = {0x12: before[0x12] | 0x44,
               0x15: (before[0x15] & 0xf0) | 0x02,
               0x17: before[0x17] & 0xfc,
               0x10: (before[0x10] & 0x80) | 0x06,
               0x11: (before[0x11] & 0x80) | 0x06}
    report['registers_test'] = hex_registers(planned)
    try:
        for reg, value in planned.items():
            device.write(reg, value)
            if device.read(reg) != value:
                raise ValueError(f'configuration readback failed at {hex(reg)}')
        time.sleep(0.1)
        device.transfer(0x20, size=14)  # Discard startup data, consume both DRDY flags.
        yield
    finally:
        errors = []
        # Power down before restoring range/BDU; try every restoration after errors.
        for reg in (0x11, 0x10, 0x17, 0x15, 0x12):
            try:
                device.write(reg, before[reg])
                if device.read(reg) != before[reg]:
                    raise ValueError('readback mismatch')
            except (OSError, ValueError) as error:
                errors.append(f'{hex(reg)}: {error}')
        report['restored'] = not errors
        report['restore_errors'] = errors
        if errors:
            raise RuntimeError('register restore failed: ' + '; '.join(errors))


def decode(data):
    raw = struct.unpack('<7h', data)  # Temperature, gyro XYZ, accelerometer XYZ.
    return (*raw, raw[0] / 256.0 + 25.0,
            *(value * 0.0175 for value in raw[1:4]),
            *(value * 0.000061 for value in raw[4:7]))


def sample(device, seconds, writer, rows):
    start = time.monotonic()
    next_read = start
    while time.monotonic() - start < seconds:
        time.sleep(max(0, next_read - time.monotonic()))
        deadline = time.monotonic() + 1.0
        while device.read(0x1e) & 0x03 != 0x03:
            if time.monotonic() > deadline:
                raise TimeoutError('accelerometer/gyro data-ready timeout')
            time.sleep(0.002)
        values = decode(device.transfer(0x20, size=14))
        row = (time.monotonic() - start, *values)
        writer.writerow(row)
        rows.append(row)
        if len(rows) == 1 or row[0] - rows[-2][0] > 0.1 or len(rows) % 50 == 0:
            print(f't={row[0]:6.2f}s a(g)={values[11]:+.3f},'
                  f'{values[12]:+.3f},{values[13]:+.3f} '
                  f'g(dps)={values[8]:+.2f},{values[9]:+.2f},{values[10]:+.2f} '
                  f'T={values[7]:.1f}C', flush=True)
        # Target user-space polling 50 Hz, no FIFO/timing guarantee.
        next_read = max(next_read + 0.02, time.monotonic())


def summarize(rows):
    if not rows:
        return {'samples': 0}
    columns = list(zip(*rows))
    norms = [math.sqrt(sum(v * v for v in row[12:15])) for row in rows]
    return {'samples': len(rows), 'last_sample_time_s': rows[-1][0],
            'mean_accel_norm_g': statistics.mean(norms),
            'near_saturation_samples': sum(any(abs(v) >= 32760 for v in r[2:8]) for r in rows),
            'axes': {FIELDS[i]: {'mean': statistics.mean(columns[i]),
                                'std': statistics.pstdev(columns[i]),
                                'min': min(columns[i]), 'max': max(columns[i])}
                     for i in range(8, 15)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bus', default='/dev/i2c-openchicken')
    parser.add_argument('--address', type=lambda value: int(value, 0), choices=(0x6a, 0x6b),
                        metavar='0x6a|0x6b')
    parser.add_argument('--seconds', type=float, help='sample for >0 to 300 seconds; omit for read-only identification')
    parser.add_argument('--output', type=Path, help='new output directory (must not exist)')
    args = parser.parse_args()
    if args.seconds is not None and (not math.isfinite(args.seconds) or not 0 < args.seconds <= 300):
        parser.error('--seconds must be >0 and <=300')
    if args.output and args.seconds is None:
        parser.error('--output requires --seconds')
    bus = Path(args.bus).resolve(strict=True)
    controller = (Path('/sys/class/i2c-dev') / bus.name / 'device/of_node').resolve()
    if controller.name != 'i2c@fe5d0000':
        raise ValueError(f'{bus} is not the configured J5 I2C4 controller: {controller}')
    with bus.open('r+b', buffering=0) as handle:
        # Cooperating copies of this script cannot change the shared bus concurrently.
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError(f'another sampling process holds {bus}; wait for it to finish') from None
        device, probes = identify(handle.fileno(), args.address)
        before = snapshot(device)
        report = {'utc': datetime.now(timezone.utc).isoformat(), 'bus': str(bus),
                  'controller': str(controller), 'address': hex(device.address),
                  'who_am_i': '0x70', 'probes': probes,
                  'registers_before': hex_registers(before)}
        print(json.dumps(report, indent=2), flush=True)
        if args.seconds is None:
            return 0
        if args.output:
            output = args.output
            output.mkdir(parents=True, exist_ok=False)
        else:
            parent = Path('artifacts/imu')
            parent.mkdir(parents=True, exist_ok=True)
            prefix = datetime.now(timezone.utc).strftime('lsm6dsv16x-%Y%m%dT%H%M%SZ-')
            output = Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
        report.update({'requested_seconds': args.seconds, 'sensor_odr_hz': 120,
                       'poll_target_hz': 50, 'accel_range_g': 2, 'gyro_range_dps': 500,
                       'restored': None, 'output': str(output)})
        (output / 'registers-before.json').write_text(json.dumps(report, indent=2) + '\n')
        rows, status = [], 0
        try:
            with (output / 'samples.csv').open('x', newline='') as stream:
                writer = csv.writer(stream)
                writer.writerow(FIELDS)
                with temporary_setup(device, before, report):
                    print(f'SAMPLING STARTED: {output}', flush=True)
                    sample(device, args.seconds, writer, rows)
        except KeyboardInterrupt:
            report['interrupted'] = True
            status = 130
        except (OSError, ValueError, RuntimeError) as error:
            report['error'] = str(error)
            status = 1
        finally:
            report['summary'] = summarize(rows)
            report['exit_status'] = status
            (output / 'report.json').write_text(json.dumps(report, indent=2) + '\n')
            print(json.dumps(report, indent=2), flush=True)
        return status


def interrupted(signum, frame):
    raise KeyboardInterrupt


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, interrupted)
    try:
        sys.exit(main())
    except PermissionError as error:
        print(f'ERROR: permission denied: {error.filename}; '
              'refresh your login or run newgrp i2c, then retry as your normal user',
              file=sys.stderr)
        sys.exit(1)
    except (OSError, ValueError, RuntimeError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
