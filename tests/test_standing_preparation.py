"""No hardware: pose integrity, actuator write scope, confirmation and abort paths."""
from contextlib import nullcontext, redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import re
import struct
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('standing_motion', ROOT / 'scripts/move-to-standing.py')
motion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(motion)
plan = motion.plan


def blocks(device_id=10, fault=1, offset=0):
    return {0: struct.pack('<H', 1200) + bytes(4) + bytes([53, device_id, 3, 0, 0, 3, 255, 2]),
            20: struct.pack('<i', offset), 31: struct.pack('<BHH', 70, 70, 35),
            38: struct.pack('<H', 1750), 44: struct.pack('<III', 445, 4095, 0),
            62: b'\xff', 63: bytes([52, 0]), 68: bytes([2, 0, fault]),
            80: struct.pack('<HHH', 0, 0, 200),
            108: struct.pack('<IIi', 0, 0, 2048),
            124: struct.pack('<hhi i', 0, 0, 0, 2048), 144: struct.pack('<HB', 79, 27)}


class StandingTests(unittest.TestCase):
    def test_targets_match_original_model_excluding_only_mouth(self):
        proto = (ROOT / 'openchicken-ipc-proto/src/lib.rs').read_text()
        model = (ROOT / 'openchicken-control/src/model.rs').read_text()
        raw = proto.split('pub const JOINT_IDS: [u8; 15] = [')[1].split('];')[0]
        ids = [int(v) for v in re.findall(r'\b\d+\b', re.sub(r'//[^\n]*', '', raw))]
        raw = model.split('pub const DEFAULT_POSITION: [f64; NUM_JOINTS] = [')[1].split('];')[0]
        angles = [float(v) for v in re.findall(r'[-+]?\d+\.\d+', re.sub(r'//[^\n]*', '', raw))]
        self.assertEqual(plan.POSE, {i: a for i, a in zip(ids, angles) if i != 34})
        self.assertEqual(plan.raw_target(0), 2048)

    def test_explicit_voltage_exception_preserves_other_fault_checks(self):
        self.assertEqual(plan.decode(10, blocks())['blockers'], [])
        self.assertIn('non-voltage hardware fault active', plan.decode(10, blocks(fault=5))['blockers'])
        self.assertIn('homing offset differs from original', plan.decode(13, blocks(device_id=13))['blockers'])
        self.assertEqual(plan.decode(13, blocks(device_id=13, offset=-512))['blockers'], [])
        bad = blocks()
        bad[0] = bad[0][:10] + b'\x08' + bad[0][11:]
        self.assertIn('goal update auto-enables torque', plan.decode(10, bad)['blockers'])

    def test_pid_and_pwm_slope_must_match_before_motion(self):
        bad = blocks()
        bad[62] = bytes([140])
        bad[80] = struct.pack('<HHH', 0, 0, 400)
        reasons = plan.decode(10, bad)['blockers']
        self.assertIn('PWM slope differs from original', reasons)
        self.assertIn('init PID differs from original', reasons)

    def test_motion_requires_fixed_robot_confirmation_before_hardware_access(self):
        with patch.object(sys, 'argv', ['motion', '--enable-and-stand']), patch.object(motion, 'check_services') as check:
            with self.assertRaises(SystemExit):
                motion.main()
            check.assert_not_called()

    def test_default_mode_only_calls_readonly_plan(self):
        with patch.object(sys, 'argv', ['motion']), patch.object(motion, 'check_services'), patch.object(plan, 'main') as preview:
            motion.main()
            preview.assert_called_once()

    def test_write_scope_excludes_eeprom_and_unplanned_positions(self):
        row = plan.decode(10, blocks())
        for address, data in ((9, b'\x00'), (20, struct.pack('<i', -512)), (63, b'\x34'),
                              (64, b'\x02'), (116, struct.pack('<i', 1024)), (108, bytes(4))):
            with self.assertRaises(ValueError):
                motion.write_packet(10, address, data, row)
        for address, data in ((64, b'\x01'), (108, struct.pack('<II', 2, 10)),
                              (116, struct.pack('<i', 2048))):
            frame = motion.write_packet(10, address, data, row)
            self.assertEqual(motion.dxl.crc16(frame[:-2]), int.from_bytes(frame[-2:], 'little'))

    def test_sync_packets_have_exact_targets_or_only_torque_off(self):
        for address, size in ((116, 4), (64, 1)):
            frame = motion.sync_packet(address)
            self.assertEqual((frame[4], frame[7]), (254, 0x83))
            body = frame[8:-2].replace(b'\xff\xff\xfd\xfd', b'\xff\xff\xfd')
            self.assertEqual(struct.unpack('<HH', body[:4]), (address, size))
            entries = [body[k:k+size+1] for k in range(4, len(body), size+1)]
            self.assertEqual([entry[0] for entry in entries], list(plan.POSE))
            expected = [0 if address == 64 else plan.raw_target(a) for a in plan.POSE.values()]
            self.assertEqual([int.from_bytes(entry[1:], 'little') for entry in entries], expected)

    def test_failure_during_profile_setup_requests_torque_off_and_restores_profile(self):
        row, sends = plan.decode(10, blocks()), []
        with patch.object(sys, 'argv', ['motion', '--enable-and-stand', '--robot-fixed']), \
             patch.object(motion, 'check_services'), patch.object(motion.dxl, 'serial_port', return_value=nullcontext(99)), \
             patch.object(plan, 'read_all', return_value=[row]), patch.object(plan, 'read', return_value=bytes(12)), \
             patch.object(motion, 'send', side_effect=lambda fd, data: sends.append(data)), \
             patch.object(motion.time, 'sleep'), redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ValueError, 'profile readback failed'):
                motion.main()
        self.assertIn(motion.sync_packet(64), sends)
        self.assertEqual(sends[-1], motion.write_packet(10, 108, bytes(8), row))
        self.assertNotIn(motion.write_packet(10, 64, b'\x01', row), sends)

    def run_mock_transition(self, fault):
        row, sends, calls = plan.decode(10, blocks()), [], {'profile': 0, 'torque': 0}
        def read(fd, i, address, size, stats):
            if address == 108:
                calls['profile'] += 1
                return (struct.pack('<II', 2, 10) if calls['profile'] == 1 else bytes(8)) + bytes(4)
            if address == 63:
                calls['torque'] += 1
                return bytes([52, 0 if calls['torque'] == 1 else 1])
            if address == 124:
                return bytes(8) + struct.pack('<i', 2048)
            if address == 68:
                return bytes([2, 0, fault])
            self.fail(f'unexpected read {address}')
        with patch.object(sys, 'argv', ['motion', '--enable-and-stand', '--robot-fixed']), \
             patch.object(motion, 'check_services'), patch.object(motion.dxl, 'serial_port', return_value=nullcontext(99)), \
             patch.object(plan, 'read_all', return_value=[row]), patch.object(plan, 'read', side_effect=read), \
             patch.object(motion, 'send', side_effect=lambda fd, data: sends.append(data)), \
             patch.object(motion.time, 'sleep'), redirect_stdout(StringIO()):
            if fault & ~1:
                with self.assertRaisesRegex(ValueError, 'non-voltage hardware fault'):
                    motion.main()
            else:
                motion.main()
        return row, sends

    def test_success_holds_enabled_pose_and_restores_original_profile(self):
        row, sends = self.run_mock_transition(1)
        enable = motion.write_packet(10, 64, b'\x01', row)
        self.assertIn(enable, sends)
        self.assertLess(sends.index(enable), sends.index(motion.sync_packet(116)))
        self.assertNotIn(motion.sync_packet(64), sends)
        self.assertEqual(sends[-1], motion.write_packet(10, 108, bytes(8), row))

    def test_fault_after_enable_requests_torque_off_before_cleanup(self):
        row, sends = self.run_mock_transition(5)
        stop = motion.sync_packet(64)
        self.assertIn(motion.write_packet(10, 64, b'\x01', row), sends)
        self.assertIn(stop, sends)
        self.assertLess(sends.index(stop), len(sends)-1)


if __name__ == '__main__':
    unittest.main()
