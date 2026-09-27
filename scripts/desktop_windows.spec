from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

root = Path(SPECPATH).parent
a = Analysis(
    [str(root/'saygo/desktop/app.py')],
    pathex=[str(root)],
    datas=[(str(root/'extensions/saygo-browser'), 'saygo/desktop/assets/extension'),
           (str(root/'LICENSE'), '.')],
    hiddenimports=collect_submodules('keyring.backends'),
)
pyz = PYZ(a.pure)
# Share dependencies, but retain stdio only in the browser native host.
gui = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='SaygoDesktop', console=False)
host = EXE(pyz, a.scripts, [], exclude_binaries=True,
           name='SaygoNativeHost', console=True)
coll = COLLECT(gui, host, a.binaries, a.datas, name='SaygoDesktop')
