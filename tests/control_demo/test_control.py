from argparse import Namespace
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from saygo.devices import control
from saygo.platforms import device_session as ds
from saygo.runtime.resources import VisualResource
from saygo.runtime.schema import validate


def args(**kwargs):
    return Namespace(**dict({'session':'test','platform':'browser','backend':None,'device':None,
        'server_url':None,'team_id':None,'app':None,'bridge_directory':None,'page_id':None},**kwargs))


class ControlTests(unittest.TestCase):
    def test_removed_entry_points_are_rejected(self):
        for module, command in [('saygo.cli', 'mobile'), ('saygo.cli', 'devices'),
                                ('saygo.integrations.browser_bridge', 'bind')]:
            with self.subTest(command=command):
                result = subprocess.run([sys.executable, '-m', module, command],
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn('invalid choice', result.stderr)

    def test_unified_cli_routes_mobile_discovery_and_boot(self):
        from saygo.cli import main
        for command, method, response in [
            (['list', '--platform', 'android'], 'discover', {'devices': []}),
            (['boot', '--platform', 'android', '--host', 'local', 'Example'], 'boot_device', {'state': 'ready'}),
        ]:
            with self.subTest(command=command), patch.object(control.mobile, method, return_value=response) as call:
                output = io.StringIO()
                with patch.object(sys, 'argv', ['saygo', 'device', *command]), contextlib.redirect_stdout(output):
                    main()
                call.assert_called_once()
                data = json.loads(output.getvalue())
                for key, value in response.items():
                    self.assertEqual(data[key], value)

    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.patch=patch.object(ds,'STATE_DIR',Path(self.tmp.name))
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_desktop_connection_persists_and_releases_controller(self):
        p=Mock();p.screen_size=(800,600)
        with patch.object(ds,'attach_desktop',return_value=p):
            result=control.connect(args(platform='windows',app='Example'))
        self.assertTrue(result['connected'])
        self.assertEqual(ds.load_state('test')['app'],'Example')
        p.teardown.assert_called_once()

    def test_mobile_delegates_identity(self):
        with patch.object(control.mobile,'connect',return_value={}) as connect:
            control.connect(args(platform='android',device='DEVICE_ID'))
        self.assertEqual(connect.call_args.args[:3],('android','DEVICE_ID','test'))

    def test_other_platform_cannot_replace_session(self):
        ds.save_state('test',{'kind':'browser'})
        with self.assertRaisesRegex(ValueError,'another platform'):
            control.connect(args(platform='windows',app='Example'))
        self.assertEqual(ds.load_state('test'),{'kind':'browser'})

    def test_disconnect_preserves_browser_and_blocks_attachment(self):
        ds.save_state('test',{'kind':'browser','browser_backend':'playwright','debugger_address':'127.0.0.1:9000'})
        with patch.object(ds,'stop') as stop:
            control.disconnect('test')
            stop.assert_not_called()
        with self.assertRaisesRegex(RuntimeError,'disconnected'):
            ds.attach_browser('test')
        self.assertIn('debugger_address',ds.load_state('test'))

    def test_failed_reconnect_restores_disconnected_marker(self):
        state={'kind':'browser','browser_backend':'playwright','disconnected':True}
        ds.save_state('test',state)
        with patch.object(ds,'attach_browser',side_effect=RuntimeError('offline')):
            with self.assertRaises(RuntimeError): control.connect(args())
        self.assertEqual(ds.load_state('test'),state)

    def test_successful_reconnect_does_not_launch_browser(self):
        ds.save_state('test',{'kind':'browser','browser_backend':'playwright','disconnected':True})
        p=Mock();p.list_pages.return_value=[];p.page_id=None
        with patch.object(ds,'attach_browser',return_value=p),patch.object(ds,'start') as start:
            self.assertTrue(control.connect(args())['connected'])
            start.assert_not_called()
        self.assertFalse(ds.load_state('test').get('disconnected'))

    def test_runtime_desktop_session_and_global_lock_compatible(self):
        spec={'kind':'windows','session':'test'}
        validate({'version':1,'resources':{'desktop':spec},'steps':[{'id':'shot','kind':'observe','resource':'desktop'}]})
        ds.save_state('test',{'kind':'desktop','os':'windows','app':'Example'})
        p=Mock()
        with patch.object(ds,'attach_desktop',return_value=p):
            self.assertIs(VisualResource(spec)._attach(),p)
        control.disconnect('test')
        with self.assertRaisesRegex(RuntimeError,'disconnected'):
            VisualResource(spec)._attach()

    def test_session_listing_is_not_a_live_probe(self):
        ds.save_state('test',{'kind':'desktop','os':'windows','app':'Example'})
        with patch.object(ds,'attach') as attach:
            self.assertEqual(control.sessions()[0]['platform'],'windows')
            attach.assert_not_called()

    def test_browser_discovery_tolerates_corrupt_session(self):
        (ds.STATE_DIR / 'broken.json').write_text('{')
        ds.save_state('web', {'kind': 'browser'})
        result = control.discover('browser')
        self.assertEqual([s['session'] for s in result['browsers']], ['web'])
        self.assertIn('error', result['sessions'][0])

    def test_release_preserves_persistent_sessions(self):
        for kind in ('android', 'ios', 'browser', 'mac', 'windows'):
            with self.subTest(kind=kind):
                controller = Mock(spec=['platform_name', '_driver', 'teardown'])
                controller.platform_name = kind
                ds.release_controller(controller)
                controller._driver.quit.assert_not_called()
                self.assertEqual(controller.teardown.call_count, int(kind in {'mac', 'windows'}))
                self.assertEqual(controller._driver.service.stop.call_count, int(kind == 'browser'))
        controller = Mock(spec=['disconnect', 'teardown'])
        ds.release_controller(controller, 'browser')
        controller.disconnect.assert_called_once()
        controller.teardown.assert_not_called()

    def test_cli_releases_selenium_service_after_failed_action(self):
        from saygo.commands.device import cmd_device
        ds.save_state('test', {'kind': 'browser'})
        controller = Mock(spec=['platform_name', '_driver', 'press_key'])
        controller.platform_name = 'browser'
        controller.press_key.side_effect = RuntimeError('input failed')
        with patch.object(ds, 'attach_browser', return_value=controller), contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit):
                cmd_device(Namespace(device_command='key', serial='test', key='enter'))
        controller._driver.service.stop.assert_called_once()
        controller._driver.quit.assert_not_called()

    def test_inline_auto_report_does_not_write_sentinel_filename(self):
        from saygo.commands import run as cli
        with patch.object(cli, 'load_config', return_value={'llm': {'api_key': 'placeholder'}}), \
                patch.object(cli, '_resolve_test_target', return_value=(['example'], None)), \
                patch.object(cli, '_load_preconditions', return_value=''), \
                patch.object(cli, '_load_accounts', return_value=[]), \
                patch('saygo.qa.execution._run_sequential', return_value=[]), \
                patch.object(cli, '_probes_mode', return_value=''), \
                patch.dict(cli.os.environ, {'SAYGO_SHARD': ''}), \
                patch('saygo.qa.report.save_html') as html, \
                patch('saygo.qa.report.save_json') as json_report, contextlib.redirect_stdout(io.StringIO()):
            cli.cmd_run('example', report_path='__auto__')
            html.assert_not_called()
            json_report.assert_not_called()

    def test_new_window_is_explicit_and_one_shot(self):
        p=Mock(); p.screen_size=(800,600)
        p.connection={'process_id':42,'process_name':'Example','new_window':True}
        with patch.object(ds,'attach_desktop',return_value=p) as attach:
            with self.assertRaisesRegex(ValueError,'new-window-arg'):
                control.connect(args(platform='windows',app='Example',new_window=True))
            attach.assert_not_called()
            control.connect(args(platform='windows',app='Example',new_window=True,
                launch='example.exe',new_window_args=['--new-window']))
        state=ds.load_state('test')
        self.assertNotIn('new_window',state)
        self.assertNotIn('new_window_args',state)
        self.assertEqual(state['process_id'],42)

    def test_handoff_blocks_until_resume_observes_again(self):
        from saygo.platforms.desktop import DesktopHandoffRequired
        ds.save_state('test',{'kind':'desktop','os':'windows','app':'Example'})
        result=control.handoff(Namespace(session='test',reason='login',instructions='Please log in'))
        self.assertEqual(result['status'],'waiting_for_human')
        with patch('saygo.platforms.create_platform') as create:
            with self.assertRaises(DesktopHandoffRequired): ds.attach_desktop(ds.load_state('test'))
            create.assert_not_called()
        self.assertFalse(control.connect(args(platform='windows',app='Example'))['connected'])
        p=Mock(); p.screenshot_raw.side_effect=RuntimeError('capture failed')
        with patch.object(ds,'attach_desktop',return_value=p):
            with self.assertRaisesRegex(RuntimeError,'capture failed'):
                control.resume(Namespace(session='test',note='Logged in'))
        self.assertIn('handoff',ds.load_state('test'))
        from PIL import Image
        picture = io.BytesIO()
        Image.new('RGB', (20, 20)).save(picture, format='PNG')
        p.screenshot_raw.side_effect=None; p.screenshot_raw.return_value=picture.getvalue()
        p.screen_size = (20, 20); p.scale = 1
        p.observation_metadata.return_value = {}
        del p.page_id
        with patch.object(ds,'attach_desktop',return_value=p),patch.object(control.mobile,'home',return_value=Path(self.tmp.name)):
            result=control.resume(Namespace(session='test',note='Logged in'))
        self.assertEqual(result['status'],'needs_observation')
        self.assertEqual(Path(result['path']).read_bytes(),picture.getvalue())
        self.assertNotIn('handoff',ds.load_state('test'))

    def test_running_app_without_window_requests_human_instead_of_retry(self):
        from saygo.platforms.desktop import DesktopHandoffRequired
        details={'status':'waiting_for_human','reason':'window_unavailable','instructions':'Restore the app'}
        with patch.object(ds,'attach_desktop',side_effect=DesktopHandoffRequired(details)) as attach:
            result=control.connect(args(platform='windows',app='Example'))
        attach.assert_called_once()
        self.assertEqual(result['status'],'waiting_for_human')
        self.assertEqual(ds.load_state('test')['handoff'],details)
