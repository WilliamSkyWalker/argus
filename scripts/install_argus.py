#!/usr/bin/env python3
"""Download and run the latest checksum-verified Argus release installer.

Requires Python 3.10+. No Git, pip package or manually downloaded release needed.
Remaining arguments are passed to the versioned installer.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

REPOSITORY = 'WilliamSkyWalker/argus'
API = 'https://api.github.com/repos/' + REPOSITORY + '/releases?per_page=100'


def fetch(url):
    request = urllib.request.Request(url, headers={'User-Agent': 'Argus-Installer'})
    with urllib.request.urlopen(request, timeout=60) as response:
        return response.read()


def latest(channel='beta'):
    candidates = []
    for release in json.loads(fetch(API)):
        tag = release.get('tag_name', '')
        if release.get('draft') or (channel == 'stable' and release.get('prerelease')):
            continue
        if re.fullmatch(r'v\d+\.\d+\.\d+', tag):
            candidates.append(release)
    if not candidates:
        raise ValueError('No releases available for '+channel+'; use --channel beta for prereleases')
    return max(candidates, key=lambda item: tuple(map(int, item['tag_name'][1:].split('.'))))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--channel', choices=['stable', 'beta'], default='beta',
                        help='beta includes prereleases (default during the Argus beta)')
    parser.add_argument('--check', action='store_true', help='Show the latest version without installing')
    args, options = parser.parse_known_args(argv)
    if sys.version_info < (3, 10): parser.error('Python 3.10+ is required')
    release = latest(args.channel)
    number = release['tag_name'][1:]
    name = 'install-argus-'+number+'.py'
    url = 'https://github.com/'+REPOSITORY+'/releases/download/'+release['tag_name']+'/'+name
    asset = next((item for item in release.get('assets', []) if item['name'] == name), None)
    if not asset or asset.get('browser_download_url') != url:
        raise ValueError('Latest release is missing its versioned installer')
    print('Latest Argus: '+number+' ('+args.channel+')', flush=True)
    if args.check: return 0
    digest = asset.get('digest', '')
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', digest):
        raise ValueError('Installer asset has no SHA256 digest')
    data = fetch(url)
    if hashlib.sha256(data).hexdigest() != digest[7:]:
        raise ValueError('Installer SHA256 mismatch; installation cancelled')
    with tempfile.TemporaryDirectory(prefix='argus-bootstrap-') as temporary:
        installer = Path(temporary)/name; installer.write_bytes(data)
        if not any(item == '--update-channel' or item.startswith('--update-channel=') for item in options):
            options += ['--update-channel', args.channel]
        return subprocess.call([sys.executable, str(installer), *options])


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print('Argus installation failed: '+str(exc), file=sys.stderr)
        raise SystemExit(1)
