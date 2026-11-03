"""Only local buffers: audit read scope, signed registers and comparison phases."""
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts/audit-servo-config.py'
spec = importlib.util.spec_from_file_location('servo_audit', path)
audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(audit)


def image(device_id=13):
    data = bytearray(148)
    values = {'id_register': device_id, 'baud_register': 3, 'protocol': 2,
              'return_delay': 0, 'pwm_slope': 255, 'shutdown': 52,
              'homing_offset': -512 if device_id in (13, 23) else 0,
              'position_p_gain': 400, 'position_i_gain': 0, 'position_d_gain': 0,
              'max_voltage_limit_raw': 70, 'min_voltage_limit_raw': 35, 'hardware_error': 1}
    for name, address, size, signed in audit.FIELDS:
        data[address:address+size] = values.get(name, 0).to_bytes(size, 'little', signed=signed)
    return data


class AuditTests(unittest.TestCase):
    def test_all_packets_are_individual_read_with_valid_crc(self):
        for device_id in audit.dxl.JOINTS:
            for address, size in audit.READS:
                b = audit.read_packet(device_id, address, size)
                self.assertEqual((b[4], b[7]), (device_id, 2))
                self.assertEqual(audit.dxl.crc16(b[:-2]), int.from_bytes(b[-2:], 'little'))
        for args in ((34, 0, 64), (200, 0, 64), (254, 0, 64), (13, 116, 4)):
            with self.assertRaises(ValueError):
                audit.read_packet(*args)

    def test_signed_offset_voltage_order_and_runtime_gain_are_separate(self):
        row = audit.decode(13, image())
        self.assertEqual(row['fields']['homing_offset'], -512)
        self.assertEqual(row['fields']['max_voltage_limit_raw'], 70)
        self.assertEqual(row['fields']['min_voltage_limit_raw'], 35)
        self.assertEqual(row['fixed_mismatches'], {})
        self.assertEqual(row['init_gain_differences'], {'position_p_gain': {'actual': 400, 'expected': 200}})
        self.assertEqual(row['non_voltage_fault_bits'], 0)

    def test_pwm_slope_mismatch_does_not_disappear_among_other_values(self):
        data = image()
        data[62] = 140
        data[70] = 5
        row = audit.decode(13, data)
        self.assertEqual(row['fixed_mismatches'], {'pwm_slope': {'actual': 140, 'expected': 255}})
        self.assertEqual(row['non_voltage_fault_bits'], 4)

    def test_partial_image_is_rejected(self):
        with self.assertRaises(ValueError):
            audit.decode(13, bytes(147))


if __name__ == '__main__':
    unittest.main()
