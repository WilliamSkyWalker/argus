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

from saygo.integrations.browser_bridge import install
from saygo.platforms import device_session as ds
from saygo.runtime import Runtime, Store
import subprocess
import sys
from saygo.platforms.browser_extension import ExtensionBrowserPlatform


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        payload = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps({'echo': payload.decode()}).encode())

    def do_GET(self):
        if self.path in ('/scroll', '/capture'):
            name = 'scroll_fixture.html' if self.path == '/scroll' else 'capture_fixture.html'
            body = Path(__file__).with_name(name).read_bytes()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write(body)
            return
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


@unittest.skipUnless(os.environ.get("SAYGO_TEST_CHROME"),"set SAYGO_TEST_CHROME for real extension integration")
class ExtensionLive(unittest.TestCase):
    def test_native_host_existing_tab_visual_input_and_release(self):
        self.run_live(self.check_capture_and_scroll)

    def test_background_tab_input_after_manual_tab_switch(self):
        self.run_live(self.check_background_input)

    def run_live(self, check):
        from playwright.sync_api import sync_playwright
        from PIL import Image
        with tempfile.TemporaryDirectory() as tmp, sync_playwright() as pw:
            root=Path(tmp)
            server=ThreadingHTTPServer(("127.0.0.1",0),Handler)
            threading.Thread(target=server.serve_forever,daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            extension=Path(os.environ.get("SAYGO_TEST_EXTENSION", "extensions/saygo-browser")).resolve()
            env={**os.environ,"HOME":tmp,"XDG_CONFIG_HOME":str(root/".config")}
            context=pw.chromium.launch_persistent_context(str(root/"profile"),
                no_viewport=True, executable_path=os.environ["SAYGO_TEST_CHROME"],headless=os.environ.get("SAYGO_TEST_HEADED") != "1",
                ignore_default_args=["--disable-background-timer-throttling",
                                     "--disable-backgrounding-occluded-windows",
                                     "--disable-renderer-backgrounding"],
                env=env,args=["--no-sandbox",f"--disable-extensions-except={extension}",f"--load-extension={extension}"])
            try:
                worker=context.service_workers[0] if context.service_workers else context.wait_for_event("serviceworker")
                eid=worker.url.split("/")[2]
                with patch("pathlib.Path.home",return_value=root):
                    install(root/"bridge",eid,"chrome")
                # Chromium and Chrome for Testing use different product directories.
                manifest=root/".config/google-chrome/NativeMessagingHosts/com.saygo.browser.json"
                for name in ["chromium","google-chrome-for-testing"]:
                    dest=root/".config"/name/"NativeMessagingHosts/com.saygo.browser.json"
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    dest.write_bytes(manifest.read_bytes())
                dest=root/"profile/NativeMessagingHosts/com.saygo.browser.json"
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
                env_cli = {**os.environ, "SAYGO_HOME_DIR":str(root/"saygo-home")}
                bound = subprocess.run([sys.executable,"-m","saygo.cli","device","connect","--platform","browser","--backend","extension",
                    "--bridge-directory",str(root/"bridge"),"--serial","daily-web"],
                    env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(bound.returncode,0,bound.stderr)
                out = subprocess.run([sys.executable,"-m","saygo.cli","device","pages",
                    "--serial","daily-web"],env=env_cli,capture_output=True,text=True,timeout=10)
                self.assertEqual(out.returncode,0,out.stderr)
                self.assertEqual(json.loads(out.stdout)["pages"][0]["page_id"],pid)
                command=[sys.executable,"-m","saygo.cli","device"]
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
                with patch.object(ds,"STATE_DIR",root/"saygo-home/device-sessions"):
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
                check(context, page, ui, platform, url, root)
                send("release")
                with self.assertRaises(RuntimeError): platform.tap(1,1)
                self.assertEqual(send("status")["pages"],[])
                self.assertFalse(page.is_closed())
            finally:
                context.close()

    def check_background_input(self, context, page, ui, platform, url, root):
        """Exercise the native bridge with an actually inactive owned tab."""
        page.goto(url+'/scroll')
        page.evaluate("window.fixtureClicks=0; document.body.addEventListener('click',()=>window.fixtureClicks++)")
        # Playwright enables focus emulation by default. Disable that test-only
        # assistance so switching tabs resembles a user's ordinary browser.
        session = context.new_cdp_session(page)
        session.send('Emulation.setFocusEmulationEnabled', {'enabled':False})
        tab = int(platform.page_id.split(':')[-1])
        window = ui.evaluate('id => chrome.tabs.get(id)', tab)['windowId']
        other = ui.evaluate('o => chrome.tabs.create(o)',
                            {'windowId':window, 'url':url+'/inactive', 'active':True})
        epoch = json.loads((root/'bridge/status.json').read_text())['epoch']
        try:
            for index in range(12):
                completed = False
                with self.subTest(background_switch=index):
                    # Chrome APIs arrange fixtures; only the bridge sends input
                    # and captures screenshots of the target.
                    ui.evaluate('id => chrome.tabs.update(id,{active:true})', tab)
                    platform.screenshot_raw()
                    ui.evaluate('id => chrome.tabs.update(id,{active:true})', other['id'])
                    before = platform.diagnose()
                    self.assertFalse(before['tab_active'])
                    platform.screenshot_raw()
                    platform.tap(150,100)
                    platform.scroll_at(150,200,-1 if index % 2 == 0 else 1)
                    platform.screenshot_raw()
                    after = platform.diagnose()
                    for key in ('window_id','window_state','window_focused','tab_active'):
                        self.assertEqual(after[key],before[key],key)
                    self.assertTrue(after['connection_retained'])
                    self.assertFalse(after['last_capture']['pending'])
                    self.assertEqual(page.evaluate('window.fixtureClicks'),index+1)
                    offsets = page.evaluate('[left.scrollTop,right.scrollTop,window.scrollY]')
                    self.assertAlmostEqual(offsets[0],100 if index % 2 == 0 else 0,delta=3)
                    self.assertEqual(offsets[1:],[0,0])
                    self.assertEqual(json.loads((root/'bridge/status.json').read_text())['epoch'],epoch)
                    completed = True
                # An ambiguous input must not be followed by another test input.
                if not completed:
                    break
        finally:
            ui.evaluate('id => chrome.tabs.remove(id)', other['id'])
            session.detach()

    def check_capture_and_scroll(self, context, page, ui, platform, url, root):
        """Chrome APIs arrange isolated fixtures; all captures/input use the bridge."""
        from PIL import Image
        page.goto(url+'/capture')
        # Stop the fixture's animation for exact pixel assertions. This JavaScript
        # only configures the owned test page; production perception remains visual.
        page.evaluate('clearInterval(window.captureFixtureTimer)')
        tab = int(platform.page_id.split(':')[-1])
        original = ui.evaluate('id => chrome.tabs.get(id)', tab)
        window = original['windowId']
        cover = ui.evaluate("url => chrome.windows.create({url, focused:true, width:900, height:700})",url+'/cover')
        cover_id = cover['id']
        inactive = None
        # Minimized windows are outside the supported acceptance scope.
        modes = ['foreground', 'background', 'covered', 'inactive'] if os.environ.get('SAYGO_TEST_HEADED') == '1' else ['inactive']
        bridge_before = json.loads((root/'bridge/status.json').read_text())
        try:
            for index, mode in enumerate(modes):
                with self.subTest(capture_mode=mode):
                    ui.evaluate('id => chrome.windows.update(id,{state:"normal"})',window)
                    ui.evaluate('id => chrome.tabs.update(id,{active:true})',tab)
                    if mode == 'foreground':
                        ui.evaluate('id => chrome.windows.update(id,{focused:true})',window)
                    elif mode == 'background':
                        ui.evaluate('id => chrome.windows.update(id,{left:0,top:0,width:700,height:650})',window)
                        ui.evaluate('id => chrome.windows.update(id,{state:"normal"})',cover_id)
                        ui.evaluate('id => chrome.windows.update(id,{left:800,top:0,width:700,height:650,focused:true})',cover_id)
                    elif mode == 'covered':
                        ui.evaluate('id => chrome.windows.update(id,{state:"maximized",focused:true})',cover_id)
                    else:
                        inactive=ui.evaluate('o => chrome.tabs.create(o)',{'windowId':window,'url':url+'/inactive','active':True})
                    page.wait_for_timeout(250)
                    before=platform.diagnose()
                    if mode=='inactive': self.assertFalse(before['tab_active'])
                    elif mode=='foreground': self.assertTrue(before['window_focused'])
                    else: self.assertFalse(before['window_focused'])
                    for color in [(31+index,70,130),(61+index,110,180)]:
                        page.evaluate('rgb => document.body.style.background=`rgb(${rgb.join(",")})`',color)
                        image=Image.open(io.BytesIO(platform.screenshot_raw())).convert('RGB')
                        self.assertEqual(image.getpixel((5,image.height-5)),color)
                    after=platform.diagnose()
                    for key in ('window_id','window_state','window_focused','tab_active'):
                        self.assertEqual(after[key],before[key],(mode,key))
                    self.assertTrue(after['connection_retained'])
                    self.assertFalse(after['last_capture']['pending'])
                    if inactive:
                        ui.evaluate('id => chrome.tabs.remove(id)',inactive['id']);inactive=None
            # Cross the worker's ordinary idle interval without screenshot requests.
            # This is a 35-second check, not a multi-hour endurance test.
            with self.subTest(capture_mode='inactive_short_idle'):
                ui.evaluate('id => chrome.windows.update(id,{state:"normal"})',window)
                inactive=ui.evaluate('o => chrome.tabs.create(o)',{'windowId':window,'url':url+'/inactive','active':True})
                before=platform.diagnose()
                self.assertFalse(before['tab_active'])
                self.assertEqual(before['window_state'],'normal')
                time.sleep(35)
                self.assertTrue(platform.diagnose()['connection_retained'])
                page.evaluate('document.body.style.background="rgb(12,34,56)"')
                image=Image.open(io.BytesIO(platform.screenshot_raw())).convert('RGB')
                self.assertEqual(image.getpixel((5,image.height-5)),(12,34,56))
                bridge_after=json.loads((root/'bridge/status.json').read_text())
                self.assertEqual(bridge_after['epoch'],bridge_before['epoch'])
                self.assertTrue(bridge_after['connected'])
                after=platform.diagnose()
                for key in ('window_id','window_state','window_focused','tab_active'):
                    self.assertEqual(after[key],before[key],key)
                self.assertFalse(after['last_capture']['pending'])
            if inactive:
                ui.evaluate('id => chrome.tabs.remove(id)',inactive['id']);inactive=None
            ui.evaluate('id => chrome.windows.update(id,{state:"normal"})',window)
            ui.evaluate('id => chrome.tabs.update(id,{active:true})',tab)
            platform.select_page(platform.page_id)
            page.goto(url+'/scroll')
            # Scroll offsets are a test oracle for this fixture only.
            offsets=lambda:page.evaluate('[left.scrollTop,right.scrollTop,window.scrollY]')
            def scroll(amount,x=150):
                platform.scroll_at(x,200,amount)
                platform.screenshot_raw()  # Observation between every action.
                return offsets()
            with self.subTest(scroll='direction_amplitude_and_pane'):
                self.assertEqual(offsets(),[0,0,0])
                down=scroll(-2)
                self.assertAlmostEqual(down[0],200,delta=3)
                self.assertEqual(down[1:],[0,0])
                fine=scroll(-.5)
                self.assertAlmostEqual(fine[0]-down[0],50,delta=3)
                back=scroll(.5)
                self.assertAlmostEqual(back[0],down[0],delta=3)
                right=scroll(-1,450)
                self.assertAlmostEqual(right[1],100,delta=3)
                self.assertAlmostEqual(right[0],back[0],delta=3)
            with self.subTest(scroll='boundaries'):
                bottom=scroll(-100)
                self.assertEqual(bottom[0],2600)
                self.assertEqual(scroll(-1),bottom)
                top=scroll(100)
                self.assertEqual(top[0],0)
                self.assertEqual(scroll(1),top)
                self.assertEqual(top[2],0)
            with self.subTest(capture='short_burst'):
                for _ in range(12):
                    self.assertTrue(platform.screenshot_raw().startswith(b'\x89PNG'))
                    self.assertFalse(platform.diagnose()['last_capture']['pending'])
                self.assertEqual(json.loads((root/'bridge/status.json').read_text())['epoch'],bridge_before['epoch'])
        finally:
            if inactive: ui.evaluate('id => chrome.tabs.remove(id)',inactive['id'])
            ui.evaluate('id => chrome.windows.remove(id)',cover_id)
            ui.evaluate('id => chrome.windows.update(id,{state:"normal"})',window)
