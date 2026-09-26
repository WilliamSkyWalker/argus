// No DOM extraction or arbitrary JavaScript execution. CDP is used only for
// screenshots, viewport metrics and input. Access covers HTTP(S) tabs in the connected browser profile.
let port = null;
let connectionError = "Disconnected";
let chain = Promise.resolve();
const attached = new Set();
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
  if (!port) return [];
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
  if (!attached.has(tabId)) {
    const ticket = generation;
    await chrome.debugger.attach({tabId}, "1.3");
    if (ticket !== generation) {
      await chrome.debugger.detach({tabId}).catch(() => {});
      throw new Error("Control was released");
    }
    attached.add(tabId);
  }
}
async function cdp(tabId, method, params = {}) {
  return chrome.debugger.sendCommand({tabId}, method, params);
}
async function detachAll() {
  await Promise.all([...attached].map(id => chrome.debugger.detach({tabId:id}).catch(() => {})));
  attached.clear();
}
async function connect() {
  if (port) return;
  await updateBlocked(() => []);
  const current = chrome.runtime.connectNative("com.argus.browser");
  port = current;
  connectionError = "";
  current.onDisconnect.addListener(() => {
    connectionError = chrome.runtime.lastError?.message || "Disconnected";
    if (port === current) { port = null; generation++; void detachAll(); }
  });
  current.onMessage.addListener(request => {
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
  // Chrome's "Cancel" control must not be undone by automatic reattachment.
  if (reason === "canceled_by_user") void updateBlocked(blocked => [...new Set([...blocked, tabId])]);
});
chrome.tabs.onRemoved.addListener(tabId => {
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
      const old = port; port = null;
      await updateBlocked(() => []);
      old?.disconnect();
      await detachAll();
      connectionError = "Control released";
    }
    return {connected:!!port,error:connectionError,pages:await pages()};
  })().then(reply, error => reply({error:String(error.message || error)}));
  return true;
});
