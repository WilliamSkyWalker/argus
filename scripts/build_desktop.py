#!/usr/bin/env python3
"""Build on the target OS. Produces a standalone folder/app; no user Python or pyenv."""
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
command=[sys.executable,'-m','PyInstaller','--noconfirm','--clean','--onedir','--name','ArgusDesktop',
         '--paths',str(ROOT), '--distpath',str(ROOT/'dist/desktop'), '--workpath',str(ROOT/'build/desktop'),
         '--specpath',str(ROOT/'build'), '--collect-submodules','keyring.backends',
         '--add-data',str(ROOT/'extensions/argus-browser')+os.pathsep+'argus/desktop/assets/extension',
         '--add-data',str(ROOT/'LICENSE')+os.pathsep+'.',
         str(ROOT/'argus/desktop/app.py')]
# Windows GUI applications lack stdin/stdout required by Chrome native messaging.
# Use a console executable for this beta; macOS produces an app bundle.
if sys.platform=='darwin': command.insert(3,'--windowed')
subprocess.run(command,cwd=ROOT,check=True)
