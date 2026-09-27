#!/usr/bin/env python3
"""Install Saygo for Claude, Codex, Qoder and QoderCN with scoped tool authorization.

Run from a checkout, or download this script alone: it fetches a source archive
without requiring Git. Optional background updates prepare separate runtimes.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import venv
import zipfile

NAME = "saygo-device"
MARKETPLACE = "saygo-managed"
# Replaced with immutable release coordinates by build_release.py.
RELEASE = None


def run(args, **kwargs):
    print('+ ' + ' '.join(map(str, args)), flush=True)
    subprocess.run(list(map(str, args)), check=True, **kwargs)


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix='.saygo-')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def client_directory(client):
    defaults = {'claude':'.claude', 'qoder':'.qoder', 'qodercn':'.qoder-cn'}
    variables = {'claude':'CLAUDE_CONFIG_DIR', 'qoder':'QODER_CONFIG_DIR', 'qodercn':'QODERCN_CONFIG_DIR'}
    return Path(os.environ.get(variables[client], Path.home()/defaults[client])).expanduser().resolve()


def client_available(client):
    if client in ('claude', 'codex'):
        return bool(shutil.which(client))
    aliases = ('qoder', 'qodercli') if client == 'qoder' else ('qodercn', 'qoder-cn', 'qoderclicn')
    root = client_directory(client)
    return any(shutil.which(name) for name in aliases) or (root/'settings.json').is_file() or (root/'bin').is_dir()


def configure_client(client, market):
    """Default scoped authorization; Qoder also gets MCP + the shared Skill."""
    directory = client_directory(client)
    settings = directory/'settings.json'
    receipt_path = directory/'saygo-install.json'
    value = json.loads(settings.read_text()) if settings.exists() else {}
    receipt = json.loads(receipt_path.read_text()) if receipt_path.exists() else {}
    owner = str(Path(market).resolve())
    if receipt and receipt.get('owner') != owner:
        raise ValueError(f'{client}: Saygo is owned by another installation: {receipt_path}')
    receipt = {**receipt, 'owner':owner, 'client':client}
    if client == 'claude':
        allow = value.setdefault('permissions', {}).setdefault('allow', [])
        added = receipt.setdefault('added_rules', [])
        for rule in ('mcp__saygo__*', 'mcp__plugin_saygo-device_saygo__*'):
            if rule not in allow:
                allow.append(rule)
                if rule not in added:
                    added.append(rule)
    else:
        source = Path(market)/'plugins'/NAME
        server = json.loads((source/'.mcp.json').read_text())['mcpServers']['saygo']
        server = {**server, 'trust':True}
        servers = value.setdefault('mcpServers', {})
        existing = servers.get('saygo')
        if existing is not None and existing != receipt.get('server') and existing != server:
            raise ValueError(f'{client}: an independently configured saygo server exists; left unchanged')
        skill = directory/'skills'/'saygo-device'/'SKILL.md'
        content = (source/'skills/device/SKILL.md').read_bytes()
        if skill.exists() and hashlib.sha256(skill.read_bytes()).hexdigest() != receipt.get('skill_sha256'):
            raise ValueError(f'{client}: existing Skill is not managed by this installer: {skill}')
        receipt.update(server=server, skill_sha256=hashlib.sha256(content).hexdigest())
        servers['saygo'] = server
        skill.parent.mkdir(parents=True, exist_ok=True)
        skill.write_bytes(content)
    directory.mkdir(parents=True, exist_ok=True)
    if settings.exists():
        backup = settings.with_name('settings.json.before-saygo')
        if not backup.exists():
            fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(settings.read_bytes())
    write_json(receipt_path, receipt)
    write_json(settings, value)
    return directory


def unconfigure_client(client, market):
    directory = client_directory(client)
    receipt_path = directory/'saygo-install.json'
    if not receipt_path.exists():
        return
    receipt = json.loads(receipt_path.read_text())
    if receipt.get('owner') != str(Path(market).resolve()):
        raise ValueError(f'{client}: another installation owns the configuration')
    settings = directory/'settings.json'
    value = json.loads(settings.read_text()) if settings.exists() else {}
    if client == 'claude':
        allow = value.get('permissions', {}).get('allow', [])
        for rule in receipt.get('added_rules', []):
            if rule in allow:
                allow.remove(rule)
    else:
        servers = value.get('mcpServers', {})
        if servers.get('saygo') == receipt.get('server'):
            servers.pop('saygo', None)
        skill = directory/'skills'/'saygo-device'/'SKILL.md'
        if skill.exists() and hashlib.sha256(skill.read_bytes()).hexdigest() == receipt.get('skill_sha256'):
            skill.unlink()
    write_json(settings, value)
    receipt_path.unlink()


def source_root(explicit, temporary, archive_path=None):
    if explicit:
        root = Path(explicit).expanduser().resolve()
    elif not archive_path and (Path(__file__).resolve().parents[1] / 'pyproject.toml').is_file():
        root = Path(__file__).resolve().parents[1]
    else:
        if not RELEASE:
            raise ValueError('Use a versioned release installer or --source; unpinned downloads are disabled')
        archive = Path(archive_path).resolve() if archive_path else temporary / 'source.zip'
        if not archive_path:
            print('Downloading verified Saygo release (no Git required)...', flush=True)
            with urllib.request.urlopen(RELEASE['url'], timeout=60) as response, archive.open('wb') as dest:
                shutil.copyfileobj(response, dest)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != RELEASE['sha256']:
            raise ValueError('Source archive SHA256 mismatch; nothing extracted or installed')
        with zipfile.ZipFile(archive) as bundle:
            for member in bundle.infolist():
                target = (temporary / member.filename).resolve()
                if not target.is_relative_to(temporary.resolve()):
                    raise ValueError('Unsafe path in source archive')
            bundle.extractall(temporary)
        root = temporary / RELEASE['root']
    for required in ('pyproject.toml', 'saygo/__init__.py', 'plugins/saygo-device/.codex-plugin/plugin.json'):
        if not (root / required).is_file():
            raise ValueError(f'Incomplete Saygo source: missing {required} in {root}')
    return root


def fingerprint(source, extras):
    digest = hashlib.sha256()
    digest.update(f'{sys.version_info[:2]}:{sys.platform}:{extras}'.encode())
    paths = [source / 'pyproject.toml']
    for directory in ('saygo', 'plugins/saygo-device', 'scripts', 'distribution', 'extensions/saygo-browser'):
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
        # Keep pip in the private runtime so future updates do not depend on
        # ensurepip being supplied by the host Python (e.g. Debian installations).
        run([*pip, 'install', '--disable-pip-version-check', 'pip', f'{source}[{extras}]'])
        run([python, '-I', '-c', 'import saygo, mcp, PIL, pip; from saygo.mcp import server'])
        write_json(ready, {'source': str(source), 'extras': extras})
    return python


def prepare_plugin(source, root, python, version, config_file=None):
    market = root / 'marketplace'
    plugin = market / 'plugins' / NAME
    shutil.copytree(source / 'plugins' / NAME, plugin, dirs_exist_ok=True,
                    ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    # Absolute interpreter path survives both clients copying the plugin into caches.
    root.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source/'saygo/updates.py', root/'update.py')
    server = {'command': str(python),
              'args': ['-I', str(root/'update.py'), '--root', str(root), '--serve', '--profile', 'device'],
              'env': {'PYTHONUNBUFFERED': '1', 'SAYGO_INSTALL_ROOT': str(root)}}
    if config_file:
        server['env']['SAYGO_CONFIG_FILE'] = str(config_file)
        version = hashlib.sha256((version + str(config_file)).encode()).hexdigest()[:16]
    write_json(plugin / '.mcp.json', {'mcpServers': {'saygo': server}})
    for directory in ('.claude-plugin', '.codex-plugin'):
        path = plugin / directory / 'plugin.json'
        manifest = json.loads(path.read_text())
        release = json.loads((source / 'distribution/release.json').read_text())
        manifest['version'] = f"{release['version']}+managed.{version}"
        if directory == '.claude-plugin':
            manifest['mcpServers'] = {'saygo': server}
        write_json(path, manifest)
    write_json(plugin / 'managed_runtime.json', {'python': str(python)})
    write_json(market / '.claude-plugin' / 'marketplace.json', {
        'name': MARKETPLACE, 'owner': {'name': 'saygo'},
        'plugins': [{'name': NAME, 'source': './plugins/' + NAME,
                     'description': 'Managed Saygo runtime and visual operation skill'}]})
    write_json(market / '.agents' / 'plugins' / 'marketplace.json', {
        'name': MARKETPLACE, 'interface': {'displayName': 'Saygo Managed'},
        'plugins': [{'name': NAME, 'source': {'source': 'local', 'path': './plugins/' + NAME},
                     'policy': {'installation': 'AVAILABLE', 'authentication': 'ON_INSTALL'},
                     'category': 'Productivity'}]})
    return market


def install_client(client, market):
    if client in ('qoder', 'qodercn'):
        configure_client(client, market)
        return
    run([client, 'plugin', 'marketplace', 'add', market])
    command = 'add' if client == 'codex' else 'install'
    run([client, 'plugin', command, NAME + '@' + MARKETPLACE])
    if client == 'claude':
        # install is a no-op for an existing plugin; update refreshes its cache.
        run([client, 'plugin', 'update', NAME + '@' + MARKETPLACE])
        configure_client(client, market)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--client', choices=['codex', 'claude', 'qoder', 'qodercn', 'both', 'all', 'auto'], default='auto',
                        help='auto detects installed clients; both = Claude+Codex; all = all four')
    parser.add_argument('--source', help='Explicit source checkout (development only)')
    parser.add_argument('--archive', help='Local source ZIP, checked against this release installer SHA256')
    parser.add_argument('--root', type=Path, default=Path.home() / '.local/share/saygo/agent-plugin')
    parser.add_argument('--mobile', action='store_true', help='Also install mobile Python dependencies')
    parser.add_argument('--install-browser', action='store_true', help='Download Chromium for Playwright')
    parser.add_argument('--prepare-only', action='store_true', help='Build runtime and plugins without changing client configuration')
    parser.add_argument('--browser', choices=['chrome', 'edge', 'none'], default='chrome')
    parser.add_argument('--extension-id', help='Override the packaged development/store extension ID')
    parser.add_argument('--bridge-directory', help='Override native host directory (WSL path on WSL)')
    parser.add_argument('--config-file', type=Path, help='Reference an existing user .env file; never copy its secrets into the plugin')
    parser.add_argument('--uninstall', action='store_true', help='Remove plugin and bridge registration; preserve sessions and files')
    parser.add_argument('--auto-update', action=argparse.BooleanOptionalAction, default=None,
                        help='Automatically update compatible runtimes at the next idle client startup')
    parser.add_argument('--update-channel', choices=['stable', 'beta'], default=None)
    args = parser.parse_args(argv)
    if args.extension_id and not re.fullmatch('[a-p]{32}', args.extension_id):
        parser.error('--extension-id must contain exactly 32 letters a-p')
    if sys.version_info < (3, 10):
        parser.error('Python 3.10+ is required')
    if args.source and args.archive:
        parser.error('--source and --archive are mutually exclusive')
    if args.config_file:
        args.config_file = args.config_file.expanduser().resolve()
        if not args.config_file.is_file():
            parser.error('--config-file must point to an existing file')
    if args.uninstall:
        return uninstall(args.root.expanduser().resolve())
    candidates = ('codex', 'claude', 'qoder', 'qodercn')
    clients = ([c for c in candidates if client_available(c)] if args.client == 'auto'
               else list(candidates) if args.client == 'all'
               else ['codex', 'claude'] if args.client == 'both' else [args.client])
    if not clients and not args.prepare_only:
        parser.error('No supported client detected; select --client explicitly or use --prepare-only')
    if not args.prepare_only:
        for client in clients:
            if client in ('claude', 'codex') and not shutil.which(client):
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
        previous = json.loads((root / 'installation.json').read_text()) if (root / 'installation.json').exists() else {}
        config_file = args.config_file or previous.get('config_file')
        if args.prepare_only and previous and not previous.get('prepared_only'):
            raise ValueError('--prepare-only requires a separate root from an installed plugin')
        with tempfile.TemporaryDirectory(prefix='saygo-install-') as temporary:
            source = source_root(args.source, Path(temporary), args.archive)
            extra_string = ','.join(extras)
            python = prepare_runtime(source, root, extra_string)
            if args.install_browser:
                run([python, '-m', 'playwright', 'install', 'chromium'])
            market = prepare_plugin(source, root, python, fingerprint(source, extra_string), config_file)
            release = json.loads((source / 'distribution/release.json').read_text())
            extension_id = args.extension_id or release.get('store_extension_id') or release['development_extension_id']
            bridge = previous.get('bridge') if not args.prepare_only else None
            record = {'python': str(python), 'marketplace': str(market),
                      'clients': previous.get('clients', []) if not args.prepare_only else [],
                      'prepared_only': args.prepare_only, 'bridge': bridge,
                      'version': release['version'], 'extension_id': extension_id,
                      'config_file': str(config_file) if config_file else None,
                      'mobile': args.mobile, 'install_browser': args.install_browser}
            spec = importlib.util.spec_from_file_location('saygo_update_metadata', source/'saygo/updates.py')
            updater = importlib.util.module_from_spec(spec); spec.loader.exec_module(updater)
            record['compatibility'] = updater.compatibility(source)
            if not args.prepare_only:
                if args.browser != 'none':
                    command = [str(python), '-I', '-m', 'saygo.integrations.browser_setup',
                               '--extension-id', extension_id, '--browser', args.browser, '--save-default']
                    if args.bridge_directory:
                        command += ['--directory', args.bridge_directory]
                    bridge = json.loads(subprocess.check_output(command, text=True))
                    record['bridge'] = bridge
                # Keep recovery metadata even when a client CLI fails partway through.
                write_json(root / 'installation.json', record)
                for client in clients:
                    if client not in record['clients']:
                        record['clients'].append(client)
                    write_json(root / 'installation.json', record)
                    install_client(client, market)
            extension = Path(bridge['directory']) / 'extension' if bridge else root / 'browser-extension'
            shutil.copytree(source / 'extensions/saygo-browser', extension, dirs_exist_ok=True)
            write_json(root / 'installation.json', record)
            (root/'active-runtime.json').unlink(missing_ok=True)
            (root/'update-state.json').unlink(missing_ok=True)
            updates = json.loads((root/'updates.json').read_text()) if (root/'updates.json').exists() else {'automatic': False, 'channel': 'stable'}
            if args.auto_update is not None: updates['automatic'] = args.auto_update
            if args.update_channel: updates['channel'] = args.update_channel
            write_json(root/'updates.json', updates)
            print(f'Prepared plugin: {market / "plugins" / NAME}')
            if not args.prepare_only:
                print('Installed. Start a new client session and ask Saygo to operate a test application.')
                if args.browser != 'none':
                    if extension_id != release['development_extension_id']:
                        print('Install the extension from: https://chromewebstore.google.com/detail/' + extension_id)
                    else:
                        if bridge.get('host') == 'windows':
                            extension = subprocess.check_output(['wslpath', '-w', str(extension)], text=True).strip()
                        print(f'Open {args.browser}://extensions, enable Developer mode, Load unpacked: {extension}')
                    print('Then open Saygo Browser and click Connect. Browser confirmation is required.')


def uninstall(root):
    record = json.loads((root / 'installation.json').read_text())
    if record.get('uninstalled'):
        print('Already uninstalled. Files preserved at ' + str(root))
        return
    if record.get('prepared_only'):
        print('Prepared only: no registrations to remove. Files preserved at ' + str(root))
        return
    for client in list(record['clients']):
        if client in ('codex', 'claude'):
            command = 'remove' if client == 'codex' else 'uninstall'
            run([client, 'plugin', command, NAME + '@' + MARKETPLACE])
        if client != 'codex':
            unconfigure_client(client, record['marketplace'])
        record['clients'].remove(client)
        write_json(root / 'installation.json', record)
    if record.get('bridge'):
        run(record['bridge']['uninstall_command'])
        default = Path(os.environ.get('SAYGO_HOME_DIR', Path.home()/'.saygo')) / 'browser-bridge.json'
        if default.exists() and json.loads(default.read_text()).get('directory') == record['bridge']['directory']:
            default.unlink()
    record['uninstalled'] = True
    write_json(root / 'installation.json', record)
    print('Registrations removed. Sessions, runtimes and extension files preserved. Remove the extension in your browser.')


if __name__ == '__main__':
    try:
        main()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print(f'Saygo installation failed: {exc}', file=sys.stderr)
        sys.exit(1)
