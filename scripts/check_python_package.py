#!/usr/bin/env python3
"""Smoke-test a wheel away from the checkout, without registering any clients."""
import argparse
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import venv
import zipfile


def check(wheel):
    wheel = Path(wheel).resolve()
    with zipfile.ZipFile(wheel) as bundle:
        with zipfile.ZipFile(io.BytesIO(bundle.read('saygo/_setup_source.zip'))) as source:
            required = ['pyproject.toml', 'setup.py', 'MANIFEST.in',
                        'scripts/install_agent_plugin.py', 'saygo/setup.py',
                        'plugins/saygo-device/.mcp.json',
                        'plugins/saygo-device/.codex-plugin/plugin.json',
                        'plugins/saygo-device/.claude-plugin/plugin.json',
                        'plugins/saygo-device/skills/device/SKILL.md',
                        'extensions/saygo-browser/manifest.json']
            for name in required:
                assert name in source.namelist(), name
            version = json.loads(source.read('distribution/release.json'))['version']
    with tempfile.TemporaryDirectory(prefix='saygo-wheel-check-') as temporary:
        root = Path(temporary)
        environment = root / 'venv'
        venv.EnvBuilder(with_pip=False).create(environment)
        python = environment / ('Scripts/python.exe' if sys.platform == 'win32' else 'bin/python')
        cli = environment / ('Scripts/saygo.exe' if sys.platform == 'win32' else 'bin/saygo')
        def run(args):
            subprocess.run(list(map(str, args)), cwd=root, check=True)
        run([sys.executable, '-m', 'pip', '--python', python, 'install', f'{wheel}[mcp]'])
        run([cli, '--help'])
        run([cli, 'setup', '--help'])
        managed = root / 'managed'
        run([cli, 'setup', '--client', 'both', '--prepare-only', '--browser', 'none', '--root', managed])
        installed = json.loads((managed / 'installation.json').read_text())
        assert installed['version'] == version
        assert installed['prepared_only'] and not installed['clients'] and installed['bridge'] is None
        market = managed / 'marketplace/plugins/saygo-device'
        server = json.loads((market / '.mcp.json').read_text())['mcpServers']['saygo']
        assert Path(server['command']).is_file()
        assert (market / 'skills/device/SKILL.md').is_file()
        assert (managed / 'browser-extension/manifest.json').is_file()
        run([server['command'], '-I', '-c', 'import saygo.setup; from saygo.mcp import server'])
        # Repeating setup should reuse the same runtime, not create a fresh one.
        run([cli, 'setup', '--client', 'both', '--prepare-only', '--browser', 'none', '--root', managed])
        assert len(list((managed / 'runtimes').iterdir())) == 1
        print('Wheel install, setup, packaged resources, MCP import and repeat setup: passed')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheel')
    check(parser.parse_args().wheel)
