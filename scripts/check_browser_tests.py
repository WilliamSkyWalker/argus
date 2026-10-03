"""Bounded headed integration gate. Run under xvfb-run with Openbox installed."""
import os
from pathlib import Path
import subprocess
import sys
import time


def main():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        chrome = pw.chromium.executable_path
    if not Path(chrome).is_file():
        raise SystemExit('Install Chromium with: python -m playwright install chromium')
    root = Path(__file__).resolve().parents[1]
    manager = subprocess.Popen(['openbox', '--sm-disable'], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    try:
        deadline=time.monotonic()+5
        while True:
            if manager.poll() is not None:
                raise RuntimeError('Openbox exited before browser tests; check window manager dependencies')
            result=subprocess.run(['xprop','-root','_NET_SUPPORTING_WM_CHECK'],capture_output=True,text=True)
            if 'window id #' in result.stdout: break
            if time.monotonic() >= deadline: raise RuntimeError('Window manager did not become ready')
            time.sleep(.05)
        env = {**os.environ, 'SAYGO_TEST_CHROME': chrome, 'SAYGO_TEST_HEADED': '1'}
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s',
                        'tests/extension_demo', '-p', 'test_live.py', '-v'],
                       cwd=root, env=env, check=True, timeout=180)
        subprocess.run([sys.executable, '-m', 'unittest', 'discover', '-s',
                        'tests/browser_demo', '-p', 'test_playwright_live.py', '-v'],
                       cwd=root, env=env, check=True, timeout=120)
    finally:
        manager.terminate()
        manager.wait(timeout=10)


if __name__ == '__main__':
    main()
