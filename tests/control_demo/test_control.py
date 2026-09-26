from argparse import Namespace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from argus import control
from argus.platforms import device_session as ds
from argus.runtime.resources import VisualResource
from argus.runtime.schema import validate


def args(**kwargs):
    return Namespace(**dict({'session':'test','platform':'browser','backend':None,'device':None,
        'server_url':None,'team_id':None,'app':None,'bridge_directory':None,'page_id':None},**kwargs))


class ControlTests(unittest.TestCase):
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
