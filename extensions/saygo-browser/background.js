// No DOM extraction or arbitrary page JavaScript execution.
// CDP provides visual input/screenshots and passive network observation.
import {NetworkJournal} from './network.js';
const network = new NetworkJournal((...args) => cdp(...args));
const networkPaused = new Set();
const networkStarting = new Map();
async function startNetwork(tabId, explicit = false) {
  if (explicit) networkPaused.delete(tabId);
  if (!port || !negotiated || networkPaused.has(tabId)) return;
  if (network.tabs.get(tabId)?.active) return;
  if (networkStarting.has(tabId)) return networkStarting.get(tabId);
  const ticket = generation;
  const job = (async () => {
    await permissions;
    const data = await state();
    if (data.blocked.includes(tabId)) return;
    await attach(tabId);
    if (!port || ticket !== generation || networkPaused.has(tabId)) return;
    network.begin(tabId);
    try {
      await cdp(tabId, 'Network.enable', {maxTotalBufferSize:4194304, maxResourceBufferSize:262144, maxPostDataSize:32768});
      if (!port || ticket !== generation) network.end(tabId, 'disconnected');
    } catch (error) { network.end(tabId, String(error.message || error)); throw error; }
  })();
  networkStarting.set(tabId,job);
  try { await job; } finally { if (networkStarting.get(tabId) === job) networkStarting.delete(tabId); }
}
async function autoNetwork(tabId) {
  try { await startNetwork(tabId); }
  catch (error) { network.begin(tabId); network.end(tabId, String(error.message || error)); }
}
let port = null;
const PROTOCOL = 1;
let negotiated = false;
let connectionError = "Disconnected";
let inputPending = false;
// A timeout does not cancel Chrome's screenshot or its temporary viewport state.
// Keep ownership until Chrome actually completes the outstanding operation.
const viewportPending = new Map();
const captureHistory = new Map();
const READ_OPERATIONS = new Set(["pages", "metadata", "diagnose", "size", "screenshot", "network_read"]);
const attached = new Set();
const attaching = new Map();
let generation = 0;
let permissions = Promise.resolve();
function updateBlocked(change) {
  permissions = permissions.then(async () => {
    const s = await state();
    await chrome.storage.session.set({blocked:change(s.blocked)});
  });
  return permissions;
}

async function state() {
  const data = await chrome.storage.session.get(["epoch", "blocked"]);
  if (!data.epoch) {
    data.epoch = crypto.randomUUID();
    data.blocked = [];
    await chrome.storage.session.set(data);
  }
  data.blocked ||= [];
  return data;
}
// Initialize before either UI or bridge requests touch shared state.
const ready = state();
function webURL(url) { return /^https?:\/\//i.test(url || ""); }
async function pages() {
  await permissions;
  const data = await state();
  if (!port || !negotiated) return [];
  const tabs = await chrome.tabs.query({});
  return tabs.filter(t => !data.blocked.includes(t.id) && webURL(t.pendingUrl || t.url)).map(t => ({
    page_id: `${data.epoch}:${t.id}`, url: t.pendingUrl || t.url, title: t.title || "",
    opener_id: t.openerTabId ? `${data.epoch}:${t.openerTabId}` : null
  }));
}
async function target(pageId) {
  const match = (await pages()).find(p => p.page_id === pageId);
  if (!match) throw new Error("Page closed, control released or browser restarted; reconnect/select a live tab");
  return Number(pageId.split(":").at(-1));
}
async function attach(tabId) {
  if (attached.has(tabId)) return;
  if (attaching.has(tabId)) return attaching.get(tabId);
  const ticket = generation;
  const job = (async () => {
    await chrome.debugger.attach({tabId}, "1.3");
    if (ticket !== generation) {
      await chrome.debugger.detach({tabId}).catch(() => {});
      throw new Error("Control was released");
    }
    attached.add(tabId);
  })();
  attaching.set(tabId,job);
  try { await job; } finally { if (attaching.get(tabId) === job) attaching.delete(tabId); }
}
async function cdp(tabId, method, params = {}) {
  return chrome.debugger.sendCommand({tabId}, method, params);
}
async function captureState(tabId) {
  const tab = await chrome.tabs.get(tabId);
  const window = await chrome.windows.get(tab.windowId);
  return {tab_active:tab.active, tab_discarded:tab.discarded, tab_status:tab.status,
    window_id:tab.windowId, window_state:window.state, window_focused:window.focused};
}
function captureStatus(trace) {
  if (!trace) return null;
  return {operation:trace.operation, stage:trace.stage, timed_out:trace.expired, pending:!trace.finished,
    elapsed_ms:(trace.finished || Date.now())-trace.started,
    stage_elapsed_ms:(trace.finished || Date.now())-trace.stageStarted,
    before:trace.before || null, error:trace.error || null};
}
async function detachAll() {
  network.reset(); networkPaused.clear();
  await Promise.all([...attached].map(id => chrome.debugger.detach({tabId:id}).catch(() => {})));
  attached.clear();
}
async function connect() {
  if (port) return;
  await updateBlocked(() => []);
  const current = chrome.runtime.connectNative("com.saygo.browser");
  port = current;
  negotiated = false;
  connectionError = "";
  let accept, reject;
  const handshake = new Promise((resolve, fail) => { accept = resolve; reject = fail; });
  const timer = setTimeout(() => reject(new Error('Bridge handshake timed out. Update the local host and reload this extension.')), 10000);
  current.onDisconnect.addListener(() => {
    const disconnectError = chrome.runtime.lastError?.message || "Disconnected";
    if (port === current) connectionError = disconnectError;
    if (port === current) { port = null; negotiated = false; generation++; void detachAll(); }
    reject(new Error(connectionError));
  });
  current.onMessage.addListener(request => {
    if (request.type === 'hello') {
      if (request.protocol !== PROTOCOL) {
        reject(new Error('Bridge protocol mismatch. Update the local host and extension together.'));
        return;
      }
      current.postMessage({type:'hello', protocol:PROTOCOL, version:chrome.runtime.getManifest().version});
      negotiated = true;
      accept();
      return;
    }
    if (!negotiated) { reject(new Error('Local host needs an update: no compatible handshake.')); return; }
    const ticket = generation;
    void (async () => {
      let response;
      try {
        await ready;
        if (port !== current || ticket !== generation) throw new Error("Control was released");
        if (Date.now() / 1000 >= request.deadline) throw new Error("Request expired before dispatch");
        response = {result: await runRequest(request.operation, request.arguments)};
      } catch (error) { response = {error: String(error.message || error)}; }
      const text = JSON.stringify(response);
      // Native host receives <= 1 MiB per frame, including Unicode JSON overhead.
      for (let i = 0; i < text.length; i += 100000) {
        if (port !== current) return;
        current.postMessage({id: request.id, chunk: text.slice(i, i + 100000), last: i + 100000 >= text.length});
      }
    })().catch(error => { connectionError = String(error); });
  });
  try { await handshake; }
  catch (error) {
    connectionError = String(error.message || error);
    // A failed handshake reports an error but never closes the user-owned port.
    throw error;
  } finally { clearTimeout(timer); }
  // Default capture covers every controllable existing tab, without a second prompt.
  await Promise.all((await pages()).map(p => autoNetwork(Number(p.page_id.split(':').at(-1)))));
}
// Input stays serialized while an underlying browser call is unresolved. Reads
// remain available, including after a timed-out input. Never replay old input.
async function runRequest(operation, args = {}) {
  const input = !READ_OPERATIONS.has(operation);
  const viewport = operation === "screenshot" || operation === "size";
  const pending = viewportPending.get(args.page_id);
  if (viewport && pending) {
    throw new Error(`Previous browser capture is still pending: ${JSON.stringify(captureStatus(pending))}; connection retained`);
  }
  if (input && inputPending) throw new Error("Previous browser input is still pending; observe before continuing");
  if (input) inputPending = true;
  const trace = {operation, stage: "target", expired:false, started:Date.now(), stageStarted:Date.now()};
  if (viewport) {
    viewportPending.set(args.page_id, trace);
    // Observation also asks for size; that must not erase screenshot evidence.
    if (operation === "screenshot") captureHistory.set(args.page_id, trace);
  }
  const work = execute(operation, args, trace).catch(error => {
    trace.error = String(error.message || error);
    throw error;
  }).finally(() => {
    trace.finished = Date.now();
    if (input) inputPending = false;
    if (viewportPending.get(args.page_id) === trace) viewportPending.delete(args.page_id);
  });
  const duration = operation === "long_press" && Number.isFinite(args.duration) && args.duration > 0 && args.duration <= 30 ? args.duration : 0;
  let timer;
  try {
    return await Promise.race([work, new Promise((_, reject) => {
      timer = setTimeout(() => {
        trace.expired = true;
        const detail = viewport ? `; capture=${JSON.stringify(captureStatus(trace))}` : "";
        reject(new Error(`Browser request timed out at ${trace.stage}; outcome unknown; connection retained${detail}`));
      }, (20 + duration) * 1000);
    })]);
  } finally { clearTimeout(timer); }
}
async function execute(operation, args = {}, trace = {}) {
  const command = (tabId, method, params) => {
    trace.stage = method;
    trace.stageStarted = Date.now();
    return cdp(tabId, method, params);
  };
  if (operation === "pages") return pages();
  if (operation === "new_page") {
    if (!port) throw new Error("Browser is disconnected");
    if (!webURL(args.url)) throw new Error("Only HTTP(S) navigation is allowed");
    const tab = await chrome.tabs.create({url:args.url, active:false});
    const data = await state();
    return {page_id:`${data.epoch}:${tab.id}`};
  }
  const tabId = await target(args.page_id);
  if (operation === "diagnose") {
    return {capture_mode:"cdp_surface", ...await captureState(tabId),
      last_capture:captureStatus(captureHistory.get(args.page_id)),
      connection_retained:!!port && negotiated, debugger_attached:attached.has(tabId)};
  }
  if (operation.startsWith('network_')) {
    if (operation === 'network_start') await startNetwork(tabId, true);
    else if (operation === 'network_stop') {
      networkPaused.add(tabId);
      await networkStarting.get(tabId);
      network.end(tabId);
      if (attached.has(tabId)) await command(tabId,'Network.disable');
    } else if (operation === 'network_clear') network.clear(tabId);
    else if (operation !== 'network_read') throw new Error('Unsupported network operation');
    return network.read(tabId, operation === 'network_read' ? args : {limit:1});
  }
  if (operation === "select") {
    const tab = await chrome.tabs.update(tabId, {active:true});
    await chrome.windows.update(tab.windowId, {focused:true});
    return {};
  }
  if (operation === "close") {
    if ((await chrome.tabs.query({})).length <= 1) throw new Error("Close the last browser tab manually");
    await chrome.tabs.remove(tabId); return {};
  }
  if (operation === "metadata") {
    return (await pages()).find(p => p.page_id === args.page_id);
  }
  const allowed = ["screenshot", "size", "tap", "hover", "double_click", "right_click", "long_press", "input", "key", "swipe", "scroll", "scroll_at", "navigate", "back", "forward"];
  if (!allowed.includes(operation)) throw new Error("Unsupported operation");
  trace.stage = "attach";
  await attach(tabId);
  // Control can be released while attach is pending.
  await target(args.page_id);
  if (operation === "size" || operation === "screenshot") {
    const checkCapture = () => {
      if (trace.expired) throw new Error("Capture request expired; late result discarded; connection retained");
    };
    checkCapture();
    trace.stage = "capture state";
    trace.stageStarted = Date.now();
    trace.before = await captureState(tabId);
    checkCapture();
    // Showing Chrome's debugger banner can resize its viewport. Observation must
    // not activate tabs, focus windows or restore a minimized window.
    await new Promise(resolve => setTimeout(resolve, 200));
    for (let attempt = 0; attempt < 3; attempt++) {
      checkCapture();
      const metrics = await command(tabId, "Page.getLayoutMetrics");
      checkCapture();
      const view = metrics.cssVisualViewport;
      const size = [Math.round(view.clientWidth), Math.round(view.clientHeight)];
      if (operation === "size") return size;
      const shot = await command(tabId, "Page.captureScreenshot", {
        format:"png", captureBeyondViewport:false,
        clip:{x:view.pageX, y:view.pageY, width:view.clientWidth, height:view.clientHeight, scale:1}
      });
      checkCapture();
      const after = (await command(tabId, "Page.getLayoutMetrics")).cssVisualViewport;
      checkCapture();
      if (after.clientWidth === view.clientWidth && after.clientHeight === view.clientHeight &&
          after.pageX === view.pageX && after.pageY === view.pageY) return {data:shot.data, size};
    }
    throw new Error("Viewport changed during screenshot; observe again");
  }

  const point = (x,y) => {
    if (![x,y].every(Number.isFinite) || x < 0 || y < 0) throw new Error("Invalid coordinates");
    return {x,y};
  };
  const inputPoint = async (x,y) => {
    const p = point(x,y);
    const {cssVisualViewport:v} = await command(tabId,"Page.getLayoutMetrics");
    if (p.x >= v.clientWidth || p.y >= v.clientHeight) throw new Error("Input coordinates outside viewport");
    return p;
  };
  if (operation === "tap") {
    const p = await inputPoint(args.x,args.y);
    await command(tabId,"Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,...p});
    await command(tabId,"Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,...p});
  } else if (operation === "hover") {
    const p = await inputPoint(args.x,args.y);
    await command(tabId,"Input.dispatchMouseEvent",{type:"mouseMoved",...p});
  } else if (operation === "double_click" || operation === "right_click") {
    const p = await inputPoint(args.x,args.y);
    const button = operation === "right_click" ? "right" : "left";
    const clickCount = operation === "double_click" ? 2 : 1;
    await command(tabId,"Input.dispatchMouseEvent",{type:"mousePressed",button,clickCount,...p});
    await command(tabId,"Input.dispatchMouseEvent",{type:"mouseReleased",button,clickCount,...p});
  } else if (operation === "long_press") {
    const p = await inputPoint(args.x,args.y);
    if (!Number.isFinite(args.duration) || args.duration < .1 || args.duration > 30) throw new Error("Invalid long-press duration");
    await command(tabId,"Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,...p});
    try {
      await new Promise(resolve => setTimeout(resolve,args.duration*1000));
    } finally {
      await command(tabId,"Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,...p});
    }
  } else if (operation === "input") {
    if (typeof args.text !== "string") throw new Error("Invalid text");
    await command(tabId,"Input.insertText",{text:args.text});
  } else if (operation === "key") {
    const keys = {enter:["Enter",13],tab:["Tab",9],escape:["Escape",27],space:[" ",32],
      delete:["Backspace",8],backspace:["Backspace",8],arrow_up:["ArrowUp",38],arrow_down:["ArrowDown",40]};
    const select = args.key === "select_all";
    const key = select ? ["a",65] : keys[args.key.toLowerCase()];
    if (!key) throw new Error("Unsupported key");
    const modifiers = select ? (/Mac/.test(navigator.platform) ? 4 : 2) : 0;
    await command(tabId,"Input.dispatchKeyEvent",{type:"rawKeyDown",key:key[0],windowsVirtualKeyCode:key[1],modifiers});
    await command(tabId,"Input.dispatchKeyEvent",{type:"keyUp",key:key[0],windowsVirtualKeyCode:key[1],modifiers});
  } else if (operation === "scroll") {
    const metrics = await command(tabId,"Page.getLayoutMetrics");
    const v = metrics.cssVisualViewport;
    await command(tabId,"Input.synthesizeScrollGesture",{x:v.clientWidth/2,y:v.clientHeight/2,
      xDistance:0,yDistance:args.direction === "up" ? 300 : -300,
      gestureSourceType:"mouse",preventFling:true,speed:1000});
  } else if (operation === "scroll_at") {
    const p = point(args.x,args.y);
    if (!Number.isFinite(args.amount) || Math.abs(args.amount) > 100) throw new Error("Invalid scroll amount");
    const {cssVisualViewport:v} = await command(tabId,"Page.getLayoutMetrics");
    if (p.x >= v.clientWidth || p.y >= v.clientHeight) throw new Error("Scroll coordinates outside viewport");
    // Use the complete scroll gesture, whose reply marks gesture completion.
    // A raw mouseWheel can scroll while its CDP acknowledgement stays pending.
    await command(tabId,"Input.synthesizeScrollGesture",{...p,xDistance:0,yDistance:args.amount*100,
      gestureSourceType:"mouse",preventFling:true,speed:1000});
  } else if (operation === "swipe") {
    const a = point(args.x1,args.y1), b = point(args.x2,args.y2);
    await command(tabId,"Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,...a});
    try {
      for(let i=1;i<=10;i++) await command(tabId,"Input.dispatchMouseEvent",{type:"mouseMoved",button:"left",buttons:1,
        x:a.x+(b.x-a.x)*i/10,y:a.y+(b.y-a.y)*i/10});
    } finally { await command(tabId,"Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,...b}); }
  } else if (operation === "navigate") {
    if (!webURL(args.url)) throw new Error("Only HTTP(S) navigation is allowed");
    await chrome.tabs.update(tabId,{url:args.url});
  } else if (operation === "back") await chrome.tabs.goBack(tabId);
  else if (operation === "forward") await chrome.tabs.goForward(tabId);
  return {};
}
chrome.debugger.onDetach.addListener(({tabId}, reason) => {
  attached.delete(tabId);
  network.end(tabId, reason);
  // Chrome's "Cancel" control must not be undone by automatic reattachment.
  if (reason === "canceled_by_user") void updateBlocked(blocked => [...new Set([...blocked, tabId])]);
});
chrome.debugger.onEvent.addListener((source, method, params) => {
  if (!source.sessionId) void network.event(source.tabId, method, params).catch(error => {
    network.end(source.tabId, String(error.message || error));
  });
});
chrome.tabs.onCreated.addListener(tab => {
  if (port && webURL(tab.pendingUrl || tab.url)) void autoNetwork(tab.id);
});
chrome.tabs.onUpdated.addListener((tabId, change, tab) => {
  if (port && webURL(tab.pendingUrl || tab.url) && (change.url || change.status)) void autoNetwork(tabId);
});
chrome.tabs.onRemoved.addListener(tabId => {
  for (const key of captureHistory.keys()) {
    if (Number(key.split(':').at(-1)) === tabId) captureHistory.delete(key);
  }
  network.tabs.delete(tabId); networkPaused.delete(tabId);
  attached.delete(tabId);
  void updateBlocked(blocked => blocked.filter(id => id !== tabId));
});
chrome.runtime.onMessage.addListener((message, sender, reply) => {
  if (sender.id !== chrome.runtime.id) return false;
  (async () => {
    await ready;
    if (message.type === "connect") await connect();
    else if (message.type === "release") {
      generation++;
      const old = port; port = null; negotiated = false;
      await updateBlocked(() => []);
      old?.disconnect();
      await detachAll();
      connectionError = "Control released";
    }
    return {connected:!!port && negotiated,error:connectionError,pages:await pages(),protocol:PROTOCOL,version:chrome.runtime.getManifest().version};
  })().then(reply, error => reply({error:String(error.message || error)}));
  return true;
});
