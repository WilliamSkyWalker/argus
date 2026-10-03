#!/usr/bin/env python3
"""Upload and submit an existing Chrome Web Store item using the official v2 API."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import zipfile

ROOT = Path(__file__).resolve().parents[1]
API = 'https://chromewebstore.googleapis.com'


def request(url, *, token=None, data=None, content_type='application/json'):
    headers = {'Content-Type': content_type}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=60) as response:
            return json.load(response)
    except HTTPError as exc:
        # Do not print response bodies: OAuth errors can contain credential data.
        raise RuntimeError(f'API returned HTTP {exc.code}; inspect the developer dashboard. '
                           'Do not blindly repeat an upload or submission.') from None
    except (URLError, TimeoutError, OSError):
        raise RuntimeError('API transport failed; request outcome may be unknown. '
                           'Check status and the developer dashboard before retrying.') from None


def access_token():
    names = ('CWS_CLIENT_ID', 'CWS_CLIENT_SECRET', 'CWS_REFRESH_TOKEN')
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        raise ValueError('Missing GitHub Secrets: ' + ', '.join(missing))
    result = request('https://oauth2.googleapis.com/token', data=urlencode({
        'client_id': os.environ[names[0]], 'client_secret': os.environ[names[1]],
        'refresh_token': os.environ[names[2]], 'grant_type': 'refresh_token',
    }).encode(), content_type='application/x-www-form-urlencoded')
    token = result.get('access_token')
    if not isinstance(token, str) or not token:
        raise RuntimeError('OAuth did not return an access token')
    return token


def versions(revision):
    return [channel.get('crxVersion') for channel in revision.get('distributionChannels', [])]


def report(status):
    # Allowlist output; never log tokens or full API response bodies.
    result = {'published': status.get('publishedItemRevisionStatus', {}),
              'submitted': status.get('submittedItemRevisionStatus', {}),
              'upload': status.get('lastAsyncUploadState'),
              'taken_down': status.get('takenDown', False), 'warned': status.get('warned', False)}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    summary = os.environ.get('GITHUB_STEP_SUMMARY')
    if summary:
        with open(summary, 'a', encoding='utf-8') as stream:
            stream.write('## Chrome Web Store status\n\n```json\n' + json.dumps(result, indent=2) + '\n```\n')
    return result


def validate_package(path, expected):
    with zipfile.ZipFile(path) as archive:
        manifests = [info for info in archive.infolist() if info.filename == 'manifest.json']
        if len(manifests) != 1:
            raise ValueError('Store ZIP must contain exactly one root manifest.json')
        manifest = json.loads(archive.read('manifest.json'))
    if 'key' in manifest:
        raise ValueError('Use the store ZIP, not the development ZIP with a manifest key')
    if manifest.get('version') != expected:
        raise ValueError('Store ZIP version does not match the checked-out extension manifest')


def submit(item, token, package, expected, wait_seconds=180):
    def status():
        return request(API + '/v2/' + item + ':fetchStatus', token=token)

    before = status()
    report(before)
    if before.get('takenDown') or before.get('warned'):
        raise RuntimeError('Store policy action needs attention in the developer dashboard')
    published = before.get('publishedItemRevisionStatus', {})
    submitted = before.get('submittedItemRevisionStatus', {})
    if expected in versions(published) and published.get('state') == 'PUBLISHED':
        print('This version is already published; nothing uploaded or submitted.')
        return
    if submitted.get('state') in ('PENDING_REVIEW', 'STAGED'):
        if expected in versions(submitted) and submitted['state'] == 'PENDING_REVIEW':
            print('This version is already pending review; no duplicate submission.')
            return
        raise RuntimeError('An existing review or staged submission needs attention; it was not cancelled')
    if before.get('lastAsyncUploadState') in ('IN_PROGRESS', 'UPLOAD_IN_PROGRESS'):
        raise RuntimeError('An upload is already in progress; query status later')
    uploaded = request(API + '/upload/v2/' + item + ':upload', token=token,
                       data=package.read_bytes(), content_type='application/zip')
    if uploaded.get('name') != item:
        raise RuntimeError('Upload response item does not match the configured store item')
    state = uploaded.get('uploadState')
    deadline = time.monotonic() + wait_seconds
    while state in ('IN_PROGRESS', 'UPLOAD_IN_PROGRESS'):
        if time.monotonic() >= deadline:
            raise RuntimeError('Upload processing is still pending; query status before retrying')
        time.sleep(5)
        state = status().get('lastAsyncUploadState')
    if state != 'SUCCEEDED':
        raise RuntimeError('Upload did not succeed; inspect the developer dashboard')
    if uploaded.get('crxVersion') and uploaded['crxVersion'] != expected:
        raise RuntimeError('Uploaded version does not match the requested version')
    result = request(API + '/v2/' + item + ':publish', token=token,
                     data=json.dumps({'publishType': 'DEFAULT_PUBLISH',
                                      'skipReview': False, 'blockOnWarnings': True}).encode())
    if result.get('state') not in ('PENDING_REVIEW', 'PUBLISHED'):
        raise RuntimeError('Submission did not reach pending review or published state; inspect the dashboard')
    print('Submission response: ' + result['state'])
    after = status()
    report(after)
    if result['state'] == 'PENDING_REVIEW':
        print('Submitted for review. Google will publish after approval; this is not yet an approved release.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'submit'])
    parser.add_argument('--package', type=Path)
    args = parser.parse_args()
    publisher = os.environ.get('CWS_PUBLISHER_ID', '')
    if not re.fullmatch(r'[A-Za-z0-9_-]+', publisher):
        raise ValueError('Set GitHub variable CWS_PUBLISHER_ID from Publisher > Settings')
    release = json.loads((ROOT / 'distribution/release.json').read_text())
    extension = json.loads((ROOT / 'extensions/saygo-browser/manifest.json').read_text())
    extension_id = release['store_extension_id']
    if not re.fullmatch(r'[a-p]{32}', extension_id):
        raise ValueError('Invalid store_extension_id in distribution/release.json')
    if args.command == 'submit':
        if os.environ.get('GITHUB_REF') != 'refs/tags/v' + release['version']:
            raise ValueError('Submit only from the tag matching distribution/release.json')
        if not args.package:
            args.package = ROOT / 'dist/release' / f"saygo-browser-{extension['version']}-store.zip"
        validate_package(args.package, extension['version'])
    token = access_token()
    item = f'publishers/{publisher}/items/{extension_id}'
    if args.command == 'status':
        report(request(API + '/v2/' + item + ':fetchStatus', token=token))
    else:
        submit(item, token, args.package, extension['version'])


if __name__ == '__main__':
    try:
        main()
    except (ValueError, RuntimeError, OSError, zipfile.BadZipFile) as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(1)
