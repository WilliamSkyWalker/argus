import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from saygo.devices import mobile
from saygo.platforms import device_session as ds


class MobileTests(unittest.TestCase):
    def test_wsl_discovery_uses_windows_host_when_linux_adb_missing(self):
        from saygo.devices import mobile_host
        result = {'devices':[{'id':'emulator-5554','platform':'android','connectable':True}],
                  'android_avds':['Saygo'],'diagnostics':[]}
        with patch.object(mobile.platform, 'system', return_value='Linux'), \
             patch.object(mobile.platform, 'release', return_value='microsoft-WSL2'), \
             patch.object(mobile, 'android_devices', return_value=[]), \
             patch.object(mobile.toolchain, 'detect_adb', return_value=None), \
             patch.object(mobile_host, 'windows_info', return_value={}), \
             patch.object(mobile_host, 'prepare_windows', return_value={'python':'host-python'}), \
             patch.object(mobile_host, 'call_windows', return_value=result) as call:
            found = mobile.discover('android')
        self.assertEqual(found['host'], 'windows')
        self.assertEqual(found['devices'][0]['id'], 'emulator-5554')
        self.assertEqual(call.call_args.args[1], 'discover')

    def test_missing_mobile_dependency_keeps_named_binding(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ds, 'STATE_DIR', Path(tmp)):
            state = {'os':'android', 'server_url':'http://localhost:4723', 'session_id':'saved'}
            ds.save_state('phone', state)
            with patch.object(ds, '_attach_driver', side_effect=ImportError('appium')):
                with self.assertRaisesRegex(RuntimeError, 'dependencies'):
                    ds.attach('phone')
            self.assertEqual(ds.load_state('phone'), state)

    def test_android_online_unauthorized_offline(self):
        raw='List of devices attached\nphone device product:test model:Example_Phone\nemulator-5554 device model:sdk\nlocked unauthorized\nlost offline\n'
        with patch.object(mobile,'run',return_value=raw):
            rows=mobile.android_devices()
        self.assertEqual([r['connectable'] for r in rows],[True,True,False,False])
        self.assertEqual(rows[1]['type'],'emulator')

    def test_missing_one_platform_does_not_hide_other(self):
        with patch.object(mobile.platform,'system',return_value='Darwin'), patch.object(mobile,'android_devices',return_value=[{'id':'phone'}]), patch.object(mobile,'ios_simulators',side_effect=RuntimeError('Xcode missing')), patch.object(mobile,'ios_physical',return_value=[]):
            result=mobile.discover()
        self.assertEqual(result['devices'],[{'id':'phone'}])
        self.assertTrue(any(d['source']=='ios_simulators' for d in result['diagnostics']))

    def test_multiple_devices_require_selection(self):
        result={'devices':[{'id':'one','connectable':True},{'id':'two','connectable':True}]}
        with patch.object(mobile,'discover',return_value=result), patch.object(ds,'start') as start:
            with self.assertRaisesRegex(RuntimeError,'exactly one'):
                mobile.connect('android')
            start.assert_not_called()

    def test_connect_preserves_device_and_signing(self):
        platform=Mock(); platform.screen_size=(100,200)
        with patch.object(ds,'load_state',return_value=None), patch.object(ds,'start',return_value=platform) as start:
            result=mobile.connect('ios','DEVICE_ID','phone','http://mac.example:4723','TEAM_PLACEHOLDER')
        cfg=start.call_args.kwargs['appium_config']
        self.assertEqual(cfg['device'],'DEVICE_ID')
        self.assertEqual(cfg['team_id'],'TEAM_PLACEHOLDER')
        self.assertFalse(cfg['auto_start'])
        self.assertEqual(result['session'],'phone')

    def test_conflicting_saved_device_is_rejected(self):
        with patch.object(ds,'load_state',return_value={'os':'ios','device_id':'other'}), patch.object(ds,'start') as start:
            with self.assertRaisesRegex(ValueError,'different device'):
                mobile.connect('ios','DEVICE_ID','phone','http://mac.example:4723')
            start.assert_not_called()

    def test_ios_install_requires_mac_before_download(self):
        with patch.object(mobile.platform,'system',return_value='Linux'), patch.object(mobile,'run') as run:
            with self.assertRaisesRegex(RuntimeError,'macOS'):
                mobile.install_ios('Saygo')
            run.assert_not_called()

    def test_ios_discovery_uses_udid_not_coredevice_identifier(self):
        payload={'result':{'devices':[{'identifier':'COREDEVICE_ID','hardwareProperties':{'udid':'USB_UDID','platform':'iOS'},'deviceProperties':{'name':'Phone'},'connectionProperties':{'pairingState':'paired','tunnelState':'connected'}}]}}
        def run(args,**kwargs): Path(args[-1]).write_text(json.dumps(payload))
        with patch.object(mobile,'run',side_effect=run): rows=mobile.ios_physical()
        self.assertEqual(rows[0]['id'],'USB_UDID')
        self.assertTrue(rows[0]['connectable'])

    def test_ios_install_picks_latest_version_and_reuses(self):
        runtimes={'runtimes':[{'identifier':'com.apple.CoreSimulator.SimRuntime.iOS-9-0','version':'9.0','isAvailable':True},{'identifier':'com.apple.CoreSimulator.SimRuntime.iOS-18-0','version':'18.0','isAvailable':True}]}
        with patch.object(mobile,'require_mac'), patch.object(mobile,'run',side_effect=['','',json.dumps(runtimes)]) as run, patch.object(mobile,'ios_simulators',return_value=[{'name':'Saygo','id':'SIM_ID','runtime':runtimes['runtimes'][1]['identifier']}]):
            result=mobile.install_ios('Saygo')
        self.assertEqual(result['device'],'SIM_ID')
        self.assertFalse(result['created'])
        self.assertEqual(run.call_count,3)

    def test_ios_boot_already_booted_is_idempotent(self):
        with patch.object(mobile,'require_mac'), patch.object(mobile,'ios_simulators',return_value=[{'id':'SIM_ID','name':'Saygo','state':'Booted'}]), patch.object(mobile,'run',return_value='') as run:
            self.assertEqual(mobile.boot_device('ios','SIM_ID',headless=True)['state'],'ready')
        self.assertEqual(run.call_args.args[0],['xcrun','simctl','bootstatus','SIM_ID','-b'])

    def test_android_install_commands_and_no_force(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); (root/'cmdline-tools/latest/bin').mkdir(parents=True)
            (root/'cmdline-tools/latest/bin/sdkmanager').touch()
            (root/'cmdline-tools/latest/bin/avdmanager').touch()
            with patch.object(mobile,'sdk_root',return_value=root), patch.object(mobile,'ensure_java'), patch.object(mobile.platform,'system',return_value='Linux'), patch.object(mobile.platform,'machine',return_value='x86_64'), patch.object(mobile,'run',return_value='') as run:
                result=mobile.install_android('Saygo',35,True)
            self.assertTrue(result['created'])
            calls=[list(map(str,c.args[0])) for c in run.call_args_list]
            self.assertIn('system-images;android-35;google_apis;x86_64',calls[1])
            self.assertIn('build-tools;35.0.0',calls[1])
            self.assertIn('platforms;android-35',calls[1])
            self.assertNotIn('--force',calls[-1])
            self.assertIn('no\n',run.call_args.kwargs['input'])

    def test_invalid_names_rejected_before_install(self):
        with patch.object(mobile,'ensure_java') as java:
            for name in ['../outside','Saygo;bad','x\ny']:
                with self.assertRaises(ValueError): mobile.install_android(name)
            java.assert_not_called()

    def test_archive_traversal_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            archive=Path(tmp)/'bad.zip'
            with zipfile.ZipFile(archive,'w') as z: z.writestr('../escape','data')
            with self.assertRaises(ValueError): mobile.unpack(archive,Path(tmp)/'target')
            self.assertFalse((Path(tmp)/'escape').exists())

    def test_android_boot_reuses_exact_avd(self):
        rows=[{'id':'emulator-5554','type':'emulator','connectable':True}]
        with patch.object(mobile,'android_devices',return_value=rows), patch.object(mobile,'run',side_effect=['Saygo','Saygo\nOK','1','Saygo\nOK']), patch.object(mobile.subprocess,'Popen') as spawn:
            result=mobile.boot_device('android','Saygo')
        self.assertEqual(result['device'],'emulator-5554')
        spawn.assert_not_called()

    def test_boot_waits_for_new_adb_server_to_discover_existing_emulator(self):
        rows=[{'id':'emulator-5554','type':'emulator','connectable':True}]
        with patch.object(mobile,'android_devices',side_effect=[[],rows]), patch.object(mobile,'run',side_effect=['Saygo','Saygo\nOK','1','Saygo\nOK']), patch.object(mobile.time,'sleep'), patch.object(mobile.subprocess,'Popen') as spawn:
            result=mobile.boot_device('android','Saygo')
        self.assertEqual(result['device'],'emulator-5554')
        spawn.assert_not_called()

    def test_boot_rejects_wrong_port_owner(self):
        rows=[{'id':'emulator-5554','type':'emulator','connectable':True}]
        with patch.object(mobile,'android_devices',return_value=rows), patch.object(mobile,'run',side_effect=['Saygo','Saygo\nOK','1','Other\nOK']):
            with self.assertRaisesRegex(ValueError,'another AVD'): mobile.boot_device('android','Saygo')

    def test_boot_timeout_is_not_ready(self):
        rows=[{'id':'emulator-5554','type':'emulator','connectable':True}]
        with patch.object(mobile,'android_devices',return_value=rows), patch.object(mobile,'run',side_effect=['Saygo','Saygo\nOK']), patch.object(mobile.time,'monotonic',side_effect=[0,300]):
            with self.assertRaisesRegex(RuntimeError,'timed out'): mobile.boot_device('android','Saygo')

    def test_session_persists_physical_id_separately_from_alias(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ds,'STATE_DIR',Path(tmp)), patch.object(ds,'attach',return_value=None), patch('saygo.platforms.appium.AppiumPlatform') as factory:
            p=factory.return_value
            p._server_url='http://mac.example:4723'; p._os='ios'
            p._driver.session_id='SESSION_ID'; p._screen_width=100; p._screen_height=200
            ds.start('alias','ios',appium_config={'device':'USB_UDID','team_id':'TEAM_PLACEHOLDER'})
            saved=ds.load_state('alias')
            self.assertEqual(saved['device_id'],'USB_UDID')
            self.assertEqual(saved['serial'],'alias')
            self.assertEqual(p.setup.call_args.args[0]['appium']['team_id'],'TEAM_PLACEHOLDER')

    def test_driver_install_failure_is_not_success(self):
        from saygo.devices import toolchain
        with tempfile.TemporaryDirectory() as tmp, patch.object(toolchain,'APPIUM_HOME',Path(tmp)), patch.object(toolchain,'_run',side_effect=[Mock(stdout='{}',returncode=0),Mock(stderr='install failed',returncode=1)]):
            with self.assertRaisesRegex(RuntimeError,'install failed'):
                toolchain.install_drivers('/node','/appium',ios=False)

    def test_reinstall_preserves_existing_avd(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp); tools=root/'cmdline-tools/latest/bin'; tools.mkdir(parents=True)
            (tools/'sdkmanager').touch(); (tools/'avdmanager').touch()
            avds=root/'avds'; avds.mkdir(); data=avds/'Saygo.avd'; data.mkdir()
            (avds/'Saygo.ini').write_text('path='+str(data)+'\n')
            (data/'config.ini').write_text('image.sysdir.1=system-images/android-35/google_apis/x86_64/\n')
            with patch.dict(os.environ,{'ANDROID_AVD_HOME':str(avds)}), patch.object(mobile,'sdk_root',return_value=root), patch.object(mobile,'ensure_java'), patch.object(mobile.platform,'system',return_value='Linux'), patch.object(mobile.platform,'machine',return_value='x86_64'), patch.object(mobile,'run',side_effect=['','','Saygo']) as run:
                self.assertFalse(mobile.install_android('Saygo',35,True)['created'])
                self.assertEqual(run.call_count,3)
