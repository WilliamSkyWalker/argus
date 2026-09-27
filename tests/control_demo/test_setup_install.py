"""Installed setup uses bundled sources and preserves installer failures."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from saygo import cli, setup


class SetupInstallTests(unittest.TestCase):
    def test_setup_dispatches_without_loading_optional_commands(self):
        with patch.object(cli, 'build_parser', side_effect=AssertionError('heavy CLI imports')), patch.object(setup, 'main', return_value=1) as main:
            self.assertEqual(cli.main(['setup', '--client', 'codex']), 1)
            main.assert_called_once_with(['--client', 'codex'])

    def test_installer_source_is_available_for_entire_call_and_cleaned_after(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp) / 'saygo'; package.mkdir()
            with zipfile.ZipFile(package / '_setup_source.zip', 'w') as bundle:
                bundle.writestr('scripts/install_agent_plugin.py', 'def main(args):\n    from pathlib import Path\n    assert (Path(args[1])/"scripts/install_agent_plugin.py").is_file()\n    return 7\n')
            with patch.object(setup, '__file__', str(package / 'setup.py')):
                self.assertEqual(setup.main(['--prepare-only']), 7)
                with setup.installation_source() as source:
                    self.assertTrue(source.is_dir())
                self.assertFalse(source.exists())

    def test_unsafe_archive_is_rejected_before_extraction(self):
        with tempfile.TemporaryDirectory() as tmp:
            package = Path(tmp)
            with zipfile.ZipFile(package / '_setup_source.zip', 'w') as bundle:
                bundle.writestr('../outside', 'bad')
            with patch.object(setup, '__file__', str(package / 'setup.py')):
                with self.assertRaisesRegex(ValueError, 'Unsafe path'):
                    with setup.installation_source():
                        self.fail('unsafe source accepted')

    def test_missing_resources_returns_failure(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(setup, '__file__', str(Path(tmp) / 'saygo/setup.py')):
            self.assertEqual(setup.main([]), 1)


if __name__ == '__main__':
    unittest.main()
