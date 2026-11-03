"""Sensor decoding and restoration failures, without hardware or register I/O."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / 'scripts/test-j5-imu.py'
spec = importlib.util.spec_from_file_location('j5_imu', SCRIPT)
imu = importlib.util.module_from_spec(spec)
spec.loader.exec_module(imu)


class FakeIMU:
    def __init__(self):
        self.regs = {0x01: 0, 0x10: 0, 0x11: 0, 0x12: 0x44,
                     0x15: 0x30, 0x17: 0x40}
        self.writes = []
        self.fail_once = None

    def read(self, reg):
        return self.regs[reg]

    def write(self, reg, value):
        self.writes.append((reg, value))
        if self.fail_once == reg:
            self.fail_once = None
            raise OSError('injected I2C error')
        self.regs[reg] = value

    def transfer(self, reg, size):
        return bytes(size)


class IMUTests(unittest.TestCase):
    def test_signed_little_endian_and_st_sensitivity(self):
        values = imu.decode(struct.pack('<7h', -256, 1000, -2000, 0, 16384, -16384, 0))
        self.assertEqual(values[7], 24.0)
        self.assertAlmostEqual(values[8], 17.5)
        self.assertAlmostEqual(values[9], -35.0)
        self.assertAlmostEqual(values[11], 0.999424)
        self.assertAlmostEqual(values[12], -0.999424)

    @patch.object(imu.time, 'sleep')
    def test_restore_after_sampling_error_and_preserve_unrelated_bits(self, sleep):
        device = FakeIMU()
        before, report = imu.snapshot(device), {}
        with self.assertRaisesRegex(OSError, 'sampling error'):
            with imu.temporary_setup(device, before, report):
                self.assertEqual(device.regs[0x15], 0x32)
                self.assertEqual(device.regs[0x17], 0x40)
                self.assertEqual(device.regs[0x10], 6)
                raise OSError('sampling error')
        self.assertEqual(imu.snapshot(device), before)
        self.assertTrue(report['restored'])

    @patch.object(imu.time, 'sleep')
    def test_restore_after_partial_configuration(self, sleep):
        device = FakeIMU()
        before, report = imu.snapshot(device), {}
        device.fail_once = 0x10
        with self.assertRaisesRegex(OSError, 'injected'):
            with imu.temporary_setup(device, before, report):
                self.fail('configuration must fail before sampling')
        self.assertEqual(imu.snapshot(device), before)
        self.assertTrue(report['restored'])

    @patch.object(imu.time, 'sleep')
    def test_restore_tries_all_registers_and_reports_failure(self, sleep):
        device = FakeIMU()
        before, report = imu.snapshot(device), {}
        with self.assertRaisesRegex(RuntimeError, 'restore failed'):
            with imu.temporary_setup(device, before, report):
                device.fail_once = 0x11
        self.assertFalse(report['restored'])
        self.assertEqual([reg for reg, _ in device.writes[-5:]], [0x11, 0x10, 0x17, 0x15, 0x12])
        self.assertEqual(device.regs[0x15], before[0x15])

    def test_refuse_alternate_bank_and_running_sensor_without_writes(self):
        device = FakeIMU()
        device.regs[0x01] = 0x80
        with self.assertRaisesRegex(ValueError, 'FUNC_CFG_ACCESS'):
            imu.snapshot(device)
        device.regs[0x01] = 0
        device.regs[0x10] = 6
        with self.assertRaisesRegex(ValueError, 'already running'):
            with imu.temporary_setup(device, imu.snapshot(device), {}):
                self.fail('must refuse an active sensor')
        self.assertEqual(device.writes, [])


if __name__ == '__main__':
    unittest.main()
