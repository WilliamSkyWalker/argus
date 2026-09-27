"""Default client installation must authorize only Saygo and preserve user settings."""
import json
import builtins
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

    def codex_policy(self):
        data = installer.parse_toml((self.directory/'config.toml').read_text())
        return data['plugins']['saygo-device@saygo-managed']['mcp_servers']['saygo']

    def test_codex_install_is_scoped_idempotent_and_uninstall_removes_owned_rule(self):
        self.directory.mkdir()
        path = self.directory/'config.toml'
        original = '# keep this comment\nmodel = "custom"\n[plugins."other@market"]\nenabled = true\n'
        path.write_text(original)
        with patch.object(installer, 'run'):
            installer.install_client('codex', self.market)
        self.assertEqual(self.codex_policy()['default_tools_approval_mode'], 'approve')
        installed = path.read_text()
        self.assertTrue(installed.startswith(original))
        self.assertEqual((self.directory/'config.toml.before-saygo').read_text(), original)
        installer.configure_client('codex', self.market)
        self.assertEqual(path.read_text(), installed)
        installer.unconfigure_client('codex', self.market)
        self.assertNotIn('default_tools_approval_mode', self.codex_policy())
        self.assertTrue(path.read_text().startswith(original))
        self.assertFalse((self.directory/'saygo-install.json').exists())

    def test_codex_explicit_policies_survive_install_and_uninstall(self):
        self.directory.mkdir()
        path = self.directory/'config.toml'
        for mode in ('approve', 'prompt', 'deny'):
            original = '[plugins."saygo-device@saygo-managed".mcp_servers.saygo]\ndefault_tools_approval_mode = "' + mode + '"\n'
            path.write_text(original)
            installer.configure_client('codex', self.market)
            installer.unconfigure_client('codex', self.market)
            self.assertEqual(path.read_text(), original)

    def test_codex_existing_server_and_tool_overrides_are_preserved(self):
        self.directory.mkdir()
        path = self.directory/'config.toml'
        original = "[plugins.'saygo-device@saygo-managed'.mcp_servers.saygo] # custom\nenabled = true\n[plugins.'saygo-device@saygo-managed'.mcp_servers.saygo.tools.device_action]\napproval_mode = 'prompt'\n"
        path.write_text(original)
        installer.configure_client('codex', self.market)
        policy = self.codex_policy()
        self.assertTrue(policy['enabled'])
        self.assertEqual(policy['tools']['device_action']['approval_mode'], 'prompt')
        installer.unconfigure_client('codex', self.market)
        self.assertEqual(path.read_text(), original)

    def test_codex_user_changes_survive_uninstall(self):
        installer.configure_client('codex', self.market)
        path = self.directory/'config.toml'
        changed = path.read_text().replace('"approve"', '"prompt"')
        path.write_text(changed)
        installer.unconfigure_client('codex', self.market)
        self.assertEqual(path.read_text(), changed)

    def test_codex_unsupported_inline_layout_is_not_overwritten(self):
        self.directory.mkdir()
        path = self.directory/'config.toml'
        original = '[plugins."saygo-device@saygo-managed"]\nmcp_servers = {saygo = {enabled = true}}\n'
        path.write_text(original)
        with self.assertRaisesRegex(ValueError, 'inline policy layout'):
            installer.configure_client('codex', self.market)
        self.assertEqual(path.read_text(), original)
        self.assertFalse((self.directory/'saygo-install.json').exists())

    def test_codex_python310_without_host_pip_uses_private_runtime(self):
        real_import = builtins.__import__
        def missing_parser(name, *args, **kwargs):
            if name in ('tomllib', 'pip._vendor'):
                raise ImportError(name)
            return real_import(name, *args, **kwargs)
        with patch('builtins.__import__', side_effect=missing_parser), patch.object(installer.subprocess, 'check_output', return_value='{"enabled": true}') as run:
            self.assertEqual(installer.parse_toml('enabled=true', self.market), {'enabled': True})
        self.assertEqual(run.call_args.args[0][0], str(self.root/'python'))
        self.assertEqual(run.call_args.kwargs['input'], 'enabled=true')

    def test_codex_config_home_override(self):
        with patch.dict(installer.os.environ, {'CODEX_HOME': str(self.root/'custom-codex')}):
            self.patch.stop()
            try:
                self.assertEqual(installer.client_directory('codex'), self.root/'custom-codex')
            finally:
                self.patch.start()

    def test_claude_install_defaults_to_scoped_allow_and_uninstall_preserves_existing(self):
        original = {'permissions':{'allow':['Read', 'mcp__saygo__*'], 'deny':['Bash(rm *)']}, 'theme':'dark'}
        installer.write_json(self.directory/'settings.json', original)
        with patch.object(installer, 'run'):
            installer.install_client('claude', self.market)
        value = json.loads((self.directory/'settings.json').read_text())
        self.assertIn('mcp__plugin_saygo-device_saygo__*', value['permissions']['allow'])
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
            self.assertTrue(value['mcpServers']['saygo']['trust'])
            self.assertEqual(value['mcpServers']['saygo']['command'],str(self.root/'python'))
            skill = self.directory/'skills/saygo-device/SKILL.md'
            self.assertEqual(skill.read_bytes(), (ROOT/'plugins/saygo-device/skills/device/SKILL.md').read_bytes())
            installer.configure_client(client, self.market)
            installer.unconfigure_client(client, self.market)
            self.assertEqual(json.loads((self.directory/'settings.json').read_text()), original)
            self.assertFalse(skill.exists())

    def test_independent_server_cannot_be_overwritten(self):
        original={'mcpServers':{'saygo':{'command':'custom'}}}
        installer.write_json(self.directory/'settings.json',original)
        with self.assertRaisesRegex(ValueError,'independently configured'):
            installer.configure_client('qoder',self.market)
        self.assertEqual(json.loads((self.directory/'settings.json').read_text()), original)

    def test_changed_skill_and_server_survive_uninstall(self):
        installer.configure_client('qoder',self.market)
        path=self.directory/'settings.json'; value=json.loads(path.read_text())
        value['mcpServers']['saygo']['command']='user-customized'
        installer.write_json(path,value)
        skill=self.directory/'skills/saygo-device/SKILL.md'; skill.write_text('custom')
        installer.unconfigure_client('qoder',self.market)
        self.assertEqual(json.loads(path.read_text())['mcpServers']['saygo']['command'],'user-customized')
        self.assertEqual(skill.read_text(),'custom')

    def test_prepare_only_does_not_configure_clients(self):
        with patch.object(installer,'prepare_runtime',return_value=self.root/'python'), patch.object(installer,'install_client') as install:
            installer.main(['--client','all','--prepare-only','--root',str(self.root/'prepared'),'--source',str(ROOT)])
        install.assert_not_called()
        self.assertFalse(self.directory.exists())
