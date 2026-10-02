"""Managed plugin bundles must work after either client copies them to a cache."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('saygo_plugin_install', ROOT / 'scripts/install_agent_plugin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PluginInstallTests(unittest.TestCase):
    def test_upgrade_preserves_extension_id_unless_explicitly_overridden(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            original = 'a' * 32
            override = 'b' * 32
            installer.write_json(root / 'installation.json',
                                 {'prepared_only': True, 'extension_id': original})
            with patch.object(installer, 'prepare_runtime', return_value=root / 'python'):
                args = ['--client', 'codex', '--prepare-only', '--root', str(root), '--source', str(ROOT)]
                installer.main(args)
                self.assertEqual(json.loads((root / 'installation.json').read_text())['extension_id'], original)
                installer.main(args + ['--extension-id', override])
                self.assertEqual(json.loads((root / 'installation.json').read_text())['extension_id'], override)

    def test_both_clients_use_same_isolated_runtime_and_skill(self):
        with tempfile.TemporaryDirectory(prefix='saygo plugin ') as tmp:
            root = Path(tmp)
            python = root / 'runtime with spaces/bin/python'
            market = installer.prepare_plugin(ROOT, root, python, 'abc123')
            plugin = market / 'plugins/saygo-device'
            claude = json.loads((plugin / '.claude-plugin/plugin.json').read_text())
            codex = json.loads((plugin / '.codex-plugin/plugin.json').read_text())
            mcp = json.loads((plugin / codex['mcpServers']).read_text())
            self.assertEqual(claude['mcpServers'], mcp['mcpServers'])
            server = mcp['mcpServers']['saygo']
            self.assertEqual(server['command'], str(python))
            self.assertEqual(server['args'], ['-I', str(root/'update.py'), '--root', str(root), '--serve', '--profile', 'device'])
            self.assertTrue((root/'update.py').is_file())
            self.assertEqual((plugin / 'skills/device/SKILL.md').read_bytes(),
                             (ROOT / 'plugins/saygo-device/skills/device/SKILL.md').read_bytes())
            self.assertNotIn('PYTHONPATH', server['env'])
            self.assertEqual(json.loads((plugin / 'managed_runtime.json').read_text())['python'], str(python))

    def test_failed_dependency_install_never_marks_ready(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(installer.venv.EnvBuilder, 'create'), patch.object(installer, 'run', side_effect=OSError('offline')):
                with self.assertRaisesRegex(OSError, 'offline'):
                    installer.prepare_runtime(ROOT, Path(tmp), 'browser,mcp')
            self.assertFalse(list(Path(tmp).rglob('ready.json')))

    def test_client_install_failure_is_propagated(self):
        with patch.object(installer, 'run', side_effect=OSError('plugin install failed')):
            with self.assertRaises(OSError):
                installer.install_client('codex', Path('/tmp/plugin-market'))

    def test_incomplete_source_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'Incomplete Saygo source'):
                installer.source_root(tmp, Path(tmp))


if __name__ == '__main__':
    unittest.main()
