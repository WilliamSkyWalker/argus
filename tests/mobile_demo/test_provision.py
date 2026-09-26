"""Host-independent provisioning preserves install, boot and connect ordering."""
from argparse import Namespace
import contextlib
import os
import sys
import unittest
from unittest.mock import Mock, patch

from argus.devices import mobile, mobile_host_worker


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.calls = Mock()
        for name, value in [('install_android', {'name': 'Example'}),
                            ('install_ios', {'device': 'SIMULATOR_ID'}),
                            ('boot_device', {'device': 'DEVICE_ID'}),
                            ('connect', {'connected': True})]:
            call = self.stack.enter_context(patch.object(mobile, name, return_value=value))
            self.calls.attach_mock(call, name)
        for name in ('ensure_node', 'install_appium', 'install_drivers'):
            call = self.stack.enter_context(patch.object(mobile.toolchain, name))
            self.calls.attach_mock(call, name)

    def test_native_connect_implies_boot_for_both_platforms(self):
        for kind in ('android', 'ios'):
            with self.subTest(kind=kind):
                self.calls.reset_mock()
                result = mobile.provision(Namespace(platform=kind, name='Example', api=35,
                    boot=False, connect=True, headless=True, session='phone'))
                self.assertTrue(result['connection']['connected'])
                self.assertEqual([c[0] for c in self.calls.mock_calls],
                    ['install_' + kind, 'ensure_node', 'install_appium', 'install_drivers', 'boot_device', 'connect'])
                self.calls.boot_device.assert_called_once_with(kind,
                    'SIMULATOR_ID' if kind == 'ios' else 'Example', headless=True)
                self.calls.connect.assert_called_once_with(kind, 'DEVICE_ID', 'phone')

    def test_windows_worker_installs_without_connecting_on_host(self):
        payload = dict(home='/tmp/example', sdk_root='/tmp/example/sdk', operation='install',
                       accept_licenses=True, name='Example', api=35, boot=True, headless=True)
        with patch.dict(os.environ), patch.object(sys, 'path', list(sys.path)), patch.object(mobile_host_worker, 'configure_adb'):
            result = mobile_host_worker.execute(payload)
        self.assertEqual(result['boot']['device'], 'DEVICE_ID')
        self.assertEqual([c[0] for c in self.calls.mock_calls],
            ['install_android', 'ensure_node', 'install_appium', 'install_drivers', 'boot_device'])
        self.calls.install_drivers.assert_called_once_with(
            self.calls.ensure_node.return_value, self.calls.install_appium.return_value, ios=False)

    def test_install_without_boot_and_failed_install_never_connect(self):
        mobile.provision_runtime('android', 'Example')
        self.calls.boot_device.assert_not_called()
        self.calls.reset_mock()
        self.calls.install_android.side_effect = RuntimeError('download failed')
        with self.assertRaisesRegex(RuntimeError, 'download failed'):
            mobile.provision(Namespace(platform='android', name='Example', api=35,
                boot=True, connect=True, headless=False, session='phone'))
        self.assertEqual([c[0] for c in self.calls.mock_calls], ['install_android'])
