"""Run every offline core suite in isolated processes and state directories."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile

SUITES = ('control_demo', 'runtime_demo', 'desktop_demo', 'cli_demo',
          'mobile_demo', 'browser_demo', 'extension_demo')


def main():
    root = Path(__file__).resolve().parents[1]
    with tempfile.TemporaryDirectory(prefix='saygo-core-checks-') as temporary:
        env = {k: v for k, v in os.environ.items() if not k.startswith('SAYGO_TEST_')}
        env['QT_QPA_PLATFORM'] = 'offscreen'
        for suite in SUITES:
            env['SAYGO_HOME_DIR'] = str(Path(temporary) / suite)
            env['SAYGO_RUNTIME_DIR'] = str(Path(temporary) / suite / 'runtime')
            print(f'Running {suite}', flush=True)
            subprocess.run([sys.executable, '-m', 'unittest', 'discover',
                            '-s', f'tests/{suite}', '-v'], cwd=root, env=env,
                           check=True, timeout=180)


if __name__ == '__main__':
    main()
