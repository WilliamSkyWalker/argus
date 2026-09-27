from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
a = Analysis(
    [str(root/'argus/desktop/app.py')],
    pathex=[str(root)],
    datas=[(str(root/'extensions/argus-browser'), 'argus/desktop/assets/extension'),
           (str(root/'LICENSE'), '.')],
    hiddenimports=collect_submodules('keyring.backends'),
)
pyz = PYZ(a.pure)
# Share dependencies, but retain stdio only in the browser native host.
gui = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='ArgusDesktop', console=False)
host = EXE(pyz, a.scripts, [], exclude_binaries=True,
           name='ArgusNativeHost', console=True)
coll = COLLECT(gui, host, a.binaries, a.datas, name='ArgusDesktop')
