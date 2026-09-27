"""Opt-in real Chromium/CDP integration, isolated profiles and local HTTP only.

SAYGO_TEST_CHROME=/path/to/chrome python3 -m unittest discover -s tests/browser_demo -v
"""

import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from PIL import Image

from saygo.platforms import device_session
from saygo.platforms.browser_playwright import PlaywrightBrowserPlatform, PageSelectionError
from saygo.runtime import Runtime, Store


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = b'''<!doctype html><title>Saygo browser fixture</title>
        <style>body {margin:0} input,button {display:block;width:200px;height:50px}</style>
        <input id="entry"><button id="login" onclick="window.open('/login','login')">Login</button>
        <div style="height:2000px">Local test page</div>'''
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@unittest.skipUnless(os.environ.get("SAYGO_TEST_CHROME"), "set SAYGO_TEST_CHROME for real-browser integration")
class ChromiumIntegration(unittest.TestCase):
    def test_popup_reconnect_runtime_handoff_and_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}"
            profile = root / "profile"
            # Test-only isolated browser; --no-sandbox supports restricted CI user namespaces.
            log = (root / "chrome.log").open("w+")
            self.addCleanup(log.close)
            proc = subprocess.Popen([os.environ["SAYGO_TEST_CHROME"], "--headless=new", "--no-sandbox",
                                     "--disable-dev-shm-usage", "--remote-debugging-port=0",
                                     f"--user-data-dir={profile}", "--no-first-run", "about:blank"],
                                    stdout=log, stderr=log)
            platform = None
            try:
                port_file = profile / "DevToolsActivePort"
                deadline = time.monotonic() + 20
                while not port_file.exists() and time.monotonic() < deadline and proc.poll() is None:
                    time.sleep(0.1)
                if not port_file.exists():
                    log.flush()
                    log.seek(0)
                    self.fail("Chromium did not start: " + log.read()[-3000:])
                port = int(port_file.read_text().splitlines()[0])
                endpoint = f"http://127.0.0.1:{port}"
                platform = PlaywrightBrowserPlatform().connect(endpoint)
                original = platform.page_id
                extra = platform.new_page(url + "/created")
                self.assertEqual(platform.page_id, original)
                platform.select_page(extra)
                self.assertTrue(platform.page.url.endswith("/created"))
                platform.close_page(extra)
                platform.select_page(original)
                platform.open_target(url)
                platform.tap(30, 25)
                platform.input_text("Saygo 中文")
                self.assertEqual(platform.page.locator("#entry").input_value(), "Saygo 中文")
                platform.press_key("select_all")
                platform.input_text("replaced")
                self.assertEqual(platform.page.locator("#entry").input_value(), "replaced")
                with Image.open(io.BytesIO(platform.screenshot_raw())) as shot:
                    self.assertEqual(shot.size, platform.screen_size)
                platform.page.evaluate("localStorage.setItem('session-marker','preserved')")
                with platform.page.expect_popup():
                    platform.tap(50, 80)
                pages = platform.list_pages()
                popup = next(p["page_id"] for p in pages if p["page_id"] != original)
                self.assertEqual(platform.page_id, original)
                self.assertTrue(any(e["kind"] == "page_opened" for e in platform.drain_events()))
                platform.select_page(popup)
                self.assertTrue(platform.page.url.endswith("/login"))
                platform.disconnect()
                platform = None
                self.assertIsNone(proc.poll(), "disconnect must preserve Chrome")

                # New Python process reconnects to the exact popup, not pages[0].
                script = '''import json,sys
from saygo.platforms.browser_playwright import PlaywrightBrowserPlatform
p=PlaywrightBrowserPlatform().connect(sys.argv[1], page_id=sys.argv[2])
try: print(json.dumps({'page_id':p.page_id,'url':p.page.url,'marker':p.page.evaluate("localStorage.getItem('session-marker')")}))
finally: p.disconnect()
'''
                out = subprocess.run([sys.executable, "-c", script, endpoint, popup], check=True,
                                     capture_output=True, text=True, timeout=30)
                data = json.loads(out.stdout)
                self.assertEqual(data["page_id"], popup)
                self.assertEqual(data["marker"], "preserved")

                sessions = root / "device-sessions"
                env = {**os.environ, "SAYGO_HOME_DIR": str(root)}
                with patch.object(device_session, "STATE_DIR", sessions):
                    device_session.save_state("web", {"kind": "browser", "browser_backend": "playwright",
                        "debugger_address": f"127.0.0.1:{port}", "port": port, "page_id": original})
                    command = [sys.executable, "-m", "saygo.cli", "device"]
                    out = subprocess.run(command + ["pages", "--serial", "web"], env=env,
                                         capture_output=True, text=True, check=True, timeout=30)
                    self.assertEqual(len(json.loads(out.stdout)["pages"]), 2)
                    # Playwright remains a CLI backend; MCP must reject it explicitly.
                    from saygo.mcp import server as mcp_server
                    from saygo.runtime.interactive import InteractiveRuntime
                    out = subprocess.run(command + ["tap", "30", "25", "--session", "web"], env=env,
                                         capture_output=True, text=True, check=True, timeout=30)
                    self.assertTrue(json.loads(out.stdout)["dispatched"])
                    result = mcp_server.device_input(" through MCP", serial="web")
                    self.assertFalse(result["ok"], result)
                    self.assertEqual(result["error_type"], "UnsupportedBackendError")
                    observed = mcp_server.device_screenshot(serial="web")
                    self.assertFalse(observed["ok"])
                    self.assertEqual([c.type for c in mcp_server.device_observe("web")], ["text"])

                    # Two resources share one live browser but retain distinct selected pages.
                    device_session.save_state("mail", {"kind":"browser", "browser_backend":"playwright",
                        "debugger_address":f"127.0.0.1:{port}", "page_id":popup})
                    interactive = InteractiveRuntime(Store(root / "runtime"))
                    task = interactive.create_task({"form":"web", "mail":"mail"})
                    task = interactive.observe(task["id"], "form")
                    kwargs = dict(resource="form", action={"type":"tap", "x":30, "y":25},
                                  observation_id=task["observations"]["form"]["id"], request_id="focus-form")
                    task = interactive.submit(task["id"], **kwargs)
                    self.assertEqual(task["status"], "idle", task)
                    self.assertIsNone(task["error"], task)
                    task = interactive.observe(task["id"], "mail")
                    self.assertEqual(task["observations"]["mail"]["page_id"], popup)
                    task_command = [sys.executable, "-m", "saygo.cli", "task"]
                    recovered = subprocess.run(task_command + ["recover", task["id"]], env=env,
                                               capture_output=True, text=True, check=True, timeout=30)
                    recovered = json.loads(recovered.stdout)
                    self.assertEqual(recovered["cursor"], 1)
                    self.assertEqual(recovered["observations"]["form"]["page_id"], original)
                    self.assertEqual(recovered["observations"]["mail"]["page_id"], popup)
                    interactive.handoff_task(task["id"], "Sign in to the test mailbox")
                    self.assertFalse(mcp_server.device_tap(30,25,serial="mail")["ok"])
                    task = interactive.resume_task(task["id"], "Test user returned control")
                    self.assertEqual(task["status"], "idle")
                    evidence = interactive.export(task["id"], str(root / "evidence.zip"))
                    self.assertTrue(Path(evidence["path"]).exists())
                    interactive.finish(task["id"], "Verified two-page operation and recovery")

                    store = Store(root / "runtime")
                    runtime = Runtime(store)
                    workflow = {"version": 1, "resources": {"web": {"kind": "browser", "session": "web"}},
                        "steps": [
                            {"id": "before", "kind": "observe", "resource": "web"},
                            {"id": "login", "kind": "human", "resource": "web", "instructions": "Log in", "verify_step": "verify"},
                            {"id": "after", "kind": "observe", "resource": "web"},
                            {"id": "verify", "kind": "check", "actual": {"$ref": "steps.after.url"}, "equals": url + "/done"}]}
                    state = runtime.run(runtime.create(workflow)["id"])
                    self.assertEqual(state["status"], "waiting_for_human")
                    self.assertEqual(state["bindings"]["web"]["page_id"], original)
                    # Simulate manual work; global CLI tab selection changes while paused.
                    platform = PlaywrightBrowserPlatform().connect(endpoint, page_id=original)
                    platform.open_target(url + "/done")
                    manual = device_session.attach_browser("web", page_id=popup)
                    manual.select_page(popup)
                    manual.disconnect()
                    self.assertEqual(device_session.load_state("web")["page_id"], popup)
                    result = Runtime(Store(root / "runtime")).resume(state["id"], "Logged in")
                    self.assertEqual(result["status"], "succeeded")
                    self.assertEqual(result["outputs"]["after"]["page_id"], original)
                    switched_workflow = {"version": 1,
                        "resources": {"web": {"kind": "browser", "session": "web", "page_id": original}},
                        "steps": [
                            {"id": "tabs", "kind": "pages", "resource": "web"},
                            {"id": "screen", "kind": "observe", "resource": "web"},
                            {"id": "switch", "kind": "action", "resource": "web",
                             "observation": {"$ref": "steps.screen"},
                             "action": {"type": "select_page", "page_id": popup}},
                            {"id": "popup_screen", "kind": "observe", "resource": "web"},
                            {"id": "navigate", "kind": "action", "resource": "web",
                             "observation": {"$ref": "steps.popup_screen"},
                             "action": {"type": "open_url", "url": url + "/next"}},
                            {"id": "result", "kind": "observe", "resource": "web"},
                            {"id": "correct_page", "kind": "check", "actual": {"$ref": "steps.result.page_id"}, "equals": popup},
                            {"id": "correct_url", "kind": "check", "actual": {"$ref": "steps.result.url"}, "equals": url + "/next"}]}
                    result = runtime.run(runtime.create(switched_workflow)["id"])
                    self.assertEqual(result["status"], "succeeded", result.get("error"))
                    self.assertEqual(result["bindings"]["web"]["page_id"], popup)
                    # device start must reuse the existing persistent process and selected tab.
                    out = subprocess.run(command + ["start", "--platform", "browser", "--backend", "playwright", "--serial", "web"],
                                         env=env, capture_output=True, text=True, check=True, timeout=30)
                    self.assertEqual(json.loads(out.stdout)["page_id"], popup)
                    # Explicit selection uses CLI and persists across controller processes.
                    out = subprocess.run(command + ["select-page", popup, "--serial", "web"], env=env,
                                         capture_output=True, text=True, check=True, timeout=30)
                    self.assertEqual(json.loads(out.stdout)["selected_page_id"], popup)
                    platform.close_page(popup)
                    with self.assertRaises(PageSelectionError):
                        device_session.attach_browser("web")
                    out = subprocess.run(command + ["screenshot", "--serial", "web", "--out", str(root / "closed.png")],
                                         env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(out.returncode, 2)
                    self.assertFalse(json.loads(out.stdout)["ok"])
                    self.assertFalse((root / "closed.png").exists())
                    # Management connection can recover selection even if the selected tab died.
                    out = subprocess.run(command + ["select-page", original, "--serial", "web"], env=env,
                                         capture_output=True, text=True, check=True, timeout=30)
                    self.assertEqual(json.loads(out.stdout)["selected_page_id"], original)
                    self.assertIsNone(proc.poll())
            finally:
                if platform is not None:
                    platform.disconnect()
                proc.terminate()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=10)
