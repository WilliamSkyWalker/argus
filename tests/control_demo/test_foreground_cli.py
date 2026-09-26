"""Exercise the public command entry point without touching real applications."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, call, patch
from PIL import Image
from argus.platforms import device_session as ds
from argus.cli import main


class ForegroundCLITests(unittest.TestCase):
    def run_command(self, command, controller, expect_error=False):
        state = {'kind': 'desktop', 'os': 'windows', 'app': 'Example'}
        output = io.StringIO()
        with patch.object(sys, 'argv', ['argus', 'device', *command]), \
             patch.object(ds, 'load_state', return_value=state), \
             patch.object(ds, 'attach_desktop', return_value=controller) as attach, \
             patch('argus.devices.service.time.sleep'), \
             patch('argus.devices.observations.wait', return_value={'condition_met':True,'timed_out':False}), contextlib.redirect_stdout(output):
            try:
                main()
            except SystemExit:
                if not expect_error:
                    raise
        return json.loads(output.getvalue()), attach

    def controller(self):
        from argus.platforms.windows_runner import WindowsRunnerPlatform
        controller = Mock(spec=WindowsRunnerPlatform)
        png = io.BytesIO()
        Image.new('RGB', (20, 20)).save(png, format='PNG')
        controller.screenshot_raw.return_value = png.getvalue()
        controller.screen_size = (20, 20)
        controller.scale = 1
        controller.platform_name = "windows"
        return controller

    def test_prepare_replace_never_submits_and_passes_session_binding(self):
        p = self.controller()
        with tempfile.TemporaryDirectory() as directory:
            result, attach = self.run_command([
                'type-send', 'Example message', '--session', 'example', '--foreground',
                '--prepare-only', '--replace', '--input-x', '4', '--input-y', '5',
                '--out', str(Path(directory) / 'draft.png')], p)
        self.assertFalse(result['submitted'])
        self.assertTrue(result['requires_observation'])
        inputs = [c for c in p.method_calls if c[0] != 'screenshot_raw']
        self.assertEqual(inputs, [call.tap(4, 5), call.press_key('ctrl+a'),
            call.input_text('Example message'), call.teardown()])
        self.assertEqual(attach.call_args.kwargs, {'serial': 'example', 'foreground': True})

    def test_send_reports_dispatch_not_verified_delivery(self):
        p = self.controller()
        with tempfile.TemporaryDirectory() as directory:
            result, _ = self.run_command([
                'type-send', 'Example', '--session', 'example', '--input-x', '4', '--input-y', '5',
                '--send-x', '8', '--send-y', '9', '--wait-s', '0',
                '--out', str(Path(directory) / 'sent.png')], p)
        self.assertTrue(result['submitted'])
        self.assertTrue(result['requires_observation'])
        self.assertNotIn('sent', result)
        self.assertEqual(p.tap.call_args_list, [call(4, 5), call(8, 9)])

    def test_missing_send_coordinates_fails_before_attachment(self):
        p = self.controller()
        with self.assertRaises(SystemExit):
            self.run_command(['type-send', 'Example', '--input-x', '4', '--input-y', '5'], p)
        p.tap.assert_not_called()

    def test_tap_and_focus_return_screenshots_via_same_entry(self):
        for command in (['focus'], ['tap', '4', '5', '--foreground']):
            p = self.controller()
            with tempfile.TemporaryDirectory() as directory:
                result, attach = self.run_command(command + ['--session', 'example', '--out', str(Path(directory) / 'view.png')], p)
            self.assertGreaterEqual(p.screenshot_raw.call_count, 1)
            self.assertEqual(attach.call_args.kwargs, {'serial': 'example', 'foreground': True})
            self.assertEqual(result['width'], 20)

    def test_capture_failure_after_send_reports_uncertain_submission(self):
        p = self.controller()
        p.screenshot_raw.side_effect = [p.screenshot_raw.return_value, RuntimeError('capture unavailable'), RuntimeError('capture unavailable')]
        result, _ = self.run_command([
            'type-send', 'Example', '--session', 'example', '--input-x', '4', '--input-y', '5',
            '--send-x', '8', '--send-y', '9', '--wait-s', '0'], p, expect_error=True)
        self.assertFalse(result['ok'])
        self.assertTrue(result['submission_attempted'])
        self.assertTrue(result['requires_observation'])
        self.assertEqual(p.tap.call_args_list, [call(4, 5), call(8, 9)])
