#!/usr/bin/env python3
"""Build deterministic beta/store extension ZIPs and a hash-pinned installer."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def archive(path, entries):
    with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as bundle:
        for name, content in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            bundle.writestr(info, content)


def source_files():
    # Deliberately exclude checkout configuration, credentials, local tests and run artifacts.
    for name in ('README.md', 'LICENSE', 'pyproject.toml'):
        yield ROOT / name
    allowed = {'.py', '.js', '.cjs', '.ps1', '.json', '.md', '.txt', '.html', '.png', '.svg'}
    for directory in ('argus', 'plugins/argus-device', 'extensions/argus-browser', 'distribution'):
        for path in sorted((ROOT / directory).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and (path.suffix in allowed or path.name == 'LICENSE'):
                yield path
    yield ROOT / 'docs/desktop.md'
    for name in ('install_agent_plugin.py', 'build_release.py', 'build_desktop.py', 'package_desktop.py',
                 'desktop_windows.spec'):
        yield ROOT / 'scripts' / name


def build(out, store_id=None):
    release = json.loads((ROOT / 'distribution/release.json').read_text())
    version = release['version']
    if not re.fullmatch(r'\d+\.\d+\.\d+', version):
        raise ValueError('Release version must be X.Y.Z')
    if store_id:
        if not re.fullmatch('[a-p]{32}', store_id):
            raise ValueError('Invalid store extension ID')
        release['store_extension_id'] = store_id
    out = Path(out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    prefix = 'argus-' + version
    entries = {prefix + '/' + p.relative_to(ROOT).as_posix(): p.read_bytes() for p in source_files()}
    entries[prefix + '/distribution/release.json'] = (json.dumps(release, indent=2)+'\n').encode()
    archive(out / (prefix+'.zip'), entries)
    digest = hashlib.sha256((out/(prefix+'.zip')).read_bytes()).hexdigest()
    coordinates = {'url':f'https://github.com/WilliamSkyWalker/argus/releases/download/v{version}/{prefix}.zip',
                   'sha256':digest, 'root':prefix}
    script = (ROOT/'scripts/install_agent_plugin.py').read_text()
    if script.count('RELEASE = None') != 1:
        raise ValueError('Installer release placeholder missing')
    installer = out/f'install-argus-{version}.py'
    installer.write_text(script.replace('RELEASE = None', 'RELEASE = ' + repr(coordinates)))
    extension_root = ROOT/'extensions/argus-browser'
    extension = {p.relative_to(extension_root).as_posix():p.read_bytes() for p in source_files() if p.is_relative_to(extension_root)}
    manifest = json.loads(extension['manifest.json'])
    if manifest['version'] != version:
        raise ValueError('Extension version differs from release version')
    archive(out/f'argus-browser-{version}-development.zip', extension)
    manifest.pop('key', None)  # The Web Store supplies the production identity.
    extension['manifest.json'] = (json.dumps(manifest, indent=2)+'\n').encode()
    archive(out/f'argus-browser-{version}-store.zip', extension)
    products = [prefix+'.zip', installer.name, f'argus-browser-{version}-development.zip', f'argus-browser-{version}-store.zip']
    (out/'SHA256SUMS').write_text(''.join(hashlib.sha256((out/name).read_bytes()).hexdigest()+'  '+name+'\n' for name in products))
    (out/'release-manifest.json').write_text(json.dumps({**release, 'source':coordinates, 'artifacts':products},indent=2)+'\n')
    return out


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', default='dist/release')
    parser.add_argument('--store-extension-id')
    args = parser.parse_args()
    print(build(args.out, args.store_extension_id))
