"""Exercise shared desktop actions using native coordinate conversions."""
import unittest
from unittest.mock import Mock, call, patch

from argus.platforms.desktop_mac import DesktopMacPlatform
from argus.platforms.desktop_win import DesktopWinPlatform


class DesktopTests(unittest.TestCase):
    def controllers(self):
        for cls in (DesktopMacPlatform, DesktopWinPlatform):
            controller = cls()
            controller._pg = Mock()
            controller._win_x, controller._win_y = -100.6, 20.6
            controller._win_w, controller._win_h = 800, 600
            yield controller

    def test_native_coordinates_used_by_all_pointer_actions(self):
        for p in self.controllers():
            with self.subTest(platform=p.platform_name), patch('argus.platforms.desktop.time.sleep') as sleep:
                first = (-91, 41) if p.platform_name == 'mac' else (-90, 40)
                last = (-71, 61) if p.platform_name == 'mac' else (-70, 60)
                p.tap(10, 20)
                p.swipe(10, 20, 30, 40)
                p.long_press(10, 20, 0)
                self.assertEqual(p._pg.mock_calls, [call.click(*first), call.moveTo(*first),
                    call.dragTo(*last, duration=0.4, button='left'), call.mouseDown(*first), call.mouseUp(*first)])
                sleep.assert_called_once_with(0.1)

    def test_unicode_paste_uses_native_modifier_and_empty_input_is_noop(self):
        for p in self.controllers():
            with self.subTest(platform=p.platform_name), patch.object(p, '_set_clipboard') as clipboard:
                p.input_text('')
                clipboard.assert_not_called()
                p.input_text('测试')
                clipboard.assert_called_once_with('测试')
                p._pg.hotkey.assert_called_once_with('command' if p.platform_name == 'mac' else 'ctrl', 'v')
                p._pg.typewrite.assert_not_called()

    def test_clipboard_failure_uses_typing_fallback(self):
        for p in self.controllers():
            with self.subTest(platform=p.platform_name), patch.object(p, '_set_clipboard', side_effect=OSError('unavailable')):
                p.input_text('example')
                p._pg.typewrite.assert_called_once_with('example', interval=0.02)
                p._pg.hotkey.assert_not_called()

    def test_keys_scroll_and_window_bounds(self):
        for p in self.controllers():
            with self.subTest(platform=p.platform_name), patch.object(p, 'long_press') as press:
                p.press_key(' RETURN ')
                p.press_key('delete')
                p.press_key('unknown')
                self.assertEqual(p._pg.press.call_args_list, [call('enter'), call('backspace')])
                p.scroll_up(); p.scroll_down()
                self.assertEqual(p._pg.scroll.call_args_list, [call(5), call(-5)])
                p._handle_platform_action({'type': 'long_press', 'x': -1, 'y': 900, 'duration': 2})
                press.assert_called_once_with(0, 599, 2.0)
                self.assertEqual(p.screen_size, (800, 600))
                with self.assertRaisesRegex(ValueError, 'Unknown action'):
                    p._handle_platform_action({'type': 'unknown'})

    def test_native_screenshot_scale_is_preserved(self):
        for p in self.controllers():
            with self.subTest(platform=p.platform_name), patch.object(p, 'screenshot_raw', return_value=b'png'):
                p._last_shot_size = (1600, 1200)
                self.assertEqual(p.screenshot_png(), b'png')
                self.assertEqual(p.scale, 2.0 if p.platform_name == 'mac' else 1.0)

    def test_runner_scale_tracks_resized_screenshots(self):
        from argus.platforms.windows_runner import WindowsRunnerPlatform
        runner = WindowsRunnerPlatform()
        runner._size = (2560, 1440)
        self.assertEqual(runner.scale, .5)
        runner._size = (800, 600)
        self.assertEqual(runner.scale, 1.0)

    def test_background_factory_never_falls_back_to_foreground(self):
        from argus.platforms import create_platform
        from argus.platforms.windows_runner import WindowsRunnerPlatform
        with patch('platform.system', return_value='Windows'), patch('platform.release', return_value='10'):
            with patch('shutil.which', return_value='powershell.exe'):
                self.assertIsInstance(create_platform('desktop', {'win': {'background': True}}), WindowsRunnerPlatform)
            with patch('shutil.which', return_value=None):
                with self.assertRaisesRegex(RuntimeError, 'foreground fallback is disabled'):
                    create_platform('windows', {'win': {'background': True}})

    def test_background_binding_survives_separate_controllers(self):
        from argus.platforms import device_session as ds
        state = {'kind': 'desktop', 'os': 'windows', 'app': 'Example',
                 'background': True, 'process_id': 123}
        binding = {'window': 12, 'target': 13, 'process_id': 123, 'class_name': 'Edit'}
        with patch.object(ds, 'load_state', return_value=state.copy()), patch.object(ds, 'save_state') as save:
            with patch('argus.platforms.create_platform') as factory:
                ds.attach_desktop(state, serial='example')
                options = factory.call_args.args[1]['win']
                self.assertTrue(options['background'])
                options['_binding_callback'](binding)
                self.assertEqual(save.call_args.args[1]['input_binding'], binding)
                options['_binding_callback'](None)
                self.assertNotIn('input_binding', save.call_args.args[1])
