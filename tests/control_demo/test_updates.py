"""Update safety: versions, hashes, concurrency and atomic runtime selection."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
import zipfile
from saygo import updates as u


class UpdateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        u.write(self.root/'installation.json', {'python': sys.executable, 'version': '0.4.0', 'compatibility': 'same'})
        self.env = patch.dict(os.environ, {'SAYGO_HOME_DIR': str(self.root/'home'), 'SAYGO_RUNTIME_DIR': str(self.root/'runtime')})
        self.env.start(); self.addCleanup(self.env.stop)
        background = patch.object(u.subprocess, 'Popen')
        background.start(); self.addCleanup(background.stop)

    def release(self, number, beta=False):
        return {'tag_name': 'v'+number, 'prerelease': beta, 'draft': False, 'assets': []}

    def test_numeric_versions_and_beta_opt_in(self):
        releases = [self.release('0.5.0'), self.release('0.10.0'), self.release('0.11.0', True)]
        with patch.object(u, 'fetch', return_value=json.dumps(releases).encode()):
            self.assertEqual(u.check(self.root, True)['release']['tag_name'], 'v0.10.0')
            u.write(self.root/'updates.json', {'channel': 'beta'})
            self.assertEqual(u.check(self.root, True)['release']['tag_name'], 'v0.11.0')

    def test_checks_are_cached_and_offline_keeps_runtime(self):
        with patch.object(u, 'fetch', side_effect=OSError('offline')) as fetch:
            self.assertIn('offline', u.check(self.root)['error'])
            u.check(self.root)
            self.assertEqual(fetch.call_count, 1)
        self.assertEqual(u.current(self.root)['python'], sys.executable)

    def test_asset_digest_and_origin_are_required(self):
        release = self.release('0.5.0'); name = 'install-saygo-0.5.0.py'
        release['assets'] = [{'name': name, 'browser_download_url': u.DOWNLOAD+'v0.5.0/'+name, 'digest': 'sha256:'+'0'*64}]
        with patch.object(u, 'fetch', return_value=b'corrupt'):
            with self.assertRaisesRegex(ValueError, 'SHA256 mismatch'): u.asset(release, name, self.root)
        release['assets'][0]['browser_download_url'] = 'https://example.com/'+name
        with patch.object(u, 'fetch') as fetch:
            with self.assertRaisesRegex(ValueError, 'unexpected'): u.asset(release, name, self.root)
            fetch.assert_not_called()

    def test_active_server_blocks_manual_update(self):
        with u.lock(self.root/'server-test.lock'):
            state = u.apply(self.root, {'available': True})
            self.assertIn('Close other', state['deferred'])
        self.assertFalse(u.busy(self.root))

    def test_unfinished_tasks_block_activation(self):
        from saygo.runtime.store import Store
        store = Store(self.root/'runtime'); state = store.create({'resources': {}})
        self.assertTrue(u.busy(self.root))
        state['status'] = 'cancelled'; store.save(state, 'cancelled')
        self.assertFalse(u.busy(self.root))

    def stage(self, fail=False):
        installer = self.root/'installer.py'; installer.write_text('')
        archive = self.root/'source.zip'
        with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr('saygo-0.5.0/placeholder', '')
        def run(command, **kwargs):
            if fail: raise subprocess.CalledProcessError(1, command)
            stage = self.root/'updates/0.5.0'
            u.write(stage/'installation.json', {'python': sys.executable, 'version': '0.5.0'})
            (stage/'update.py').write_text('# verified staged launcher\n')
        with patch.object(u, 'asset', side_effect=[installer, archive]), patch.object(u, 'compatibility', return_value='same'), patch.object(u.subprocess, 'run', side_effect=run):
            return u.apply(self.root, {'checked_at': time.time(), 'available': True, 'release': self.release('0.5.0')}, stage_only=True)

    def test_background_preparation_does_not_switch_running_version(self):
        with u.lock(self.root/'server-test.lock'): self.stage()
        self.assertEqual(u.current(self.root)['version'], '0.4.0')
        self.assertEqual(u.read(self.root/'pending-runtime.json')['version'], '0.5.0')

    def test_failed_prepare_preserves_runtime_pointer(self):
        with self.assertRaises(subprocess.CalledProcessError): self.stage(fail=True)
        self.assertEqual(u.current(self.root)['version'], '0.4.0')
        self.assertFalse((self.root/'pending-runtime.json').exists())

    def test_next_idle_start_activates_prepared_runtime(self):
        self.stage(); u.write(self.root/'updates.json', {'automatic': True, 'channel': 'stable'})
        with patch.object(u, 'run_server', return_value=0) as call:
            self.assertEqual(u.serve(self.root, ['--profile', 'device']), 0)
            self.assertEqual(call.call_args.args[1], ['--profile', 'device'])
        self.assertEqual(u.current(self.root)['version'], '0.5.0')
        self.assertEqual(u.read(self.root/'previous-runtime.json')['version'], '0.4.0')
        self.assertEqual(list(self.root.glob('server-*.lock')), [])

    def test_disable_auto_prevents_pending_activation(self):
        self.stage(); u.write(self.root/'updates.json', {'automatic': False})
        with patch.object(u, 'run_server', return_value=0): u.serve(self.root, [])
        self.assertEqual(u.current(self.root)['version'], '0.4.0')

    def test_launcher_does_not_write_to_mcp_stdout(self):
        u.write(self.root/'update-state.json', {'checked_at': time.time(), 'available': True, 'release': self.release('0.5.0')})
        stdout = io.StringIO()
        with patch('sys.stdout', stdout), patch('sys.stderr', io.StringIO()), patch.object(u, 'run_server', return_value=0):
            u.serve(self.root, [])
        self.assertEqual(stdout.getvalue(), '')

    def test_cli_update_routes_flags(self):
        from saygo.cli import main
        with patch.object(u, 'main', return_value=0) as command:
            main(['update', '--auto', 'on'])
            command.assert_called_once_with(['--auto', 'on'])

    def test_changed_integration_requires_full_installer(self):
        installer = self.root/'installer.py'; installer.write_text('')
        archive = self.root/'source.zip'
        with zipfile.ZipFile(archive, 'w') as bundle: bundle.writestr('saygo-0.5.0/placeholder', '')
        with patch.object(u, 'asset', side_effect=[installer, archive]), patch.object(u, 'compatibility', return_value='different'), patch.object(u.subprocess, 'run') as run:
            result = u.apply(self.root, {'available': True, 'release': self.release('0.5.0')})
            self.assertIn('full installer', result['deferred']); run.assert_not_called()
