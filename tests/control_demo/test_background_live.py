"""Opt-in, isolated Windows GUI test; no screenshot is sent to an LLM."""
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest

from PIL import Image

from argus.platforms.windows_runner import WindowsRunnerPlatform
from argus.devices.toolchain import background_options


@unittest.skipUnless(os.environ.get('ARGUS_TEST_WINDOWS') == '1',
                     'set ARGUS_TEST_WINDOWS=1 on Windows or WSL with an unlocked desktop')
class BackgroundLiveTests(unittest.TestCase):
    def test_occluded_window_input_and_reconnection(self):
        self._exercise()

    @unittest.skipUnless(os.environ.get('ARGUS_TEST_WINDOWS_FOREGROUND') == '1',
                         'foreground tests require a separate explicit opt-in')
    def test_explicit_foreground_actions(self):
        self._exercise(foreground=True)

    def _exercise(self, foreground=False):
        def windows_path(path):
            if os.name == 'nt':
                return str(path)
            return subprocess.check_output(['wslpath', '-w', str(path)], text=True).strip()

        self.assertTrue(shutil.which('powershell.exe'))
        with tempfile.TemporaryDirectory(prefix='argus-background-') as directory:
            state_path = Path(directory) / 'state.json'
            fixture = subprocess.Popen([
                'powershell.exe', '-NoProfile', '-NonInteractive', '-STA', '-WindowStyle', 'Hidden',
                '-ExecutionPolicy', 'Bypass', '-File',
                windows_path(Path(__file__).with_name('background_fixture.ps1')),
                '-StateFile', windows_path(state_path),
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                **background_options())
            runner = WindowsRunnerPlatform()

            def read():
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        return json.loads(state_path.read_text(encoding='utf-8-sig'))
                    except (FileNotFoundError, ValueError):
                        time.sleep(.05)
                self.fail('Fixture did not publish state')

            background_phase = True

            def check(**expected):
                deadline = time.monotonic() + 3
                while time.monotonic() < deadline:
                    current = read()
                    if background_phase:
                        self.assertEqual(current['activations'], before['activations'], current)
                    if all(current[key] == value for key, value in expected.items()):
                        return current
                    time.sleep(.05)
                self.fail(f'Expected {expected}, got {current}')

            try:
                read()
                time.sleep(.5)
                before = read()
                self.assertNotEqual(before['foreground'], before['target'])
                self.assertEqual(before['activations'], 0, before)
                self.assertNotEqual(before['foreground'], before['cover'], before)
                config = {'win': {'app': 'Argus Background Fixture', 'background': True,
                                  'process_name': 'powershell', 'process_id': before['pid']}}
                runner.setup(config)
                check()
                picture = Image.open(io.BytesIO(runner.screenshot_raw()))
                self.assertEqual(picture.getpixel((400, 250))[:3], (173, 216, 230))
                metadata = runner.observation_metadata()
                self.assertEqual(metadata['window_id'], str(before['target']))
                self.assertEqual(metadata['process_id'], before['pid'])
                self.assertEqual(metadata['window_bounds'][2:], list(runner.screen_size))
                for stale in (dict(metadata, window_id='0'),
                              dict(metadata, process_id=0),
                              dict(metadata, window_bounds=[0,0,1,1])):
                    runner.expect_window(stale)
                    with self.assertRaisesRegex(RuntimeError, 'identity or bounds changed'):
                        runner.tap(*before['button'])
                    check(clicks=0)
                runner.expect_window(metadata)

                # Wrong/stale focus must never guess the first text field.
                with self.assertRaisesRegex(RuntimeError, 'tap the intended field first'):
                    runner.input_text('wrong field')
                runner.tap(*before['edit'])
                runner.input_text('first')
                check(first='first', second='')
                runner.press_key('ctrl+a')
                runner.input_text('中文😀')
                check(first='中文😀', second='')
                runner.tap(*before['other'])
                runner.input_text('second')
                check(first='中文😀', second='second')
                binding = runner.input_binding
                runner.teardown()
                runner = WindowsRunnerPlatform()
                config['win']['input_binding'] = binding
                runner.setup(config)
                runner.input_text('-reconnected')
                check(first='中文😀', second='second-reconnected')
                with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                    runner.press_key('ctrl+s')
                runner.tap(*before['button'])
                check(clicks=1, second='second-reconnected')
                runner.tap(*before['panel'])
                after = check(point='37,29')
                observations = {key + '_unchanged': after[key] == before[key]
                                for key in ('foreground', 'cursor', 'clipboard')}
                print('Background observations:', observations, flush=True)
                if os.environ.get('ARGUS_TEST_WINDOWS_IDLE') == '1':
                    self.assertTrue(all(observations.values()), observations)
                self.assertNotEqual(after['foreground'], after['cover'], after)
                runner.teardown()
                runner = WindowsRunnerPlatform()
                config['win']['input_binding'] = dict(binding, target=before['cover'])
                runner.setup(config)
                with self.assertRaisesRegex(RuntimeError, 'tap the intended field first'):
                    runner.input_text('wrong window')
                runner.teardown()
                if not foreground:
                    return
                background_phase = False
                config['win']['foreground'] = True
                runner = WindowsRunnerPlatform()
                runner.setup(config)
                with self.assertRaisesRegex(RuntimeError, 'covered by another window'):
                    runner.tap(*before['edit'])
                Path(str(state_path) + '.foreground').touch()
                check(cover_visible=False)
                runner.tap(*before['edit'])
                runner.press_key('ctrl+a')
                time.sleep(.2)
                runner.input_text('前台🙂+{}')
                check(first='前台🙂+{}', second='second-reconnected')
                runner.tap(*before['button'])
                check(clicks=2)
                image = Image.open(io.BytesIO(runner.screenshot_raw()))
                self.assertEqual(image.getpixel((400, 250))[:3], (173, 216, 230))
                self.assertEqual(read()['clipboard'], before['clipboard'])
            finally:
                runner.teardown()
                Path(str(state_path) + '.stop').touch()
                try:
                    fixture.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    fixture.kill()
                    fixture.wait(timeout=5)
                fixture.stderr.close()
