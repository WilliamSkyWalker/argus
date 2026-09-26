"""Installed runtimes resolve private configuration outside site-packages."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from argus import config
from test_plugin_install import installer, ROOT


class ConfigInstallTests(unittest.TestCase):
    def test_user_project_and_process_precedence(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'config.env').write_text('LLM_MODEL=user-model\nLLM_API_KEY=user-secret\n')
            project = root/'project.env'
            project.write_text('LLM_MODEL=project-model\n')
            with patch.dict(os.environ, {'ARGUS_HOME_DIR':tmp}, clear=True), patch.object(config, 'ENV_FILE', project):
                loaded = config.load_config()
                self.assertEqual(loaded['llm']['model'], 'project-model')
                self.assertEqual(loaded['llm']['api_key'], 'user-secret')
                with patch.dict(os.environ, {'LLM_MODEL':'process-model'}):
                    self.assertEqual(config.load_config()['llm']['model'], 'process-model')
                with patch.dict(os.environ, {'ARGUS_CONFIG_FILE':str(root/'missing.env')}):
                    with self.assertRaises(FileNotFoundError):
                        config.load_config()

    def test_explicit_config_reference_does_not_copy_secrets(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            secret = root/'private.env'; secret.write_text('LLM_API_KEY=unique-test-secret\n')
            market = installer.prepare_plugin(ROOT, root/'managed', Path('/fake/python'), 'test', secret)
            mcp = json.loads((market/'plugins/argus-device/.mcp.json').read_text())
            self.assertEqual(mcp['mcpServers']['argus']['env']['ARGUS_CONFIG_FILE'], str(secret))
            for p in market.rglob('*'):
                if p.is_file():
                    self.assertNotIn(b'unique-test-secret', p.read_bytes())
