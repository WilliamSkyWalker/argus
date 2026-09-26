"""Managed plugin bundles must work after either client copies them to a cache."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('argus_plugin_install', ROOT / 'scripts/install_agent_plugin.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class PluginInstallTests(unittest.TestCase):
    def test_both_clients_use_same_isolated_runtime_and_skill(self):
        with tempfile.TemporaryDirectory(prefix='argus plugin ') as tmp:
            root = Path(tmp)
            python = root / 'runtime with spaces/bin/python'
            market = installer.prepare_plugin(ROOT, root, python, 'abc123')
            plugin = market / 'plugins/argus-device'
            claude = json.loads((plugin / '.claude-plugin/plugin.json').read_text())
            codex = json.loads((plugin / '.codex-plugin/plugin.json').read_text())
            mcp = json.loads((plugin / codex['mcpServers']).read_text())
            self.assertEqual(claude['mcpServers'], mcp['mcpServers'])
            server = mcp['mcpServers']['argus']
            self.assertEqual(server['command'], str(python))
            self.assertEqual(server['args'], ['-I', '-m', 'argus.mcp.server', '--profile', 'device'])
            self.assertEqual((plugin / 'skills/device/SKILL.md').read_bytes(),
                             (ROOT / 'plugins/argus-device/skills/device/SKILL.md').read_bytes())
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
            with self.assertRaisesRegex(ValueError, 'Incomplete Argus source'):
                installer.source_root(tmp, Path(tmp))


if __name__ == '__main__':
    unittest.main()
