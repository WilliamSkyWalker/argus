import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../extensions/saygo-browser/background.js', import.meta.url), 'utf8');
function browser() {
  const events = [];
  const listener = {addListener() {}};
  const context = vm.createContext({
    NetworkJournal: class {},
    chrome: {
      storage: {session: {get: async () => ({epoch:'test', blocked:[]})}},
      tabs: {query: async () => [{id:1,url:'https://example.test'}],
        onCreated:listener,onUpdated:listener,onRemoved:listener},
      debugger: {attach:async () => {},onDetach:listener,onEvent:listener,
        sendCommand: async (target, method, params) => {
          if (method === 'Page.getLayoutMetrics') return {cssVisualViewport:{clientWidth:1000,clientHeight:800}};
          events.push({target,method,params});
        }},
      runtime: {onMessage:listener}
    }
  });
  vm.runInContext(source.replace("import {NetworkJournal} from './network.js';", '') + '\nport = {}; negotiated = true;', context);
  return {events, run:args => context.execute('scroll_at', {page_id:'test:1',...args})};
}

test('wheel targets left and right panes with both directions and fractional amounts', async () => {
  const b = browser();
  await b.run({x:250,y:400,amount:-3});
  await b.run({x:750,y:400,amount:1.5});
  assert.deepEqual(b.events.map(e => [e.target.tabId,e.method,e.params.gestureSourceType,e.params.x,e.params.y,e.params.xDistance,e.params.yDistance]), [
    [1,'Input.synthesizeScrollGesture','mouse',250,400,0,-300],
    [1,'Input.synthesizeScrollGesture','mouse',750,400,0,150]
  ]);
});

test('invalid amount and viewport coordinates never dispatch input', async () => {
  const b = browser();
  for (const args of [{x:-1},{x:1000},{y:800},{x:NaN},{amount:Infinity},{amount:101},{amount:'3'}]) {
    await assert.rejects(b.run({x:250,y:400,amount:-3,...args}));
  }
  assert.equal(b.events.length,0);
});
