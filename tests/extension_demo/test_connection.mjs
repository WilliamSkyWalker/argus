import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../extensions/saygo-browser/background.js', import.meta.url), 'utf8');
function browser(tabs = [{id:1,url:'https://example.test'}]) {
  let disconnected = 0;
  const removed = [];
  const listeners = {};
  const listener = {addListener() {}};
  const port = {
    onDisconnect:listener,
    onMessage:{addListener(fn) { listeners.message = fn; }},
    postMessage() {}, disconnect() { disconnected++; }
  };
  const context = vm.createContext({
    setTimeout:(fn, ms) => setTimeout(fn, Math.min(ms, 30)), clearTimeout,
    NetworkJournal:class {},
    chrome:{
      storage:{session:{get:async () => ({epoch:'test',blocked:[]}),set:async () => {}}},
      tabs:{query:async () => tabs, remove:async id => removed.push(id),
        onCreated:listener,onUpdated:listener,onRemoved:listener},
      debugger:{onDetach:listener,onEvent:listener},
      runtime:{connectNative:() => port,onMessage:listener}
    }
  });
  vm.runInContext(source.replace("import {NetworkJournal} from './network.js';", ''), context);
  return {context, removed, listeners, disconnected:() => disconnected,
    connected:() => vm.runInContext('port !== null', context),
    activate:() => vm.runInContext('port = {}; negotiated = true;', context)};
}

test('handshake timeout keeps the native port open', async () => {
  const b = browser();
  await assert.rejects(b.context.connect(), /handshake timed out/);
  assert.equal(b.disconnected(), 0);
  assert.equal(b.connected(), true);
});

test('protocol mismatch keeps the native port open', async () => {
  const b = browser();
  const pending = b.context.connect();
  await new Promise(resolve => setImmediate(resolve));
  b.listeners.message({type:'hello',protocol:99});
  await assert.rejects(pending, /protocol mismatch/);
  assert.equal(b.disconnected(), 0);
  assert.equal(b.connected(), true);
});

test('timed-out input allows reads, rejects overlapping input, and is not replayed', async () => {
  const b = browser();
  let finish;
  const calls = [];
  b.context.execute = async operation => {
    calls.push(operation);
    if (operation === 'scroll_at') await new Promise(resolve => { finish = resolve; });
    return operation;
  };
  await assert.rejects(b.context.runRequest('scroll_at'), /outcome unknown/);
  assert.equal(await b.context.runRequest('screenshot'), 'screenshot');
  assert.equal(await b.context.runRequest('pages'), 'pages');
  await assert.rejects(b.context.runRequest('tap'), /still pending/);
  finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(await b.context.runRequest('tap'), 'tap');
  assert.deepEqual(calls, ['scroll_at','screenshot','pages','tap']);
  assert.equal(b.disconnected(), 0);
});

test('closing the last tab is rejected, other tabs can be closed', async () => {
  const b = browser(); b.activate();
  await assert.rejects(b.context.execute('close', {page_id:'test:1'}), /last browser tab manually/);
  assert.deepEqual(b.removed, []);
  const multi = browser([{id:1,url:'https://example.test'}, {id:2,url:'https://example.test/other'}]);
  multi.activate();
  await multi.context.execute('close', {page_id:'test:1'});
  assert.deepEqual(multi.removed, [1]);
});
