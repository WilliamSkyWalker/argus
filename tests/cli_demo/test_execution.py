"""Behavioral regressions for the shared QA execution paths; no LLM or devices."""
import os
import unittest
from unittest.mock import Mock, patch
from argus.qa import execution


class ExecutionTests(unittest.TestCase):
    def agent(self, config):
        agent = Mock()
        agent.platform.platform_name = config['platform']
        agent.run.return_value = {'result': 'pass', 'steps': 1, 'duration': 0, 'steps_detail': []}
        return agent

    def test_sequential_and_grid_skip_before_reset_and_share_browser_preparation(self):
        cases = ['**Platform**: ios\nDo not run', '- **Automation**: manual\nDo not run',
                 'open_url https://example.test/start\nCheck page']
        for mode in ('sequential', 'grid'):
            with self.subTest(mode=mode):
                agent = self.agent({'platform': 'browser'})
                with patch.object(execution, '_create_agent', return_value=agent), \
                     patch.object(execution.time, 'sleep'), patch.dict(os.environ, {'PROBES_MODE': 'all'}):
                    if mode == 'sequential':
                        results = execution._run_sequential({'platform': 'browser'}, cases, None)
                    else:
                        results = execution._run_concurrent({'platform': 'browser'}, cases, None, 1)
                self.assertEqual([r['result'] for r in results], ['skipped', 'skipped', 'pass'])
                agent.run.assert_called_once_with(cases[2])
                agent.platform._driver.delete_all_cookies.assert_called_once()
                agent.platform._driver.execute_script.assert_called_once()
                agent.platform.open_target.assert_called_once_with('https://example.test/start')

    def test_grid_failure_retains_case_identity_and_releases_agent(self):
        agent = self.agent({'platform': 'browser'})
        agent.run.side_effect = [RuntimeError('example failure'),
                                 {'result': 'pass', 'steps': 1, 'duration': 0, 'steps_detail': []}]
        with patch.object(execution, '_create_agent', return_value=agent):
            results = execution._run_concurrent({'platform': 'browser'}, ['first', 'second'], None, 1)
        self.assertEqual([(r['case'], r['result']) for r in results], [('first', 'error'), ('second', 'pass')])
        self.assertEqual(results[0]['steps_detail'], [])
        agent.platform.teardown.assert_called_once()

    def test_device_start_failure_preserves_surviving_account_and_case_order(self):
        agent = self.agent({'platform': 'android'})
        cases = ['**Platform**: ios\nSkip', '**Reset before**: none\nRun ${EMAIL}']
        with patch.object(execution, '_create_agent', side_effect=[RuntimeError('offline'), agent]), \
             patch.object(execution, '_check_or_reconnect_device', return_value=True), \
             patch.object(execution, '_reset_android_state') as reset, \
             patch.dict(os.environ, {'PROBES_MODE': 'all'}):
            results = execution._run_dispatched_devices({'platform': 'android'}, cases,
                ['device-a', 'device-b'], [{'email': 'a@example.test'}, {'email': 'b@example.test'}], None)
        self.assertEqual([r['result'] for r in results], ['skipped', 'pass'])
        self.assertEqual([r['device'] for r in results], ['device-b', 'device-b'])
        self.assertIn('b@example.test', results[1]['case'])
        reset.assert_called_once_with(agent.platform, 'none')
        agent.platform.teardown.assert_called_once()

    def test_browser_cleanup_failure_still_navigates(self):
        platform = Mock()
        platform._driver.delete_all_cookies.side_effect = RuntimeError('unsupported')
        with patch.object(execution.time, 'sleep'):
            execution._prepare_browser_case(platform, 'open_url https://example.test', None)
        platform.open_target.assert_called_once_with('https://example.test')
