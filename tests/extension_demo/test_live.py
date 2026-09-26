"""Opt-in isolated Chromium extension + real native host end-to-end test."""
import io
import base64
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from argus.integrations.browser_bridge import install
from argus.platforms import device_session as ds
from argus.runtime import Runtime, Store
import subprocess
import sys
from argus.platforms.browser_extension import ExtensionBrowserPlatform


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'echo': payload.decode()}).encode())

    def do_GET(self):
        if self.path == '/ws':
            key = self.headers['Sec-WebSocket-Key'] + '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'
            self.send_response(101)
            self.send_header('Upgrade', 'websocket')
            self.send_header('Connection', 'Upgrade')
            self.send_header('Sec-WebSocket-Accept', base64.b64encode(hashlib.sha1(key.encode()).digest()).decode())
            self.end_headers()
            self.connection.settimeout(5)
            head = self.rfile.read(2)
            length = head[1] & 127
            mask = self.rfile.read(4)
            payload = self.rfile.read(length)
            decoded = bytes(value ^ mask[i % 4] for i, value in enumerate(payload))
            self.wfile.write(bytes([0x81, len(decoded)]) + decoded)
            self.wfile.flush()
            self.wfile.write(b'\x88\x00')
            return
        if self.path == '/events':
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            for i in range(2):
                self.wfile.write(f'id: {i}\ndata: message-{i}\n\n'.encode())
                self.wfile.flush()
                time.sleep(.15)
            return
        body = b'''<!doctype html><meta charset="utf-8"><title>Extension fixture</title>
        <style>body{margin:0}input,button{display:block;width:200px;height:50px}</style>
        <input><button onclick="window.open('/popup')">Popup</button>'''
        self.send_response(200)
        self.send_header("Content-Type","text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(body)
    def log_message(self,*args): pass


@unittest.skipUnless(os.environ.get("ARGUS_TEST_CHROME"),"set ARGUS_TEST_CHROME for real extension integration")
class ExtensionLive(unittest.TestCase):
    def test_native_host_existing_tab_visual_input_and_release(self):
        from playwright.sync_api import sync_playwright
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            root=Path(tmp)
            server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            extension=Path("extensions/argus-browser").resolve()
            env={**os.environ,"HOME":tmp,"XDG_CONFIG_HOME":str(root/".config")}
            context=pw.chromium.launch_persistent_context(str(root/"profile"),
                executable_path=os.environ["ARGUS_TEST_CHROME"],headless=True,
                env=env,args=["--no-sandbox",f"--disable-extensions-except={extension}",f"--load-extension={extension}"])
            try:
                worker=context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                eid=worker.url.split("/")[2]
                with patch("pathlib.Path.home",return_value=root):
                    install(root/"bridge",eid,"chrome")
                # Chromium and Chrome for Testing use different product directories.
                manifest=root/".config/google-chrome/NativeMessagingHosts/com.argus.browser.json"
                for name in ["chromium","google-chrome-for-testing"]:
                    dest=root/".config"/name/"NativeMessagingHosts/com.argus.browser.json"
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    dest.write_bytes(manifest.read_bytes())
                dest=root/"profile/NativeMessagingHosts/com.argus.browser.json"
                dest.parent.mkdir(parents=True,exist_ok=True)
                dest.write_bytes(manifest.read_bytes())
                page=context.pages[0]
                url=f"http://127.0.0.1:{server.server_port}"
                page.goto(url)
                page.evaluate("localStorage.setItem('session','already-logged-in')")
                # Exercise extension UI messages in its own origin, not production page scripts.
                ui=context.new_page()
                ui.goto(f"chrome-extension://{eid}/popup.html")
                page.bring_to_front()
                send=lambda kind: ui.evaluate("type => chrome.runtime.sendMessage({type})",kind)
                send("connect")
                end=time.monotonic()+10
                while not (root/"bridge/status.json").exists() and time.monotonic()<end:
                    page.wait_for_timeout(100)
                self.assertTrue((root/"bridge/status.json").exists(),send("status"))
                available=send("status")["pages"]
                self.assertEqual(len(available),1)
                pid=available[0]["page_id"]
                platform=ExtensionBrowserPlatform().connect(root/"bridge",pid)
                # No network start command: connection automatically enables observation.
                self.assertTrue(platform.network()['active'])
                page.evaluate("""async () => {
                    await fetch('/api', {method:'POST', body:'network-test'});
                    await Promise.all([
                      new Promise((resolve,reject) => {
                        const ws = new WebSocket(location.origin.replace('http','ws')+'/ws');
                        ws.onopen = () => ws.send('hello-ws');
                        ws.onmessage = e => { if(e.data === 'hello-ws') resolve(); else reject(e.data); };
                        ws.onerror = reject;
                      }),
                      new Promise((resolve,reject) => {
                        const es = new EventSource('/events'); let count=0;
                        es.onmessage = () => { if(++count === 2) { es.close(); resolve(); } };
                        es.onerror = reject;
                      }),
                      fetch('/events').then(r => r.text())
                    ]);
                }""")
                deadline=time.monotonic()+5
                while time.monotonic()<deadline:
                    traffic=platform.network(limit=200)['events']
                    kinds={row['kind'] for row in traffic}
                    if {'http.body','ws.sent','ws.received','sse.message','stream.chunk'} <= kinds:
                        break
                    page.wait_for_timeout(100)
                self.assertTrue({'http.body','ws.sent','ws.received','sse.message','stream.chunk'} <= kinds, traffic)
                self.assertTrue(any('network-test' in row.get('body','') for row in traffic),traffic)
                self.assertTrue(any(row.get('data')=='hello-ws' and row['kind']=='ws.received' for row in traffic))
                platform.network('stop')
                self.assertFalse(platform.network()['active'])
                count = platform.network()['retained']
                page.reload()
                self.assertFalse(platform.network()['active'])
                self.assertEqual(platform.network()['retained'], count)
                platform.network('start')
                platform.network('clear')
                self.assertEqual(platform.network()['events'], [])
                platform.tap(40,25)
                platform.input_text("中文 existing session")
                self.assertEqual(page.locator("input").input_value(),"中文 existing session")
                shot=platform.screenshot_raw()
                self.assertEqual(Image.open(io.BytesIO(shot)).size,platform.screen_size)
                self.assertEqual(page.evaluate("localStorage.getItem('session')"),"already-logged-in")
                platform.disconnect()
                platform=ExtensionBrowserPlatform().connect(root/"bridge",pid)
                platform.press_key("select_all")
                platform.input_text("reconnected")
                self.assertEqual(page.locator("input").input_value(),"reconnected")
                with page.expect_popup() as popup_event:
                    platform.tap(50,80)
                popup=popup_event.value
                self.assertEqual(len(platform.list_pages()),2,"new popup must be available automatically")
                popup.bring_to_front()
                shared=send("status")
                self.assertEqual(len(shared["pages"]),2)
                self.assertEqual(platform.page_id,pid)
                popup_id=next(p["page_id"] for p in shared["pages"] if p["page_id"]!=pid)
                platform.select_page(popup_id)
                self.assertTrue(platform.observation_metadata()["url"].endswith("/popup"))
                platform.close_page(popup_id)
                with self.assertRaises(RuntimeError): platform.tap(1,1)
                platform.select_page(pid)
                # Real CLI reconnect and durable Runtime handoff use the same adapter.
                env_cli = {**os.environ, "ARGUS_HOME_DIR":str(root/"argus-home")}
                bound = subprocess.run([sys.executable,"-m","argus.cli","device","connect","--platform","browser","--backend","extension",
                    "--bridge-directory",str(root/"bridge"),"--serial","daily-web"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(bound.returncode,0,bound.stderr)
                out = subprocess.run([sys.executable,"-m","argus.cli","device","pages",
                    "--serial","daily-web"],env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(out.returncode,0,out.stderr)
                self.assertEqual(json.loads(out.stdout)["pages"][0]["page_id"],pid)
                command=[sys.executable,"-m","argus.cli","device"]
                traffic_cli=subprocess.run(command+["network","read","--session","daily-web","--limit","5"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(traffic_cli.returncode,0,traffic_cli.stderr+traffic_cli.stdout)
                self.assertTrue(json.loads(traffic_cli.stdout)['active'])
                created=subprocess.run(command+["new-page",url+"/created","--serial","daily-web"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(created.returncode,0,created.stderr+created.stdout)
                created_id=json.loads(created.stdout)["created_page_id"]
                self.assertNotEqual(created_id,pid)
                chosen=subprocess.run(command+["select-page",created_id,"--serial","daily-web"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(chosen.returncode,0,chosen.stderr+chosen.stdout)
                self.assertEqual(json.loads(chosen.stdout)["selected_page_id"],created_id)
                new_traffic=subprocess.run(command+["network","--session","daily-web"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(new_traffic.returncode,0,new_traffic.stderr+new_traffic.stdout)
                self.assertTrue(json.loads(new_traffic.stdout)['active'])
                closed=subprocess.run(command+["close-page",created_id,"--serial","daily-web"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(closed.returncode,0,closed.stderr+closed.stdout)
                self.assertNotIn(created_id,[p["page_id"] for p in json.loads(closed.stdout)["pages"]])
                with patch.object(ds,"STATE_DIR",root/"argus-home/device-sessions"):
                    restored=ds.attach_browser("daily-web",manage_pages=True)
                    restored.select_page(pid)
                    restored.disconnect()
                    runtime=Runtime(Store(root/"runtime"))
                    workflow={"version":1,"resources":{"web":{"kind":"browser","session":"daily-web","backend":"extension"}},
                        "steps":[
                            {"id":"before","kind":"observe","resource":"web"},
                            {"id":"manual","kind":"human","resource":"web","instructions":"Navigate manually","verify_step":"verify"},
                            {"id":"after","kind":"observe","resource":"web"},
                            {"id":"verify","kind":"check","actual":{"$ref":"steps.after.url"},"equals":url+"/done"}]}
                    state=runtime.run(runtime.create(workflow)["id"])
                    self.assertEqual(state["status"],"waiting_for_human",state.get("error"))
                    page.goto(url+"/done")
                    state=runtime.resume(state["id"],"Manual navigation complete")
                    self.assertEqual(state["status"],"succeeded",state.get("error"))
                    self.assertEqual(state["bindings"]["web"]["page_id"],pid)
                    workflow["steps"]=[
                        {"id":"before","kind":"observe","resource":"web"},
                        {"id":"create","kind":"action","resource":"web","observation":{"$ref":"steps.before"},
                         "action":{"type":"new_page","url":url+"/runtime"}},
                        {"id":"verify","kind":"check","actual":{"$ref":"steps.create.page_id"},"equals":pid}]
                    created_state=runtime.run(runtime.create(workflow)["id"])
                    self.assertEqual(created_state["status"],"succeeded",created_state.get("error"))
                    new_id=created_state["outputs"]["create"]["created_page_id"]
                    platform.close_page(new_id)
                send("release")
                with self.assertRaises(RuntimeError): platform.tap(1,1)
                self.assertEqual(send("status")["pages"],[])
                self.assertFalse(page.is_closed())
            finally:
                context.close()
