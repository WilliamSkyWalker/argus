import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from saygo.integrations.browser_bridge import Client, atomic_json, read_frame, write_frame
from saygo.platforms.browser_extension import ExtensionBrowserPlatform
from saygo.platforms.browser_playwright import PageSelectionError
from saygo.platforms import device_session as ds


class BridgeTests(unittest.TestCase):
    def test_stopping_extension_session_never_terminates_browser(self):
        with patch.object(ds, 'clear_state') as clear, patch('os.kill') as kill:
            ds._browser_stop('web', {'browser_backend': 'extension', 'pid': 123})
            clear.assert_called_once_with('web')
            kill.assert_not_called()

    def test_scroll_at_capability_and_percent_coordinates(self):
        from saygo.devices.actions import capabilities, prepare, dispatch
        p = ExtensionBrowserPlatform()
        self.assertIn('scroll_at', capabilities(p)['actions'])
        self.assertEqual(capabilities(p)['scroll_at_pixels_per_unit'], 100)
        with patch.object(p, '_call', return_value=[1000, 800]) as call:
            action = prepare(p, {'type': 'scroll_at', 'x': 25, 'y': 50,
                                 'coordinate_space': 'percent', 'amount': -3})
            dispatch(p, action)
            call.assert_called_with('scroll_at', x=250, y=400, amount=-3)

    def test_qa_scroll_uses_shared_fractional_amount_validation(self):
        p = ExtensionBrowserPlatform()
        with patch.object(p, '_call', return_value=[1000, 800]) as call:
            p.execute_action({'type': 'scroll_at', 'x_pct': 25, 'y_pct': 50, 'amount': -.6})
            call.assert_called_with('scroll_at', x=250, y=400, amount=-.6)
            with self.assertRaises(ValueError):
                p.execute_action({'type': 'scroll_at', 'x': 25, 'y': 50, 'amount': float('nan')})

    def test_frozen_windows_uses_separate_stdio_host(self):
        from saygo.integrations.browser_bridge import host_command
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp)/'SaygoDesktop.exe'
            host = Path(tmp)/'SaygoNativeHost.exe'
            with patch.object(sys, 'frozen', True, create=True), \
                 patch.object(sys, 'platform', 'win32'), patch.object(sys, 'executable', str(exe)):
                with self.assertRaisesRegex(FileNotFoundError, 'complete desktop ZIP'):
                    host_command(tmp)
                host.touch()
                self.assertEqual(host_command(tmp),
                                 [str(host), '--native-host', '--directory', tmp])

    def test_frame_unicode_partial_reads_and_limits(self):
        stream = io.BytesIO()
        write_frame(stream, {"text": "中文"})
        class Slow(io.BytesIO):
            def read(self, size):
                return super().read(min(size, 2))
        self.assertEqual(read_frame(Slow(stream.getvalue())), {"text": "中文"})
        with self.assertRaises(ValueError):
            read_frame(io.BytesIO(struct.pack("=I", 2**25)))
        with self.assertRaises(EOFError):
            read_frame(io.BytesIO(b"\x01"))

    def test_real_host_roundtrip_chunks_and_stale_epoch(self):
        self._host_roundtrip(["-m", "saygo.integrations.browser_bridge"])

    def test_legacy_module_host_roundtrip(self):
        self._host_roundtrip(["-m", "saygo.browser_bridge"])

    def test_installed_legacy_script_host_roundtrip(self):
        script = Path(__file__).resolve().parents[2] / "saygo" / "browser_bridge.py"
        self._host_roundtrip(["-I", "-S", str(script)])

    def _host_roundtrip(self, entry):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([sys.executable, *entry, "host", "--directory", tmp],
                                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                hello = read_frame(proc.stdout)
                self.assertEqual(hello['protocol'], 1)
                # A bad handshake must not close the host before a valid one.
                write_frame(proc.stdin, {'type':'hello', 'protocol':99, 'version':'test'})
                write_frame(proc.stdin, {'type':'hello', 'protocol':1, 'version':'test'})
                status = Path(tmp) / "status.json"
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if status.exists() and json.loads(status.read_text()).get('connected'):
                        break
                    time.sleep(.02)
                self.assertTrue(status.exists())
                errors = []
                def extension():
                    try:
                        request = read_frame(proc.stdout)
                        self.assertEqual(request["operation"], "pages")
                        text = json.dumps({"result": [{"page_id":"epoch:1", "title":"中文"}]})
                        for i, chunk in enumerate([text[:15],text[15:]]):
                            write_frame(proc.stdin, {"id":request["id"],"chunk":chunk,"last":i==1})
                    except Exception as exc:
                        errors.append(exc)
                t = threading.Thread(target=extension)
                t.start()
                self.assertEqual(Client(tmp, timeout=3).call("pages")[0]["title"], "中文")
                t.join(2)
                self.assertFalse(errors)
                rid = "a" * 32
                atomic_json(Path(tmp)/(rid+".request"), {"id":rid,"epoch":"old", "deadline":time.time()+10})
                response = Path(tmp)/(rid+".response")
                end = time.monotonic()+3
                while not response.exists() and time.monotonic()<end:
                    time.sleep(.02)
                self.assertIn("stale",json.loads(response.read_text())["error"])
            finally:
                proc.stdin.close()
                proc.wait(timeout=5)
                proc.stdout.close()
                proc.stderr.close()

    def test_timeout_does_not_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            atomic_json(Path(tmp)/"status.json", {"connected":True,"epoch":"test", "protocol":1})
            with self.assertRaisesRegex(RuntimeError,"outcome may be unknown"):
                Client(tmp, timeout=.15).call("tap", page_id="test:1",x=1,y=2)
            self.assertEqual(list(Path(tmp).glob("*.request")),[])

    def test_host_timeout_keeps_connection_and_ignores_late_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.Popen([
                sys.executable, '-c',
                'import sys; from saygo.integrations import browser_bridge as b; '
                'b.RESPONSE_TIMEOUT = .3; b.MAX_RESPONSE_BYTES = 64; b.serve(sys.argv[1])', tmp],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            errors = []
            requests = []
            worker = None
            try:
                read_frame(proc.stdout)
                write_frame(proc.stdin, {'type': 'hello', 'protocol': 1, 'version': 'test'})
                status = Path(tmp) / 'status.json'
                end = time.monotonic() + 5
                while time.monotonic() < end:
                    if status.exists() and json.loads(status.read_text()).get('connected'):
                        break
                    time.sleep(.02)

                def extension():
                    try:
                        first = read_frame(proc.stdout)
                        requests.append(first['operation'])
                        # No reply until the host times out and accepts a new call.
                        second = read_frame(proc.stdout)
                        requests.append(second['operation'])
                        for chunk, last in [('{"result":', False), ('"late"}', True)]:
                            write_frame(proc.stdin, {'id': first['id'], 'chunk': chunk, 'last': last})
                        write_frame(proc.stdin, {'id': second['id'],
                                                'chunk': '{"result": ["current"]}', 'last': True})
                        third = read_frame(proc.stdout)
                        requests.append(third['operation'])
                        write_frame(proc.stdin, {'id': third['id'],
                                                'chunk': json.dumps({'result': 'x' * 100}), 'last': True})
                        fourth = read_frame(proc.stdout)
                        requests.append(fourth['operation'])
                        time.sleep(.4)  # Longer than RESPONSE_TIMEOUT; within long-press budget.
                        write_frame(proc.stdin, {'id': fourth['id'], 'chunk': '{"result": {}}', 'last': True})
                    except Exception as exc:
                        errors.append(exc)

                worker = threading.Thread(target=extension, daemon=True)
                worker.start()
                with self.assertRaisesRegex(RuntimeError, 'extension timeout; outcome unknown'):
                    Client(tmp, timeout=3).call('scroll_at', page_id='test:1', x=10, y=20, amount=-3)
                after = json.loads(status.read_text())
                self.assertTrue(after['connected'])
                self.assertEqual(after['last_timeout']['operation'], 'scroll_at')
                self.assertIsNone(proc.poll())
                self.assertEqual(Client(tmp, timeout=3).call('pages'), ['current'])
                with self.assertRaisesRegex(RuntimeError, 'response exceeds'):
                    Client(tmp, timeout=3).call('screenshot')
                self.assertTrue(json.loads(status.read_text())['connected'])
                self.assertEqual(Client(tmp, timeout=3).call('long_press', duration=.3), {})
                worker.join(2)
                self.assertFalse(worker.is_alive())
                self.assertFalse(errors)
                self.assertEqual(requests, ['scroll_at', 'pages', 'screenshot', 'long_press'])
            finally:
                proc.stdin.close()
                proc.wait(timeout=5)
                if worker:
                    worker.join(2)
                proc.stdout.close()
                proc.stderr.close()
            self.assertFalse(json.loads(status.read_text())['connected'])

    def test_selection_and_session_backend_routing(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(ds,"STATE_DIR",Path(tmp)), patch("saygo.platforms.browser_extension.Client") as factory:
            client = factory.return_value
            client.call.return_value = [{"page_id":"epoch:1"},{"page_id":"epoch:2"}]
            ds.save_state("web",{"kind":"browser","browser_backend":"extension","bridge_directory":tmp})
            with self.assertRaises(PageSelectionError):
                ds.attach_browser("web")
            p = ds.attach_browser("web", manage_pages=True)
            p.select_page("epoch:2")
            self.assertEqual(ds.load_state("web")["page_id"],"epoch:2")
            self.assertEqual(ds.attach_browser("web").page_id,"epoch:2")
            with self.assertRaisesRegex(ValueError,"require backend"):
                ds.attach_browser("web",backend="playwright")
            p.disconnect()
            self.assertNotIn("close",[c.args[0] for c in client.call.call_args_list])

    def test_hidpi_screenshot_coordinates(self):
        import base64
        from PIL import Image
        raw=io.BytesIO()
        Image.new("RGB",(200,100)).save(raw,format="PNG")
        p=ExtensionBrowserPlatform()
        p._page_id="epoch:1"
        with patch.object(p,"_call",return_value={"data":base64.b64encode(raw.getvalue()).decode(),"size":[100,50]}):
            self.assertEqual(Image.open(io.BytesIO(p.screenshot_raw())).size,(100,50))
