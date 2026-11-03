"""Check that the leg-only entry point cannot send head commands."""
import importlib.util
from pathlib import Path
import struct
import unittest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('leg_standing', ROOT / 'scripts/move-legs-to-standing.py')
legs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(legs)


class LegScopeTests(unittest.TestCase):
    def test_only_leg_targets_and_emergency_stop_entries(self):
        self.assertEqual(tuple(legs.motion.plan.POSE), legs.LEG_IDS)
        for address, size in ((64, 1), (116, 4)):
            packet = legs.motion.sync_packet(address)
            body = packet[8:-2].replace(b'\xff\xff\xfd\xfd', b'\xff\xff\xfd')
            self.assertEqual(struct.unpack('<HH', body[:4]), (address, size))
            self.assertEqual(tuple(body[k] for k in range(4, len(body), size+1)), legs.LEG_IDS)

    def test_head_reads_and_writes_rejected(self):
        for i in (30, 31, 32, 33):
            with self.assertRaises(ValueError):
                legs.motion.write_packet(i, 64, b'\x01', {'id': i})
            with self.assertRaises(ValueError):
                legs.motion.plan.read_packet(i, 63, 2)

    def test_preflight_reads_only_legs(self):
        from unittest.mock import patch
        seen = []
        with patch.object(legs.motion.plan, 'read', side_effect=lambda fd, i, a, n, s: seen.append(i)), \
             patch.object(legs.motion.plan, 'decode', side_effect=lambda i, b: i):
            self.assertEqual(legs.motion.plan.read_all(None, {}), list(legs.LEG_IDS))
        self.assertEqual(set(seen), set(legs.LEG_IDS))
        self.assertEqual(len(seen), len(legs.LEG_IDS)*len(legs.motion.plan.READS))


if __name__ == '__main__':
    unittest.main()
