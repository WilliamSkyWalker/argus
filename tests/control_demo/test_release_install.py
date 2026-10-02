"""Distribution integrity and browser setup contracts."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from saygo.integrations import browser_bridge, browser_setup

ROOT = Path(__file__).resolve().parents[2]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


class ReleaseTests(unittest.TestCase):
    def test_release_roundtrip_and_corruption_rejection(self):
        builder = module('builder', ROOT/'scripts/build_release.py')
        with tempfile.TemporaryDirectory() as tmp:
            out = builder.build(Path(tmp)/'release')
            version = json.loads((out/'release-manifest.json').read_text())['version']
            installer = module('release_installer', out/f'install-saygo-{version}.py')
            archive = out/f'saygo-{version}.zip'
            with zipfile.ZipFile(archive) as bundle:
                self.assertFalse(any('/.env' in n or '/.saygo/' in n or '__pycache__' in n for n in bundle.namelist()))
            work = Path(tmp)/'extract'; work.mkdir()
            source = installer.source_root(None, work, str(archive))
            self.assertTrue((source/'saygo/integrations/browser_setup.py').is_file())
            plugin = installer.prepare_plugin(source, Path(tmp)/'managed', Path('/fake/python'), 'test')
            self.assertTrue((plugin/'plugins/saygo-device/.mcp.json').is_file())
            archive.write_bytes(archive.read_bytes()+b'corrupted')
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'):
                installer.source_root(None, work, str(archive))
            extension_version = json.loads((ROOT/'extensions/saygo-browser/manifest.json').read_text())['version']
            self.assertEqual(json.loads((out/'release-manifest.json').read_text())['extension_version'], extension_version)
            with zipfile.ZipFile(out/f'saygo-browser-{extension_version}-development.zip') as dev, zipfile.ZipFile(out/f'saygo-browser-{extension_version}-store.zip') as store:
                import base64
                manifest = json.loads(dev.read('manifest.json'))
                key = base64.b64decode(manifest['key'])
                eid = ''.join(chr(97+int(c,16)) for c in hashlib.sha256(key).hexdigest()[:32])
                self.assertEqual(eid, json.loads((out/'release-manifest.json').read_text())['development_extension_id'])
                self.assertNotIn('key', json.loads(store.read('manifest.json')))

    def test_bridge_uninstall_does_not_remove_another_installation(self):
        with tempfile.TemporaryDirectory() as tmp, patch('pathlib.Path.home', return_value=Path(tmp)), patch.object(browser_bridge.sys, 'platform', 'linux'):
            first, second = Path(tmp)/'first', Path(tmp)/'second'
            browser_bridge.install(first, 'a'*32, 'chrome')
            browser_bridge.install(second, 'b'*32, 'chrome')
            with self.assertRaisesRegex(RuntimeError, 'another installation'):
                browser_bridge.uninstall(first, 'chrome')
            browser_bridge.uninstall(second, 'chrome')
            self.assertTrue((second/'host.sh').exists())
            self.assertFalse((Path(tmp)/'.config/google-chrome/NativeMessagingHosts/com.saygo.browser.json').exists())

    def test_invalid_identity_has_no_setup_side_effects(self):
        with patch('saygo.devices.mobile_host.is_wsl') as wsl:
            with self.assertRaises(ValueError):
                browser_setup.setup('invalid')
            wsl.assert_not_called()

    def test_protocol_mismatch_never_enqueues_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            browser_bridge.atomic_json(Path(tmp)/'status.json', {'connected':True, 'protocol':999})
            with self.assertRaisesRegex(RuntimeError, 'protocol mismatch'):
                browser_bridge.Client(tmp).call('tap', x=10, y=20)
            self.assertEqual(list(Path(tmp).glob('*.request')), [])

    def test_default_bridge_is_resolved_before_resource_lock(self):
        from types import SimpleNamespace
        from saygo.devices import control
        from saygo.platforms import device_session as ds
        with tempfile.TemporaryDirectory() as tmp, patch.object(ds, 'STATE_DIR', Path(tmp)/'device-sessions'), patch.object(ds, 'load_state', return_value=None):
            directory = str(Path(tmp)/'bridge')
            browser_bridge.atomic_json(Path(tmp)/'browser-bridge.json', {'directory':directory})
            args = SimpleNamespace(session='web', platform='browser', backend='extension', bridge_directory=None)
            with patch('saygo.runtime.locking.resource_guard') as guard, patch.object(control, '_connect', return_value={'connected':True}):
                self.assertTrue(control.connect(args)['connected'])
                self.assertIn('browser-extension:'+directory, guard.call_args.args[0])
                self.assertEqual(args.bridge_directory, directory)


if __name__ == '__main__':
    unittest.main()
