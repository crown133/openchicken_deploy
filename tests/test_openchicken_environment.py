"""Host-only tests for authored planning code; no upstream script is executed."""
from contextlib import redirect_stdout
import importlib.util
from io import StringIO
from pathlib import Path
import tempfile
import unittest
import re
import sys
import subprocess
import os
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / 'deploy/openchicken/configure_hat.py'
spec = importlib.util.spec_from_file_location('configure_hat', MODULE)
hat = importlib.util.module_from_spec(spec)
spec.loader.exec_module(hat)
sys.modules['configure_hat'] = hat
audio_spec = importlib.util.spec_from_file_location('configure_audio', MODULE.parent / 'configure_audio.py')
audio = importlib.util.module_from_spec(audio_spec)
audio_spec.loader.exec_module(audio)


class BootPlanTests(unittest.TestCase):
    def test_preserve_debug_console_boot_root_and_unrelated_overlays(self):
        original = ('# keep this comment\nconsole=both\nextraargs=console=ttyFIQ0\n'
                    'rootdev=UUID=sd-card\noverlay_prefix=rk35xx\n'
                    'overlays=uart2-m0 unrelated-camera\nuser_overlays=local-led\n')
        planned = hat.plan_environment(original)
        values = hat.env_values(planned)
        for key in ('console', 'extraargs', 'rootdev', 'overlay_prefix'):
            self.assertEqual(values[key], hat.env_values(original)[key])
        self.assertEqual(values['overlays'], 'uart2-m0 unrelated-camera')
        self.assertEqual(values['user_overlays'], 'local-led openchicken-z3w-a1')
        self.assertIn('# keep this comment', planned)

    def test_remove_old_hat_audio_and_bitbang_tokens_only(self):
        old = ('overlays=i2c3-pihat aic3104-i2c3 camera\n'
               'user_overlays=i2c-gpio-pihat aic3104-pihat my-extra\n')
        values = hat.env_values(hat.plan_environment(old))
        self.assertEqual(values['overlays'], 'camera')
        self.assertEqual(values['user_overlays'], 'my-extra openchicken-z3w-a1')

    def test_idempotent(self):
        for extension in ('none', 'spi3', 'uart9'):
            once = hat.plan_environment('console=serial\n', extension)
            self.assertEqual(once, hat.plan_environment(once, extension))

    def test_switch_extension_removes_previous_mode(self):
        spi = hat.plan_environment('console=both\n', 'spi3')
        uart = hat.plan_environment(spi, 'uart9')
        self.assertIn('openchicken-z3w-a1-ext-uart9', uart)
        self.assertNotIn('openchicken-z3w-a1-ext-spi3', uart)
        self.assertNotIn('-ext-', hat.plan_environment(uart, 'none'))

    def test_audio_opt_in_preserved_after_common_board_setup_and_j7_switch(self):
        old = 'console=both\noverlays=i2c3-pihat aic3104-i2c3 camera\n'
        self.assertNotIn(hat.AUDIO_OVERLAY, hat.plan_environment(old))
        first = hat.plan_environment(old, 'uart9', 'external-12mhz')
        expected = 'openchicken-z3w-a1 openchicken-z3w-a1-ext-uart9 aic3104-i2c4'
        self.assertEqual(hat.env_values(first)['user_overlays'], expected)
        self.assertEqual(first, hat.plan_environment(first, 'uart9'))
        switched = hat.plan_environment(first, 'spi3')
        self.assertTrue(hat.env_values(switched)['user_overlays'].endswith('aic3104-i2c4'))
        self.assertNotIn(hat.AUDIO_OVERLAY, hat.plan_environment(switched, 'spi3', 'off'))
        with self.assertRaises(ValueError):
            hat.plan_environment(old, audio='invented-clock')

    def test_extension_is_preserved_on_reboot_and_explicit_none_disables_it(self):
        uart = hat.plan_environment('', 'uart9')
        self.assertEqual(hat.select_extension(None, uart), 'uart9')
        self.assertEqual(hat.select_extension('none', uart), 'none')
        self.assertEqual(hat.select_extension(None, ''), 'none')
        with self.assertRaises(ValueError):
            hat.select_extension(None, 'user_overlays=openchicken-z3w-a1-ext-uart9 openchicken-z3w-a1-ext-spi3\n')

    def test_old_i2c_alias_removed_without_changing_other_rules(self):
        unrelated = 'SUBSYSTEM=="tty", SYMLINK+="my-uart"\n'
        old = ('SUBSYSTEM=="i2c-dev", KERNELS=="fe5c0000.i2c", SYMLINK+="i2c-pihat"\n'
               'SUBSYSTEM=="i2c-dev", KERNELS=="fe5c0000.i2c", SYMLINK+="other i2c-pihat"\n'
               + unrelated)
        new = hat.plan_legacy_rule(old)
        active = '\n'.join(line for line in new.splitlines() if not line.startswith('#'))
        self.assertNotIn('i2c-pihat', active)
        self.assertIn('SYMLINK+="other"', active)
        self.assertIn(unrelated, new)
        self.assertEqual(hat.plan_legacy_rule(new), new)
        with self.assertRaises(ValueError):
            hat.plan_legacy_rule('SUBSYSTEM=="i2c-dev", KERNELS=="custom", SYMLINK+="i2c-pihat"\n')

    def test_refuse_duplicate_boot_keys(self):
        for key in ('overlays', 'user_overlays', 'console', 'fdtfile'):
            with self.subTest(key=key), self.assertRaises(ValueError):
                hat.plan_environment(f'{key}=first\n{key}=second\n')

    def test_refuse_pin_ownership_conflicts(self):
        for word in ('uart4-m0', 'i2c5-m0', 'spi3-m1', 'uart9-m1', 'usb2-host2',
                     'uart5-m0', 'i2c4-m1', 'pwm8-m1', 'pwm9-m1'):
            with self.subTest(word=word), self.assertRaises(ValueError):
                hat.plan_environment('overlays=' + word + '\n')

    def test_refuse_invalid_or_combined_extension(self):
        with self.assertRaises(ValueError):
            hat.plan_environment('', 'spi3 uart9')

    def test_plan_does_not_write_or_run_system_changes(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            path = Path(tmp)
            env = path / 'armbianEnv.txt'
            env.write_text('console=both\n')
            dtb = path / 'board.dtb'
            dtb.write_bytes(b'fixture')
            argv = ['configure_hat.py', '--plan', '--env', str(env), '--dtb', str(dtb)]
            with patch('sys.argv', argv), patch.object(hat, 'validate_and_compile', return_value={}), \
                 patch.object(hat.tempfile, 'tempdir', str(path)), \
                 patch.object(hat, 'LEGACY_RULE', path / 'legacy.rules'), \
                 patch.object(hat.shutil, 'which', return_value='/fixture/tool'), \
                 patch.object(hat, 'write_changed') as writes, patch.object(hat, 'run') as commands, \
                 redirect_stdout(StringIO()):
                self.assertEqual(hat.main(), 0)
                writes.assert_not_called()
                commands.assert_not_called()
            self.assertEqual(env.read_text(), 'console=both\n')


class MappingAndBackupTests(unittest.TestCase):
    def test_identify_controllers_when_linux_numbers_differ(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            sys = root / 'sys'
            def add(subsystem, name, node):
                platform = sys / 'devices/platform' / node.replace('@', '.')
                of_node = sys / 'firmware/devicetree/base' / node
                of_node.mkdir(parents=True, exist_ok=True)
                platform.mkdir(parents=True, exist_ok=True)
                (platform / 'of_node').symlink_to(of_node)
                entry = sys / 'class' / subsystem / name
                entry.mkdir(parents=True)
                (entry / 'device').symlink_to(platform)
            add('tty', 'ttyS1', 'serial@fe690000')
            add('tty', 'ttyS5', 'serial@fe660000')
            add('i2c-dev', 'i2c-7', 'i2c@fe5d0000')
            add('i2c-dev', 'i2c-4', 'i2c@fe5c0000')
            add('pwm', 'pwmchip3', 'pwm@fe6f0000')
            add('pwm', 'pwmchip0', 'pwm@fe6f0010')
            found = hat.discover_devices(sys, root / 'dev')
            self.assertEqual(found['dxl_uart5'], str(root / 'dev/ttyS1'))
            self.assertEqual(found['hat_i2c4'], str(root / 'dev/i2c-7'))
            self.assertTrue(found['pwm8_pin16'].endswith('pwmchip3'))
            self.assertTrue(found['pwm9_pin18'].endswith('pwmchip0'))
            self.assertIsNone(found['extension_uart9'])

    def test_backup_before_replace_and_preserve_permissions(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            file = root / 'armbianEnv.txt'
            file.write_bytes(b'console=both\n')
            file.chmod(0o640)
            backup = root / 'backups'
            self.assertTrue(hat.write_changed(file, b'console=both\nuser_overlays=new\n', backup))
            saved = backup / file.relative_to('/')
            self.assertEqual(saved.read_bytes(), b'console=both\n')
            self.assertEqual(file.stat().st_mode & 0o777, 0o640)

    def test_unchanged_config_does_not_create_backup(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            file = root / 'armbianEnv.txt'
            file.write_bytes(b'same\n')
            backup = root / 'backups'
            self.assertFalse(hat.write_changed(file, b'same\n', backup))
            self.assertFalse(backup.exists())


class AudioTests(unittest.TestCase):
    def test_speaker_mixer_default_volume_and_remutes_on_failure(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            amixer = root / 'amixer'
            amixer.write_text('#!/bin/sh\n'
                              'printf "%s\\n" "$*" >> "$MIXER_LOG"\n'
                              'if [ "${FAIL_DAC:-}" = yes ] && '
                              '[ "$4" = "name=Line DAC Playback Volume" ]; then exit 1; fi\n')
            amixer.chmod(0o755)
            log = root / 'calls.log'
            env = {**os.environ, 'PATH': str(root) + os.pathsep + os.environ['PATH'],
                   'MIXER_LOG': str(log)}
            script = ROOT / 'deploy/audio/aic3104-speaker-init.sh'
            result = subprocess.run(['/bin/sh', str(script)], env=env, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            calls = log.read_text()
            self.assertIn('name=PCM Playback Volume 102,102', calls)
            self.assertIn('name=Line Playback Volume 0,0', calls)
            self.assertNotIn('Capture', calls)
            self.assertNotIn('PGA', calls)
            self.assertTrue(calls.rstrip().endswith('name=Line Playback Switch on,on'))
            log.write_text('')
            result = subprocess.run(['/bin/sh', str(script)], env={**env, 'FAIL_DAC': 'yes'}, capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            calls = log.read_text()
            self.assertTrue(calls.rstrip().endswith('name=Line Playback Switch off,off'))
            self.assertNotIn('name=Line Playback Switch on,on', calls)

    def test_apply_enables_service_without_starting_it_or_playing_audio(self):
        def execute(*args, **kwargs):
            return 10 if '--apply' in args else 0
        with patch('sys.argv', ['configure_audio.py', '--apply']), \
             patch.object(audio, 'check_target'), patch.object(audio, 'require_tools'), \
             patch.object(audio, 'call', side_effect=execute) as commands, \
             patch.object(audio, 'module_available', return_value=True), \
             patch.object(audio.subprocess, 'run', return_value=subprocess.CompletedProcess([], 1)), \
             patch.object(hat.grp, 'getgrnam'), patch.object(hat, 'write_changed'), \
             redirect_stdout(StringIO()):
            self.assertEqual(audio.main(), 0)
            invoked = [item.args for item in commands.call_args_list]
            self.assertIn(('systemctl', 'enable', audio.UNIT), invoked)
            self.assertFalse(any('start' in row or '--now' in row or 'aplay' in row for row in invoked))

    def test_audio_plan_does_not_change_system_or_build_driver(self):
        with patch('sys.argv', ['configure_audio.py', '--plan']), \
             patch.object(audio, 'require_tools'), patch.object(audio, 'call') as commands, \
             patch.object(audio, 'module_available', return_value=False), \
             patch.object(hat, 'write_changed') as writes, \
             patch.object(audio, 'install_driver') as driver, redirect_stdout(StringIO()):
            self.assertEqual(audio.main(), 0)
            writes.assert_not_called()
            driver.assert_not_called()
            commands.assert_called_once()
            self.assertIn('--plan', commands.call_args.args)
            self.assertIn('external-12mhz', commands.call_args.args)

    def test_apply_refuses_missing_codec_driver_before_writes(self):
        with patch('sys.argv', ['configure_audio.py', '--apply']), \
             patch.object(audio, 'check_target'), patch.object(audio, 'require_tools'), \
             patch.object(audio, 'call'), patch.object(audio, 'module_available', return_value=False), \
             patch.object(hat, 'write_changed') as writes, redirect_stdout(StringIO()):
            with self.assertRaisesRegex(ValueError, 'codec modules missing'):
                audio.main()
            writes.assert_not_called()

    def test_driver_build_refuses_missing_matching_headers_before_copy_or_dkms(self):
        with patch.object(audio, 'check_target'), patch.object(audio, 'require_tools'), \
             patch.object(audio, 'kernel_headers', side_effect=ValueError('exact headers missing')), \
             patch.object(audio.shutil, 'copyfile') as copies, patch.object(audio, 'call') as commands:
            with self.assertRaisesRegex(ValueError, 'exact headers'):
                audio.install_driver()
            copies.assert_not_called()
            commands.assert_not_called()

    def test_audio_verification_uses_i2c4_of_identity_and_bound_codec(self):
        with tempfile.TemporaryDirectory(dir=ROOT) as tmp:
            root = Path(tmp)
            sys_root = root / 'sys'
            node = sys_root / 'firmware/devicetree/base/i2c@fe5d0000/codec@18'
            node.mkdir(parents=True)
            codec = sys_root / 'bus/i2c/devices/7-0018'
            codec.mkdir(parents=True)
            (codec / 'of_node').symlink_to(node)
            driver = sys_root / 'bus/i2c/drivers/tlv320aic3x'
            driver.mkdir(parents=True)
            (codec / 'driver').symlink_to(driver)
            card_node = sys_root / 'firmware/devicetree/base/sound-aic3104'
            card_node.mkdir()
            card_platform = sys_root / 'devices/platform/sound-aic3104'
            card_platform.mkdir(parents=True)
            (card_platform / 'of_node').symlink_to(card_node)
            card = sys_root / 'class/sound/card3'
            card.mkdir(parents=True)
            (card / 'device').symlink_to(card_platform)
            (card / 'id').write_text('aic3104\n')
            status = audio.audio_status(sys_root, root / 'proc')
            self.assertEqual(status['alsa_cards'], ['card3'])
            self.assertEqual(status['i2c4_bound_codecs'], ['7-0018'])
            self.assertFalse(status['playback_tested'])
            (codec / 'driver').unlink()
            self.assertFalse(audio.audio_status(sys_root, root / 'proc')['i2c4_bound_codecs'])


class ProfileSourceTests(unittest.TestCase):
    def test_mux_values_match_pinned_armbian_pinctrl(self):
        reference = (ROOT / 'provenance/reference/rk3568-pinctrl.dtsi').read_text()
        checks = {
            'oc_i2c4m0': ('openchicken-z3w-a1.dts', ['i2c4m0_xfer']),
            'oc_uart5m1': ('openchicken-z3w-a1.dts', ['uart5m1_xfer']),
            'oc_pwm8m0': ('openchicken-z3w-a1.dts', ['pwm8m0_pins']),
            'oc_pwm9m0': ('openchicken-z3w-a1.dts', ['pwm9m0_pins']),
            'oc_i2s3m0': ('openchicken-z3w-a1.dts', ['i2s3m0_sclk', 'i2s3m0_lrck', 'i2s3m0_sdi', 'i2s3m0_sdo']),
            'oc_spi3m1': ('openchicken-z3w-a1-ext-spi3.dts', ['spi3m1_pins', 'spi3m1_cs0']),
            'oc_uart9m1': ('openchicken-z3w-a1-ext-uart9.dts', ['uart9m1_xfer']),
        }
        def block(text, label):
            return re.search(r'\b' + label + r':\s*[^\{]+\{(.*?)\};', text, re.S)[1]
        for label, (filename, upstream_labels) in checks.items():
            actual_text = block((MODULE.parent / filename).read_text(), label)
            actual = {tuple(map(int, row)) for row in re.findall(r'<(\d+)\s+(\d+)\s+(\d+)\s+&', actual_text)}
            expected = set()
            for upstream_label in upstream_labels:
                for bank, group, pin, mux in re.findall(r'<(\d+)\s+RK_P([A-D])([0-7])\s+(\d+)\s+&', block(reference, upstream_label)):
                    expected.add((int(bank), (ord(group) - ord('A')) * 8 + int(pin), int(mux)))
            with self.subTest(label=label):
                self.assertTrue(expected)
                self.assertEqual(actual, expected)

    def test_config_template_uses_stable_alias(self):
        source = (ROOT / 'deploy/robotd.toml').read_text()
        self.assertIn('port = "/dev/openchicken-dxl"', source)
        self.assertNotIn('port = "/dev/ttyS2"', source)

    def test_release_install_stops_before_bootstrap(self):
        script = (ROOT / 'scripts/install.sh').read_text()
        main = script[script.index('\nmain() {'):]
        self.assertLess(main.index('die "OpenChicken'), main.index('bootstrap_first_release'))
        self.assertNotIn('fetch "${config_raw}/deploy/robotd.toml"', script)

    def test_local_provisioning_does_not_refetch_official_profile(self):
        board = (ROOT / 'scripts/provision.sh').read_text()
        host = (ROOT / 'scripts/provision-board.sh').read_text()
        self.assertIn('exec sh "$SCRIPT_DIR/setup-board.sh"', board)
        for source in (board, host):
            self.assertNotIn('curl ', source)
            self.assertNotIn('raw.githubusercontent.com', source)
            self.assertNotIn('sudo ', source)


if __name__ == '__main__':
    unittest.main(verbosity=2)
