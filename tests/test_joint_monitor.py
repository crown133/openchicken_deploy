"""Calibration display conversion and read-only transport scope."""
import importlib.util
from pathlib import Path
import struct
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location(
    'joint_monitor', Path(__file__).resolve().parents[1] / 'scripts/monitor-joints.py')
monitor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(monitor)


def feedback(position, voltage=78):
    return struct.pack('<i', position) + bytes(8) + struct.pack('<HB', voltage, 24)


class MonitorTests(unittest.TestCase):
    def test_signed_feedback_zero_and_full_turn_are_not_wrapped(self):
        for position in (-5, 0, 2048, 4096):
            raw, angle, voltage = monitor.decode(feedback(position))
            self.assertEqual(raw, position)
            self.assertEqual(angle, (position - 2048) * 360 / 4096)
            self.assertEqual(voltage, 7.8)
        with self.assertRaises(ValueError):
            monitor.decode(bytes(14))

    def test_transport_rejects_writes_and_non_feedback_reads(self):
        dxl = monitor.load_diagnostic()
        for instruction in (3, 4, 5, 6, 8, 0x83):
            with self.assertRaises(ValueError):
                dxl.packet(13, instruction, b'\x40\x00\x01')
        with self.assertRaises(ValueError):
            dxl.packet(13, 2, struct.pack('<HH', 116, 4))
        packet = dxl.packet(13, 2, struct.pack('<HH', 132, 15))
        self.assertEqual(dxl.crc16(packet[:-2]), int.from_bytes(packet[-2:], 'little'))

    def test_all_14_ids_use_only_feedback_read_and_preserve_alert(self):
        dxl = monitor.load_diagnostic()
        with patch.object(monitor, 'read_feedback', return_value=(feedback(2048), True)) as exchange:
            rows = monitor.sample(dxl, 99, {})
        self.assertEqual([r[0] for r in rows], list(dxl.JOINTS))
        for call in exchange.call_args_list:
            self.assertIn(call.args[2], dxl.JOINTS)
        self.assertTrue(all(r[-1] for r in rows))

    def test_failed_joint_aborts_sweep_instead_of_reusing_old_feedback(self):
        dxl = monitor.load_diagnostic()
        with patch.object(monitor, 'read_feedback', side_effect=[(feedback(2048), False), TimeoutError()]):
            with self.assertRaises(TimeoutError):
                monitor.sample(dxl, 99, {})

    def test_reader_handles_partial_reply_and_ignores_wrong_id(self):
        dxl = monitor.load_diagnostic()
        def status(device_id):
            body = b'\x55\x80' + feedback(-5)
            body = body.replace(b'\xff\xff\xfd', b'\xff\xff\xfd\xfd')
            frame = dxl.HEADER + bytes([device_id]) + struct.pack('<H', len(body) + 2) + body
            return frame + struct.pack('<H', dxl.crc16(frame))
        incoming = status(23) + status(13)
        stats = dict.fromkeys(('requests', 'status_packets', 'crc_errors', 'invalid_lengths',
                              'echo_or_instruction_frames', 'unexpected_id_frames', 'timeouts'), 0)
        with patch.object(monitor.select, 'select', return_value=([99], [99], [])), \
             patch.object(monitor.os, 'write', side_effect=lambda fd, data: len(data)) as write, \
             patch.object(monitor.os, 'read', side_effect=[incoming[:10], incoming[10:]]):
            self.assertEqual(monitor.read_feedback(dxl, 99, 13, stats), (feedback(-5), True))
        self.assertEqual(write.call_args.args[1], dxl.packet(13, 2, struct.pack('<HH', 132, 15)))
        self.assertEqual(stats['unexpected_id_frames'], 1)
        self.assertEqual(stats['status_packets'], 1)


if __name__ == '__main__':
    unittest.main()
