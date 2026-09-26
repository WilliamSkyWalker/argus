#!/usr/bin/env python3
"""Wrap a native desktop build as DMG (macOS), ZIP (Windows) or tar.gz (Linux)."""
import argparse
import hashlib
import json
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def package(build_dir=None, out_dir=None):
    build_dir = Path(build_dir or ROOT/'dist/desktop').resolve()
    out = Path(out_dir or ROOT/'dist/installers').resolve()
    out.mkdir(parents=True, exist_ok=True)
    version = json.loads((ROOT/'distribution/release.json').read_text())['version']
    name = f'ArgusDesktop-{version}-{sys.platform}-{platform.machine()}'
    if sys.platform == 'darwin':
        app = build_dir/'ArgusDesktop.app'
        if not app.is_dir():
            raise FileNotFoundError('Build ArgusDesktop.app on macOS first')
        artifact = out/(name+'.dmg')
        with tempfile.TemporaryDirectory(prefix='argus-dmg-') as temporary:
            stage = Path(temporary)
            shutil.copytree(app, stage/app.name, symlinks=True)
            (stage/'Applications').symlink_to('/Applications', target_is_directory=True)
            subprocess.run(['hdiutil','create','-volname','Argus Desktop','-srcfolder',str(stage),
                            '-ov','-format','UDZO',str(artifact)],check=True)
        subprocess.run(['hdiutil','verify',str(artifact)],check=True)
    else:
        directory = build_dir/'ArgusDesktop'
        executable = directory/('ArgusDesktop.exe' if sys.platform == 'win32' else 'ArgusDesktop')
        if not executable.is_file():
            raise FileNotFoundError('Build the native desktop executable first')
        artifact = Path(shutil.make_archive(str(out/name), 'zip' if sys.platform=='win32' else 'gztar',
                                           root_dir=build_dir,base_dir='ArgusDesktop'))
    digest=hashlib.sha256(artifact.read_bytes()).hexdigest()
    artifact.with_suffix(artifact.suffix+'.sha256').write_text(digest+'  '+artifact.name+'\n')
    return artifact


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--build-dir',type=Path)
    parser.add_argument('--out-dir',type=Path)
    args=parser.parse_args()
    print(package(args.build_dir,args.out_dir))
