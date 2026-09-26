"""The Windows worker bundle must run without the repository or installed packages."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from argus.devices import mobile_host


class WorkerBundleTests(unittest.TestCase):
    def test_isolated_worker_imports_reorganized_stdlib_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'python').mkdir()
            (root / 'python/python.exe').touch()
            details = {'home': tmp, 'sdk_root': str(root / 'sdk')}
            with patch.object(mobile_host, 'wsl_path', side_effect=lambda value, windows=False: str(value)):
                host = mobile_host.prepare_windows(details)
            script = Path(host['script'])
            package = script.parents[1]
            self.assertEqual(script.parent.name, 'devices')
            self.assertEqual({str(p.relative_to(package)) for p in package.rglob('*') if p.is_file()}, {
                '__init__.py', 'logger.py', 'devices/__init__.py', 'devices/mobile.py',
                'devices/toolchain.py', 'devices/mobile_host_worker.py', 'devices/windows_no_console.cjs'})
            # HTTP avoids Windows locking. An invalid port exits before network I/O,
            # after importing mobile/toolchain from the copied package under -I -S.
            payload = dict(details, operation='http', port=0, base_path='/argus-test', method='GET', path='/status')
            result = subprocess.run([sys.executable, '-I', '-S', str(script)],
                input=json.dumps(payload), text=True, capture_output=True, cwd=tmp, timeout=10)
            self.assertEqual(result.returncode, 2, result.stderr)
            self.assertEqual(json.loads(result.stdout), {'error': 'Invalid Appium port'})
