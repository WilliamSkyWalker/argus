import hashlib
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('latest_installer', Path(__file__).resolve().parents[2]/'scripts/install_argus.py')
installer = importlib.util.module_from_spec(spec); spec.loader.exec_module(installer)


class LatestInstallerTests(unittest.TestCase):
    def release(self, number, beta=False):
        name='install-argus-'+number+'.py'
        return {'tag_name':'v'+number, 'prerelease':beta, 'assets':[{
            'name':name, 'browser_download_url':'https://github.com/WilliamSkyWalker/argus/releases/download/v'+number+'/'+name,
            'digest':'sha256:'+hashlib.sha256(b'installer').hexdigest()}]}

    def test_latest_semver_and_stable_channel(self):
        with patch.object(installer,'fetch',return_value=json.dumps([self.release('0.9.0'),self.release('0.10.0',True)]).encode()):
            self.assertEqual(installer.latest()['tag_name'],'v0.10.0')
            self.assertEqual(installer.latest('stable')['tag_name'],'v0.9.0')

    def test_download_verifies_and_passes_arguments(self):
        with patch.object(installer,'fetch',side_effect=[json.dumps([self.release('0.10.0')]).encode(),b'installer']), patch.object(installer.subprocess,'call',return_value=0) as run:
            self.assertEqual(installer.main(['--auto-update','--client','codex']),0)
            self.assertEqual(run.call_args.args[0][2:],['--auto-update','--client','codex','--update-channel','beta'])

    def test_corrupt_installer_is_never_executed(self):
        with patch.object(installer,'fetch',side_effect=[json.dumps([self.release('0.10.0')]).encode(),b'corrupt']), patch.object(installer.subprocess,'call') as run:
            with self.assertRaisesRegex(ValueError,'SHA256 mismatch'): installer.main([])
            run.assert_not_called()

    def test_check_does_not_download_or_execute(self):
        with patch.object(installer,'fetch',return_value=json.dumps([self.release('0.10.0')]).encode()) as fetch, patch.object(installer.subprocess,'call') as run:
            installer.main(['--check']); self.assertEqual(fetch.call_count,1); run.assert_not_called()
