import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from argus.cli import build_parser, main
from argus.commands import background, targets
from argus.qa import cases


class CommandTests(unittest.TestCase):
    def test_help_does_not_import_qa_agent(self):
        result = subprocess.run([sys.executable, '-c',
            "from argus.cli import build_parser; import sys; build_parser(); assert 'argus.qa.agent' not in sys.modules"],
            capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_command_handlers_cover_all_families(self):
        parser = build_parser()
        commands = ['init', 'new demo --platform android', 'list --json', 'run example',
                    'mcp doctor', 'device focus --session example', 'probes list', 'status', 'figma frames example']
        for command in commands:
            with self.subTest(command=command):
                args = parser.parse_args(command.split())
                self.assertTrue(callable(args.handler))
        self.assertEqual(parser.parse_args(['workflow', 'status', 'example']).command, 'workflow')

    def test_scaffold_and_discover_from_entry_point(self):
        template = targets.TESTS_DIR / '_template'
        with tempfile.TemporaryDirectory() as directory, patch.object(targets, 'TESTS_DIR', Path(directory)), \
             patch.object(cases, 'TESTS_DIR', Path(directory)), \
             patch.object(targets, 'PROJECT_ROOT', Path(directory)), contextlib.redirect_stdout(io.StringIO()):
            shutil.copytree(template, Path(directory) / '_template')
            main(['new', 'example', '--platform', 'browser', '--url', 'https://example.test'])
            target = Path(directory) / 'example'
            self.assertTrue((target / 'README.md').exists())
            resolved, root = cases._resolve_test_target(str(target))
            self.assertTrue(resolved)
            self.assertEqual(root, target)

    def test_background_report_resolution_updates_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / 'report.html'
            report.write_text('example')
            log = root / 'output.log'
            log.write_text(f'报告已保存: {report}\n')
            meta = {'report': '__auto__', 'log': str(log)}
            self.assertEqual(background._resolve_report_path(meta, root), str(report))
            self.assertEqual(json.loads((root / 'meta.json').read_text())['report'], str(report))
