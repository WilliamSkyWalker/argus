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
let chain = Promise.resolve();
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
    chain = chain.then(async () => {
      let response;
      try {
        await ready;
        if (port !== current || ticket !== generation) throw new Error("Control was released");
        if (Date.now() / 1000 >= request.deadline) throw new Error("Request expired before dispatch");
        response = {result: await execute(request.operation, request.arguments)};
      } catch (error) { response = {error: String(error.message || error)}; }
      const text = JSON.stringify(response);
      // Native host receives <= 1 MiB per frame, including Unicode JSON overhead.
      for (let i = 0; i < text.length; i += 100000) {
        if (port !== current) return;
        current.postMessage({id: request.id, chunk: text.slice(i, i + 100000), last: i + 100000 >= text.length});
      }
    }).catch(error => { connectionError = String(error); });
  });
  try { await handshake; }
  catch (error) {
    connectionError = String(error.message || error);
    if (port === current) { port = null; negotiated = false; generation++; }
    current.disconnect();
    await detachAll();
    throw error;
  } finally { clearTimeout(timer); }
  // Default capture covers every controllable existing tab, without a second prompt.
  await Promise.all((await pages()).map(p => autoNetwork(Number(p.page_id.split(':').at(-1)))));
}
async function execute(operation, args = {}) {
  if (operation === "pages") return pages();
  if (operation === "new_page") {
    if (!port) throw new Error("Browser is disconnected");
    if (!webURL(args.url)) throw new Error("Only HTTP(S) navigation is allowed");
    const tab = await chrome.tabs.create({url:args.url, active:false});
    const data = await state();
    return {page_id:`${data.epoch}:${tab.id}`};
  }
  const tabId = await target(args.page_id);
  if (operation.startsWith('network_')) {
    if (operation === 'network_start') await startNetwork(tabId, true);
    else if (operation === 'network_stop') {
      networkPaused.add(tabId);
      await networkStarting.get(tabId);
      network.end(tabId);
      if (attached.has(tabId)) await cdp(tabId,'Network.disable');
    } else if (operation === 'network_clear') network.clear(tabId);
    else if (operation !== 'network_read') throw new Error('Unsupported network operation');
    return network.read(tabId, operation === 'network_read' ? args : {limit:1});
  }
  if (operation === "select") {
    const tab = await chrome.tabs.update(tabId, {active:true});
    await chrome.windows.update(tab.windowId, {focused:true});
    return {};
  }
  if (operation === "close") { await chrome.tabs.remove(tabId); return {}; }
  if (operation === "metadata") {
    return (await pages()).find(p => p.page_id === args.page_id);
  }
  const allowed = ["screenshot", "size", "tap", "input", "key", "swipe", "scroll", "navigate", "back", "forward"];
  if (!allowed.includes(operation)) throw new Error("Unsupported operation");
  await attach(tabId);
  // Control can be released while attach is pending.
  await target(args.page_id);
  if (operation === "size" || operation === "screenshot") {
    await chrome.tabs.update(tabId, {active:true});
    // Activating a tab and showing Chrome's debugger banner can resize its viewport.
    await new Promise(resolve => setTimeout(resolve, 200));
    for (let attempt = 0; attempt < 3; attempt++) {
      const metrics = await cdp(tabId, "Page.getLayoutMetrics");
      const view = metrics.cssVisualViewport;
      const size = [Math.round(view.clientWidth), Math.round(view.clientHeight)];
      if (operation === "size") return size;
      const shot = await cdp(tabId, "Page.captureScreenshot", {
        format:"png", captureBeyondViewport:false,
        clip:{x:view.pageX, y:view.pageY, width:view.clientWidth, height:view.clientHeight, scale:1}
      });
      const after = (await cdp(tabId, "Page.getLayoutMetrics")).cssVisualViewport;
      if (after.clientWidth === view.clientWidth && after.clientHeight === view.clientHeight &&
          after.pageX === view.pageX && after.pageY === view.pageY) return {data:shot.data, size};
    }
    throw new Error("Viewport changed during screenshot; observe again");
  }

  const point = (x,y) => {
    if (![x,y].every(Number.isFinite) || x < 0 || y < 0) throw new Error("Invalid coordinates");
    return {x,y};
  };
  if (operation === "tap") {
    const p = point(args.x,args.y);
    await cdp(tabId,"Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,...p});
    await cdp(tabId,"Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,...p});
  } else if (operation === "input") {
    if (typeof args.text !== "string") throw new Error("Invalid text");
    await cdp(tabId,"Input.insertText",{text:args.text});
  } else if (operation === "key") {
    const keys = {enter:["Enter",13],tab:["Tab",9],escape:["Escape",27],space:[" ",32],
      delete:["Backspace",8],backspace:["Backspace",8],arrow_up:["ArrowUp",38],arrow_down:["ArrowDown",40]};
    const select = args.key === "select_all";
    const key = select ? ["a",65] : keys[args.key.toLowerCase()];
    if (!key) throw new Error("Unsupported key");
    const modifiers = select ? (/Mac/.test(navigator.platform) ? 4 : 2) : 0;
    await cdp(tabId,"Input.dispatchKeyEvent",{type:"rawKeyDown",key:key[0],windowsVirtualKeyCode:key[1],modifiers});
    await cdp(tabId,"Input.dispatchKeyEvent",{type:"keyUp",key:key[0],windowsVirtualKeyCode:key[1],modifiers});
  } else if (operation === "scroll") {
    const metrics = await cdp(tabId,"Page.getLayoutMetrics");
    const v = metrics.cssVisualViewport;
    await cdp(tabId,"Input.dispatchMouseEvent",{type:"mouseWheel",x:v.clientWidth/2,y:v.clientHeight/2,
      deltaX:0,deltaY:args.direction === "up" ? -300 : 300});
  } else if (operation === "swipe") {
    const a = point(args.x1,args.y1), b = point(args.x2,args.y2);
    await cdp(tabId,"Input.dispatchMouseEvent",{type:"mousePressed",button:"left",clickCount:1,...a});
    try {
      for(let i=1;i<=10;i++) await cdp(tabId,"Input.dispatchMouseEvent",{type:"mouseMoved",button:"left",buttons:1,
        x:a.x+(b.x-a.x)*i/10,y:a.y+(b.y-a.y)*i/10});
    } finally { await cdp(tabId,"Input.dispatchMouseEvent",{type:"mouseReleased",button:"left",clickCount:1,...b}); }
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
