#!/usr/bin/env python3
"""Build on the target OS. Produces a standalone folder/app; no user Python or pyenv."""
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','SaygoDesktop',
         '--paths',str(ROOT), '--distpath',str(ROOT/'dist/desktop'), '--workpath',str(ROOT/'build/desktop'),
         '--specpath',str(ROOT/'build'), '--collect-submodules','keyring.backends',
         '--add-data',str(ROOT/'extensions/saygo-browser')+os.pathsep+'saygo/desktop/assets/extension',
         '--add-data',str(ROOT/'LICENSE')+os.pathsep+'.',
         str(ROOT/'saygo/desktop/app.py')]
if sys.platform == 'win32':
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--distpath', str(ROOT/'dist/desktop'),
               '--workpath', str(ROOT/'build/desktop'),
               str(ROOT/'scripts/desktop_windows.spec')]
elif sys.platform=='darwin':
    command.insert(3,'--windowed')
subprocess.run(command,cwd=ROOT,check=True)
