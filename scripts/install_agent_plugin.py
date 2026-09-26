#!/usr/bin/env python3
"""Install a managed Argus runtime and native Claude/Codex plugins.

Run from a checkout, or download this script alone: it fetches a source archive
without requiring Git. No dependency downloads take place during MCP startup.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv
import zipfile

NAME = "argus-device"
MARKETPLACE = "argus-managed"
REPOSITORY = "https://codeload.github.com/WilliamSkyWalker/argus/zip/refs/heads/main"


def run(args, **kwargs):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


def source_root(explicit, temporary):
    if explicit:
        root = Path(explicit).expanduser().resolve()
    elif (Path(__file__).resolve().parents[1] / 'pyproject.toml').is_file():
        root = Path(__file__).resolve().parents[1]
    else:
        archive = temporary / 'source.zip'
        print('Downloading Argus source archive (no Git required)...', flush=True)
        with urllib.request.urlopen(REPOSITORY, timeout=60) as response, archive.open('wb') as dest:
            shutil.copyfileobj(response, dest)
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (temporary / member.filename).resolve()
                if not target.is_relative_to(temporary.resolve()):
                    raise ValueError('Unsafe path in source archive')
            bundle.extractall(temporary)
        root = temporary / 'argus-main'
    for required in ('pyproject.toml', 'argus/__init__.py', 'plugins/argus-device/.codex-plugin/plugin.json'):
        if not (root / required).is_file():
            raise ValueError(f'Incomplete Argus source: missing {required} in {root}')
    return root


def fingerprint(source, extras):
    digest = hashlib.sha256()
    digest.update(f'{sys.version_info[:2]}:{sys.platform}:{extras}'.encode())
    paths = [source / 'pyproject.toml']
    for directory in ('argus', 'plugins/argus-device'):
        paths.extend(p for p in (source / directory).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    for path in sorted(paths):
        digest.update(str(path.relative_to(source)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


def prepare_runtime(source, root, extras):
    runtime = root / 'runtimes' / fingerprint(source, extras)
    python = runtime / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')
    ready = runtime / 'ready.json'
    if not ready.exists():
        # A failed installation is retried; no readiness marker is written on failure.
        venv.EnvBuilder(with_pip=False).create(runtime)
        if importlib.util.find_spec('pip'):
            pip = [sys.executable, '-m', 'pip', '--python', str(python)]
        else:
            run([python, '-m', 'ensurepip', '--upgrade'])
            pip = [str(python), '-m', 'pip']
        run([*pip, 'install', '--disable-pip-version-check', f'{source}[{extras}]'])
        run([python, '-I', '-c', 'import argus, mcp, PIL; from argus.mcp import server'])
        write_json(ready, {'source': str(source), 'extras': extras})
    return python


def prepare_plugin(source, root, python, version):
    market = root / 'marketplace'
    plugin = market / 'plugins' / NAME
    shutil.copytree(source / 'plugins' / NAME, plugin, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Absolute interpreter path survives both clients copying the plugin into caches.
    server = {'command': str(python), 'args': ['-I', '-m', 'argus.mcp.server', '--profile', 'device'],
              'env': {'PYTHONUNBUFFERED': '1'}}
    write_json(plugin / '.mcp.json', {'mcpServers': {'argus': server}})
    for directory in ('.claude-plugin', '.codex-plugin'):
        path = plugin / directory / 'plugin.json'
        manifest = json.loads(path.read_text())
        manifest['version'] = f'0.3.0+managed.{version}'
        if directory == '.claude-plugin':
            manifest['mcpServers'] = {'argus': server}
        write_json(path, manifest)
    write_json(plugin / 'managed_runtime.json', {'python': str(python)})
    write_json(market / '.claude-plugin' / 'marketplace.json', {
        'name': MARKETPLACE, 'owner': {'name': 'argus'},
        'plugins': [{'name': NAME, 'source': './plugins/' + NAME,
                     'description': 'Managed Argus runtime and visual operation skill'}]})
    write_json(market / '.agents' / 'plugins' / 'marketplace.json', {
        'name': MARKETPLACE, 'interface': {'displayName': 'Argus Managed'},
        'plugins': [{'name': NAME, 'source': {'source': 'local', 'path': './plugins/' + NAME},
                     'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
                     'category': 'Productivity'}]})
    return market


def install_client(client, market):
    run([client, 'plugin', 'marketplace', 'add', market])
    command = 'add' if client == 'codex' else 'install'
    run([client, 'plugin', command, NAME + '@' + MARKETPLACE])
    if client == 'claude':
        # install is a no-op for an existing plugin; update refreshes its cache.
        run([client, 'plugin', 'update', NAME + '@' + MARKETPLACE])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=['codex', 'claude', 'both'], required=True)
    parser.add_argument('--source', help='Source checkout; otherwise use adjacent checkout or download main')
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/argus/agent-plugin')
    parser.add_argument('--mobile', action='store_true', help='Also install mobile Python dependencies')
    parser.add_argument('--install-browser', action='store_true', help='Download Chromium for Playwright')
    parser.add_argument('--prepare-only', action='store_true', help='Build runtime and plugins without changing client configuration')
    args = parser.parse_args(argv)
    if sys.version_info < (3, 10):
        parser.error('Python 3.10+ is required')
    clients = ['codex', 'claude'] if args.client == 'both' else [args.client]
    if not args.prepare_only:
        for client in clients:
            if not shutil.which(client):
                parser.error(f'{client} CLI is not installed or not on PATH')
    extras = ['mcp']
    if args.install_browser:
        extras.append('browser')
    if sys.platform == 'win32':
        extras.append('windows')
    elif sys.platform == 'darwin':
        extras.append('mac')
    if args.mobile:
        extras.append('mobile')
    root = args.root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    # Cross-process lock avoids concurrent pip writers; kernel releases it on exit.
    with (root / 'install.lock').open('a+b') as lock:
        if os.name == 'nt':
            import msvcrt
            lock.write(b'0'); lock.flush(); lock.seek(0)
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='argus-install-') as temporary:
            source = source_root(args.source, Path(temporary))
            extra_string = ','.join(extras)
            python = prepare_runtime(source, root, extra_string)
            if args.install_browser:
                run([python, '-m', 'playwright', 'install', 'chromium'])
            market = prepare_plugin(source, root, python, fingerprint(source, extra_string))
            if not args.prepare_only:
                for client in clients:
                    install_client(client, market)
            write_json(root / 'installation.json', {'python': str(python), 'marketplace': str(market),
                                                    'clients': clients, 'prepared_only': args.prepare_only})
            print(f'Prepared plugin: {market / "plugins" / NAME}')
            if not args.prepare_only:
                print('Installed. Start a new client session and ask Argus to operate a test application.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Argus installation failed: {exc}', file=sys.stderr)
        sys.exit(1)
