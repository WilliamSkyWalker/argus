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
    def test_scroll_at_capability_and_percent_coordinates(self):
        from saygo.devices.actions import capabilities, prepare, dispatch
        p = ExtensionBrowserPlatform()
        self.assertIn('scroll_at', capabilities(p)['actions'])
        with patch.object(p, '_call', return_value=[1000, 800]) as call:
            action = prepare(p, {'type': 'scroll_at', 'x': 25, 'y': 50,
                                 'coordinate_space': 'percent', 'amount': -3})
            dispatch(p, action)
            call.assert_called_with('scroll_at', x=250, y=400, amount=-3)

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
