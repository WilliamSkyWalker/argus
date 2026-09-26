"""Default client installation must authorize only Argus and preserve user settings."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from test_plugin_install import installer, ROOT


class ClientInstallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.market = installer.prepare_plugin(ROOT, self.root/'managed', self.root/'python', 'test')
        self.directory = self.root/'client'
        self.patch = patch.object(installer, 'client_directory', return_value=self.directory)
        self.patch.start(); self.addCleanup(self.patch.stop)

    def test_claude_install_defaults_to_scoped_allow_and_uninstall_preserves_existing(self):
        original = {'permissions':{'allow':['Read', 'mcp__argus__*'], 'deny':['Bash(rm *)']}, 'theme':'dark'}
        installer.write_json(self.directory/'settings.json', original)
        with patch.object(installer, 'run'):
            installer.install_client('claude', self.market)
        value = json.loads((self.directory/'settings.json').read_text())
        self.assertIn('mcp__plugin_argus-device_argus__*', value['permissions']['allow'])
        self.assertNotIn('*', value['permissions']['allow'])
        installer.configure_client('claude', self.market)
        installer.unconfigure_client('claude', self.market)
        self.assertEqual(json.loads((self.directory/'settings.json').read_text()), original)

    def test_qoder_and_cn_share_runtime_skill_and_default_trust(self):
        for client in ('qoder', 'qodercn'):
            original = {'mcpServers':{'other':{'command':'keep'}}, 'permissions':{'deny':['Bash(rm *)']}}
            installer.write_json(self.directory/'settings.json', original)
            installer.install_client(client, self.market)
            value = json.loads((self.directory/'settings.json').read_text())
            self.assertTrue(value['mcpServers']['argus']['trust'])
            self.assertEqual(value['mcpServers']['argus']['command'],str(self.root/'python'))
            skill = self.directory/'skills/argus-device/SKILL.md'
            self.assertEqual(skill.read_bytes(), (ROOT/'plugins/argus-device/skills/device/SKILL.md').read_bytes())
            installer.configure_client(client, self.market)
            installer.unconfigure_client(client, self.market)
            self.assertEqual(json.loads((self.directory/'settings.json').read_text()), original)
            self.assertFalse(skill.exists())

    def test_independent_server_cannot_be_overwritten(self):
        original={'mcpServers':{'argus':{'command':'custom'}}}
        installer.write_json(self.directory/'settings.json',original)
        with self.assertRaisesRegex(ValueError,'independently configured'):
            installer.configure_client('qoder',self.market)
        self.assertEqual(json.loads((self.directory/'settings.json').read_text()), original)

    def test_changed_skill_and_server_survive_uninstall(self):
        installer.configure_client('qoder',self.market)
        path=self.directory/'settings.json'; value=json.loads(path.read_text())
        value['mcpServers']['argus']['command']='user-customized'
        installer.write_json(path,value)
        skill=self.directory/'skills/argus-device/SKILL.md'; skill.write_text('custom')
        installer.unconfigure_client('qoder',self.market)
        self.assertEqual(json.loads(path.read_text())['mcpServers']['argus']['command'],'user-customized')
        self.assertEqual(skill.read_text(),'custom')

    def test_prepare_only_does_not_configure_clients(self):
        with patch.object(installer,'prepare_runtime',return_value=self.root/'python'), patch.object(installer,'install_client') as install:
            installer.main(['--client','all','--prepare-only','--root',str(self.root/'prepared'),'--source',str(ROOT)])
        install.assert_not_called()
        self.assertFalse(self.directory.exists())
