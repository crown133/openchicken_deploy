"""Protocol vectors, corruption handling and read-only command boundary."""
import importlib.util
from pathlib import Path
import struct
import unittest

path = Path(__file__).resolve().parents[1] / 'scripts/test-dxl-communication.py'
spec = importlib.util.spec_from_file_location('dxl_diagnostic', path)
dxl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dxl)


def status(device_id, parameters):
    body = (b'\x55\x00' + parameters).replace(b'\xff\xff\xfd', b'\xff\xff\xfd\xfd')
    frame = dxl.HEADER + bytes([device_id]) + struct.pack('<H', len(body) + 2) + body
    return frame + struct.pack('<H', dxl.crc16(frame))


class ProtocolTests(unittest.TestCase):
    def stats(self):
        return {'crc_errors': 0, 'invalid_lengths': 0}

    def test_robotis_official_crc_vectors(self):
        self.assertEqual(dxl.packet(1, 1).hex(), 'fffffd0001030001194e')
        frame = bytes.fromhex('fffffd000107000284000400')
        self.assertEqual(dxl.crc16(frame), 0x151d)
        frame = bytes.fromhex('fffffd000107005500060426655d')
        self.assertEqual(dxl.crc16(frame[:-2]), int.from_bytes(frame[-2:], 'little'))

    def test_voltage_limit_address_order_from_xl330_control_table(self):
        # Address 31: temperature; 32: maximum voltage; 34: minimum voltage.
        self.assertEqual(dxl.decode_limits(struct.pack('<BHH', 70, 70, 35)),
                         {'temperature_limit_c': 70, 'max_voltage_limit_v': 7.0,
                          'min_voltage_limit_v': 3.5})

    def test_only_ping_and_whitelisted_reads_can_be_transmitted(self):
        for instruction in (3, 4, 5, 6, 8, 0x83, 0x8a, 0x93):
            with self.assertRaises(ValueError):
                dxl.packet(10, instruction, b'\x40\x00\x01')
        with self.assertRaises(ValueError):
            dxl.packet(10, 2, struct.pack('<HH', 116, 4))
        with self.assertRaises(ValueError):
            dxl.packet(254, 1)
        self.assertTrue(dxl.packet(10, 2, struct.pack('<HH', 144, 3)))

    def test_partial_packets_and_echo_are_distinguishable(self):
        stats = self.stats()
        frame = status(10, b'\x06\x04\x26')
        buffer = bytearray(b'noise' + frame[:8])
        self.assertIsNone(dxl.take_frame(buffer, stats))
        buffer.extend(frame[8:])
        self.assertEqual(dxl.take_frame(buffer, stats), (10, b'\x55\x00\x06\x04\x26'))
        echo = bytearray(dxl.packet(10, 1))
        self.assertEqual(dxl.take_frame(echo, stats), (10, b'\x01'))

    def test_crc_corruption_resynchronizes_to_next_valid_packet(self):
        frame = bytearray(status(10, b'\x06\x04\x26'))
        frame[-1] ^= 1
        frame.extend(status(11, b'\x06\x04\x26'))
        stats = self.stats()
        self.assertEqual(dxl.take_frame(frame, stats), (11, b'\x55\x00\x06\x04\x26'))
        self.assertEqual(stats['crc_errors'], 1)

    def test_stuffed_payload_is_crc_checked_then_decoded(self):
        frame = bytearray(status(10, b'\xff\xff\xfd\xfd\x01'))
        self.assertEqual(dxl.take_frame(frame, self.stats()), (10, b'\x55\x00\xff\xff\xfd\xfd\x01'))

    def test_expected_ids_exclude_mouth_and_imu_bridge(self):
        self.assertEqual(list(dxl.JOINTS), list(range(10, 15)) + list(range(20, 25)) + list(range(30, 34)))


if __name__ == '__main__':
    unittest.main()
