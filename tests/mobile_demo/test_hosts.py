from argparse import Namespace
import base64
import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from argus.devices import mobile, mobile_host, mobile_host_worker, mobile_relay


def install_args(**changes):
    values = dict(device_command="install", platform="android", host="auto", name="Argus",
                  api=35, accept_licenses=False, boot=False, connect=False, session=None,
                  headless=False, dry_run=False)
    return Namespace(**(values | changes))


class HostTests(unittest.TestCase):
    def test_windows_helpers_request_no_console(self):
        from argus.devices import toolchain
        with patch.object(mobile, "environment", return_value={}), patch.object(toolchain.os, "name", "nt"), patch.object(toolchain.subprocess, "CREATE_NO_WINDOW", 0x08000000, create=True), patch.object(toolchain.subprocess, "run", return_value=Mock(returncode=0, stdout="", stderr="")) as run:
            mobile.run(["adb.exe", "version"])
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)
            toolchain._run(["node.exe", "--version"])
            self.assertEqual(run.call_args.kwargs["creationflags"], 0x08000000)

    def test_private_adb_port_is_persisted_and_local_daemon_can_autostart(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {}, clear=True):
            root = Path(tmp) / "runtime"
            root.mkdir()
            (root / "host-adb.json").write_text('{"port": 15037}')
            with patch.object(mobile_host_worker.socket, "socket") as socket:
                self.assertEqual(mobile_host_worker.configure_adb(tmp), 15037)
                socket.assert_not_called()
            self.assertEqual(os.environ["ANDROID_ADB_SERVER_PORT"], "15037")
            self.assertEqual(os.environ["ADB_SERVER_SOCKET"], "tcp:15037")

    def test_private_adb_port_reaches_appium_capabilities(self):
        from argus.platforms.appium import AppiumPlatform
        with patch("argus.platforms.appium_server.AppiumServerManager") as manager, patch("appium.webdriver.Remote") as remote, patch.object(AppiumPlatform, "_detect_screen_size"):
            manager.return_value.ensure_running.return_value = "http://127.0.0.1:4723"
            AppiumPlatform().setup({"appium": {"os": "android", "device": "emulator-5554", "adb_port": 15037}})
        self.assertEqual(remote.call_args.kwargs["options"].to_capabilities()["appium:adbPort"], 15037)

    def test_relay_rejects_missing_token_before_dispatch(self):
        class Connection:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(b"POST /session HTTP/1.0\r\nContent-Length: 2\r\n\r\n{}")
            def settimeout(self, value):
                pass
            def sendall(self, value):
                self.output.extend(value)
        connection = Connection()
        with patch.object(mobile_relay, "relay_request") as dispatch:
            mobile_relay.handler({"token": "secret"})(connection, ("127.0.0.1", 10000), Mock())
        self.assertIn(b"403", connection.output)
        dispatch.assert_not_called()

    def test_relay_preserves_appium_errors_and_does_not_retry(self):
        class Connection:
            def __init__(self):
                self.output = bytearray()
            def makefile(self, *args):
                return io.BytesIO(b"POST /secret/session HTTP/1.0\r\nContent-Length: 2\r\n\r\n{}")
            def settimeout(self, value):
                pass
            def sendall(self, value):
                self.output.extend(value)
        for failure in (False, True):
            with self.subTest(transport_failure=failure):
                connection = Connection()
                response = {"status": 404, "body": base64.b64encode(b'{"value":{"error":"invalid session id"}}').decode()}
                with patch.object(mobile_relay, "relay_request", return_value=response,
                                  side_effect=RuntimeError("lost response") if failure else None) as dispatch:
                    mobile_relay.handler({"token": "secret"})(connection, ("127.0.0.1", 10000), Mock())
                self.assertEqual(dispatch.call_count, 1)
                self.assertIn(b"502" if failure else b"404", connection.output)
                self.assertIn(b"outcome may be unknown" if failure else b"invalid session id", connection.output)

    def test_windows_batch_quoting_preserves_sdk_paths_and_rejects_expansion(self):
        with patch.object(mobile.os, "name", "nt"):
            value = mobile.command([r"C:\Program Files\Android\sdkmanager.bat", "system-images;android-35;google_apis;x86_64"])
            self.assertIn('/c ""C:\\Program Files\\Android\\sdkmanager.bat"', value)
            self.assertNotIn('\\"', value)
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                mobile.command([r"C:\sdkmanager.bat", "%SECRET%"])

    def test_wsl_auto_falls_back_only_without_kvm(self):
        with patch.object(mobile_host, "is_wsl", return_value=True), patch.object(mobile_host, "has_kvm", return_value=False):
            self.assertEqual(mobile_host.select_host("auto", "android"), "windows")
            self.assertEqual(mobile_host.select_host("local", "android"), "local")
        with patch.object(mobile_host, "is_wsl", return_value=True), patch.object(mobile_host, "has_kvm", return_value=True):
            self.assertEqual(mobile_host.select_host("auto", "android"), "local")

    def test_other_hosts_are_not_silently_redirected(self):
        with patch.object(mobile_host, "is_wsl", return_value=False), patch.object(mobile_host.platform, "system", return_value="Linux"):
            self.assertEqual(mobile_host.select_host("auto", "android"), "local")
            with self.assertRaisesRegex(ValueError, "requires WSL"):
                mobile_host.select_host("windows", "android")
        with self.assertRaisesRegex(ValueError, "iOS"):
            mobile_host.select_host("windows", "ios")

    def test_dry_run_returns_blockers_without_install_or_consent(self):
        details = {"host": "windows", "blockers": ["insufficient disk"]}
        with patch.object(mobile_host, "plan", return_value=details), patch.object(mobile_host, "prepare_windows") as prepare, patch.object(mobile_host, "confirm_licenses") as consent:
            self.assertEqual(mobile_host.execute(install_args(dry_run=True)), details)
            prepare.assert_not_called()
            consent.assert_not_called()

    def test_preflight_rejects_low_disk_and_missing_acceleration(self):
        details = dict(home=r"C:\Example\Argus", sdk_root=r"C:\Example\sdk", free_bytes=1,
                       architecture="AMD64", hypervisor=True, whpx=False)
        with patch.object(mobile_host, "select_host", return_value="windows"), patch.object(mobile_host, "windows_info", return_value=details):
            result = mobile_host.plan(install_args())
        self.assertEqual(len(result["blockers"]), 2)
        with patch.object(mobile_host, "plan", return_value=result), patch.object(mobile_host, "prepare_windows") as prepare, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "Hypervisor"):
                mobile_host.execute(install_args(accept_licenses=True))
            prepare.assert_not_called()

    def test_noninteractive_requires_explicit_licenses(self):
        with patch.object(mobile_host.sys, "stdin", io.StringIO()), patch.object(mobile_host, "prepare_windows") as prepare:
            with self.assertRaisesRegex(RuntimeError, "--accept-licenses"):
                mobile_host.confirm_licenses(False)
            mobile_host.confirm_licenses(True)
            prepare.assert_not_called()

    def test_declining_license_prevents_bootstrap(self):
        with patch.object(mobile_host, "plan", return_value={"host": "windows", "blockers": []}), patch.object(mobile_host.sys.stdin, "isatty", return_value=True), patch("builtins.input", return_value="n"), patch.object(mobile_host, "prepare_windows") as prepare, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "cancelled"):
                mobile_host.execute(install_args())
            prepare.assert_not_called()

    def test_windows_install_keeps_alias_and_verifies_screenshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            args = install_args(accept_licenses=True, connect=True, session="phone")
            details = {"host": "windows", "blockers": []}
            controller = Mock()
            controller.screenshot_raw.return_value = b"screenshot"
            with patch.object(mobile_host, "plan", return_value=details), patch.object(mobile_host, "prepare_windows", return_value={}) as prepare, patch.object(mobile_host, "call_windows", return_value={"boot": {"device": "emulator-5554"}}), patch.object(mobile_host, "connect_windows", return_value={"session": "phone"}) as connect, patch("argus.platforms.device_session.attach", return_value=controller), patch.object(mobile, "home", return_value=Path(tmp)), contextlib.redirect_stderr(io.StringIO()):
                result = mobile_host.execute(args)
            prepare.assert_called_once_with(details, install=True)
            connect.assert_called_once_with({}, "emulator-5554", "phone")
            self.assertEqual(Path(result["verification_screenshot"]).read_bytes(), b"screenshot")

    def test_windows_failure_is_not_reported_as_connected(self):
        with patch.object(mobile_host, "plan", return_value={"host": "windows", "blockers": []}), patch.object(mobile_host, "prepare_windows", return_value={}), patch.object(mobile_host, "call_windows", side_effect=RuntimeError("download failed")), patch.object(mobile_host, "connect_windows") as connect, contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "download failed"):
                mobile_host.execute(install_args(accept_licenses=True, connect=True))
            connect.assert_not_called()

    def test_windows_boot_without_install_and_named_connection(self):
        args = Namespace(device_command="boot", host="windows", platform="android", device="Argus",
                         timeout=300, headless=True, connect=True, session="phone")
        with patch.object(mobile_host, "select_host", return_value="windows"), patch.object(mobile_host, "windows_info", return_value={}), patch.object(mobile_host, "prepare_windows", return_value={}) as prepare, patch.object(mobile_host, "call_windows", return_value={"device": "emulator-5554"}) as call, patch.object(mobile_host, "connect_windows", return_value={}) as connect:
            mobile_host.execute(args)
        prepare.assert_called_once_with({})
        call.assert_called_once_with({}, "boot", device="Argus", timeout=300, headless=True)
        connect.assert_called_once_with({}, "emulator-5554", "phone")

    def test_windows_worker_uses_json_stdin_not_shell_interpolation(self):
        host = {"python": "/mnt/c/Program Files/python.exe", "script": r"C:\Path With Spaces\worker.py",
                "home": r"C:\Example", "sdk_root": r"C:\Example\sdk"}
        with patch.object(mobile_host.subprocess, "run", return_value=Mock(returncode=0, stdout='{"ok": true}')) as run:
            result = mobile_host.call_windows(host, "boot", device="Argus")
        self.assertTrue(result["ok"])
        self.assertEqual(run.call_args.args[0], [host["python"], host["script"]])
        self.assertEqual(json.loads(run.call_args.kwargs["input"])["device"], "Argus")

    def test_transport_failure_never_replays_request(self):
        with patch.object(mobile_host, "call_windows", side_effect=RuntimeError("lost response")) as call:
            with self.assertRaisesRegex(RuntimeError, "lost response"):
                mobile_relay.relay_request({"host": {}, "port": 4723, "base_path": "/argus-example"}, "POST", "/session/example/actions", b"{}")
        self.assertEqual(call.call_count, 1)

    def test_host_http_targets_only_fixed_loopback_endpoint(self):
        with self.assertRaisesRegex(ValueError, "path"):
            mobile_host_worker.request_appium(4723, "/argus-example", "GET", "//example.com")
        response = Mock(status=200, headers={"Content-Type": "application/json"})
        response.read.return_value = b'{"value": {}}'
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock()
        opener.open.return_value = response
        with patch.object(mobile_host_worker.urllib.request, "build_opener", return_value=opener):
            result = mobile_host_worker.request_appium(4723, "/argus-example", "POST", "/session", base64.b64encode(b"{}").decode())
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:4723/argus-example/session")
        self.assertEqual(base64.b64decode(result["body"]), b'{"value": {}}')

    def test_bad_download_checksum_is_rejected(self):
        response = io.BytesIO(b"corrupted archive")
        with tempfile.TemporaryDirectory() as tmp, patch.object(mobile.urllib.request, "urlopen", return_value=response), contextlib.redirect_stderr(io.StringIO()):
            with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
                mobile.download("https://example.com/archive", Path(tmp) / "archive", "0" * 64)


if __name__ == "__main__":
    unittest.main()
