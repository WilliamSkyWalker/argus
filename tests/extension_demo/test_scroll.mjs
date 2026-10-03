import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {Clock} from './clock.mjs';

const source = await readFile(new URL('../../extensions/saygo-browser/background.js', import.meta.url), 'utf8');
function browser(handler = async () => {}) {
  const clock = new Clock();
  const events = [];
  const listener = {addListener() {}};
  const context = vm.createContext({
    setTimeout:clock.setTimeout,clearTimeout:clock.clearTimeout,Date:clock.Date,
    NetworkJournal: class {},
    chrome: {
      storage: {session: {get: async () => ({epoch:'test', blocked:[]})}},
      tabs: {query: async () => [{id:1,url:'https://example.test'}],
        onCreated:listener,onUpdated:listener,onRemoved:listener},
      debugger: {attach:async () => {},onDetach:listener,onEvent:listener,
        sendCommand: async (target, method, params) => {
          if (method === 'Page.getLayoutMetrics') return {cssVisualViewport:{clientWidth:1000,clientHeight:800}};
          events.push({target,method,params});
          return handler(method,params);
        }},
      runtime: {onMessage:listener}
    }
  });
  vm.runInContext(source.replace("import {NetworkJournal} from './network.js';", '') + '\nport = {}; negotiated = true;', context);
  return {events, clock, context, run:(args,operation='scroll_at') => context.runRequest(operation, {page_id:'test:1',...args})};
}

test('wheel targets left and right panes with both directions and fractional amounts', async () => {
  const b = browser();
  await b.run({x:250,y:400,amount:-3});
  await b.run({x:750,y:400,amount:1.5});
  assert.deepEqual(b.events.filter(e => e.method === 'Input.dispatchMouseEvent').map(e =>
    [e.target.tabId,e.params.type,e.params.x,e.params.y,e.params.deltaX,e.params.deltaY]), [
    [1,'mouseWheel',250,400,0,300],
    [1,'mouseWheel',750,400,0,-150]
  ]);
  assert.deepEqual(b.events.filter(e => e.method === 'Emulation.setFocusEmulationEnabled').map(e => e.params.enabled),
    [true,false,true,false]);
});

test('invalid amount and viewport coordinates never dispatch input', async () => {
  const b = browser();
  for (const args of [{x:-1},{x:1000},{y:800},{x:NaN},{amount:Infinity},{amount:101},{amount:'3'}]) {
    await assert.rejects(b.run({x:250,y:400,amount:-3,...args}));
  }
  assert.equal(b.events.length,0);
});


test('center scroll uses the same background wheel path', async () => {
  const b = browser();
  await b.run({direction:'up'},'scroll');
  await b.run({direction:'down'},'scroll');
  assert.deepEqual(b.events.filter(e => e.method === 'Input.dispatchMouseEvent').map(e =>
    [e.params.x,e.params.y,e.params.deltaY]), [[500,400,-300],[500,400,300]]);
});

test('wheel rejection restores logical focus without replay', async () => {
  const b = browser(async method => { if (method === 'Input.dispatchMouseEvent') throw Error('wheel failed'); });
  await assert.rejects(b.run({x:10,y:10,amount:-1}), /wheel failed/);
  assert.deepEqual(b.events.map(e => e.params.enabled ?? e.params.type),[true,'mouseWheel',false]);
});

test('timeout resets focus but retains the pending input lock until Chrome replies', async () => {
  let finish;
  const b = browser(async method => {
    if (method === 'Input.dispatchMouseEvent') await new Promise(resolve => { finish=resolve; });
  });
  const rejected=assert.rejects(b.run({x:10,y:10,amount:-1}), /outcome unknown/);
  await b.clock.advance(20000);
  await rejected;
  assert.deepEqual(b.events.map(e => e.params.enabled ?? e.params.type),[true,'mouseWheel',false]);
  await assert.rejects(b.run({x:10,y:10,amount:-1}), /still pending/);
  finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(vm.runInContext('inputPending',b.context),false);
  assert.equal(b.events.length,3);
  assert.equal(vm.runInContext('port !== null',b.context),true);
});

test('late focus preparation cannot dispatch expired input', async () => {
  let finish;
  const b = browser(async (method,params) => {
    if (method === 'Emulation.setFocusEmulationEnabled' && params.enabled)
      await new Promise(resolve => { finish=resolve; });
  });
  const rejected=assert.rejects(b.run({x:10,y:10,amount:-1}), /outcome unknown/);
  await b.clock.advance(20000);
  await rejected;
  finish();
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(b.events.map(e => e.params.enabled),[true,false]);
  assert.equal(vm.runInContext('inputPending',b.context),false);
});

for (const change of ['navigation','manual release']) {
  test(`${change} during focus preparation prevents wheel input`, async () => {
    let b;
    b = browser(async (method,params) => {
      if (method === 'Emulation.setFocusEmulationEnabled' && params.enabled)
        vm.runInContext(change === 'navigation' ? 'captureDocuments.delete(1)' : 'generation++; attached.clear()', b.context);
    });
    await assert.rejects(b.run({x:10,y:10,amount:-1}), /page\/control changed/);
    assert.equal(b.events.some(e => e.method === 'Input.dispatchMouseEvent'),false);
    assert.deepEqual(b.events.map(e => e.params.enabled),change === 'navigation' ? [true,false] : [true]);
  });
}

test('focus preparation error restores focus and never dispatches input', async () => {
  const b = browser(async (method,params) => {
    if (method === 'Emulation.setFocusEmulationEnabled' && params.enabled) throw Error('focus failed');
  });
  await assert.rejects(b.run({x:10,y:10,amount:-1}), /focus failed/);
  assert.deepEqual(b.events.map(e => e.params.enabled),[true,false]);
});
