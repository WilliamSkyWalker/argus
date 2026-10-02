import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';

const source = await readFile(new URL('../../extensions/saygo-browser/background.js', import.meta.url), 'utf8');
function browser({state='normal', active=false, command} = {}) {
  const calls = [];
  const listener = {addListener() {}};
  const tabs = [1,2].map(id => ({id,windowId:id,url:'https://example.test',active,status:'complete',discarded:false}));
  const context = vm.createContext({
    setTimeout:(fn,ms) => setTimeout(fn,ms === 200 ? 0 : 50), clearTimeout,
    NetworkJournal:class {},
    chrome:{
      storage:{session:{get:async () => ({epoch:'test',blocked:[]})}},
      tabs:{query:async () => tabs,get:async id => tabs.find(t => t.id === id),
        update:async () => {throw new Error('Capture must not activate a tab');},
        onCreated:listener,onUpdated:listener,onRemoved:listener},
      windows:{get:async () => ({state,focused:false}),
        update:async () => {throw new Error('Capture must not focus or restore a window');}},
      debugger:{attach:async () => {},onDetach:listener,onEvent:listener,
        sendCommand:async (target,method,params) => {
          calls.push({tab:target.tabId,method,params});
          const result = command?.(target,method,params);
          if (result !== undefined) return result;
          if (method === 'Page.getLayoutMetrics') return {cssVisualViewport:{clientWidth:800,clientHeight:600,pageX:0,pageY:30}};
          if (method === 'Page.captureScreenshot') return {data:'fresh-image'};
        }},
      runtime:{onMessage:listener}
    }
  });
  vm.runInContext(source.replace("import {NetworkJournal} from './network.js';", '')+'\nport = {}; negotiated = true;',context);
  return {calls,run:(operation='screenshot',id=1) => context.runRequest(operation,{page_id:`test:${id}`})};
}

for (const state of ['normal','minimized','maximized']) {
  test(`capture in ${state} window does not change focus, tab or screenshot method`,async () => {
    const b = browser({state});
    const shot = await b.run();
    assert.equal(shot.data,'fresh-image');
    assert.deepEqual(Array.from(shot.size),[800,600]);
    const captures = b.calls.filter(c => c.method === 'Page.captureScreenshot');
    assert.equal(captures.length,1);
    assert.equal(JSON.stringify(captures[0].params),JSON.stringify({format:'png',captureBeyondViewport:false,
      clip:{x:0,y:30,width:800,height:600,scale:1}}));
  });
}

test('pending capture does not accumulate requests or block diagnostics and other tabs',async () => {
  let finish;
  const b = browser({command:({tabId},method) => {
    if (tabId === 1 && method === 'Page.captureScreenshot') return new Promise(resolve => {finish=resolve;});
  }});
  await assert.rejects(b.run(),/timed out at Page.captureScreenshot/);
  await assert.rejects(b.run(),/Previous browser capture is still pending/);
  await assert.rejects(b.run('size'),/still pending/);
  const info = await b.run('diagnose');
  assert.equal(info.last_capture.pending,true);
  assert.equal(info.last_capture.timed_out,true);
  assert.equal(info.connection_retained,true);
  assert.equal(info.window_focused,false);
  assert.equal(await b.run('pages').then(p => p.length),2);
  assert.equal((await b.run('screenshot',2)).data,'fresh-image');
  finish({data:'late-image'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal((await b.run('diagnose')).last_capture.pending,false);
  assert.match((await b.run('diagnose')).last_capture.error,/late result discarded/);
  assert.equal(b.calls.filter(c => c.tab === 1 && c.method === 'Page.captureScreenshot').length,1);
  // Chrome completing the old call releases ownership, without reconnecting.
  const next=b.run();
  await new Promise(resolve => setTimeout(resolve,5));
  finish({data:'new-image'});
  assert.equal((await next).data,'new-image');
});

test('late layout response cannot initiate a screenshot after expiry',async () => {
  let finish;
  const b = browser({command:(_,method) => {
    if (method === 'Page.getLayoutMetrics') return new Promise(resolve => {finish=resolve;});
  }});
  await assert.rejects(b.run(),/timed out at Page.getLayoutMetrics/);
  finish({cssVisualViewport:{clientWidth:800,clientHeight:600,pageX:0,pageY:0}});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(b.calls.filter(c => c.method === 'Page.captureScreenshot').length,0);
  assert.equal((await b.run('diagnose')).last_capture.pending,false);
});

test('a late screenshot cannot start viewport checks or another capture',async () => {
  let finish;
  const b = browser({command:(_,method) => {
    if (method === 'Page.captureScreenshot') return new Promise(resolve => {finish=resolve;});
  }});
  await assert.rejects(b.run(),/timed out/);
  finish({data:'late-image'});
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(b.calls.map(c => c.method),['Page.getLayoutMetrics','Page.captureScreenshot']);
});

test('capture errors release ownership without changing the connection',async () => {
  let fail = true;
  const b = browser({command:(_,method) => {
    if (method === 'Page.captureScreenshot' && fail) return Promise.reject(new Error('capture failed'));
  }});
  await assert.rejects(b.run(),/capture failed/);
  fail = false;
  assert.equal((await b.run()).data,'fresh-image');
  assert.equal((await b.run('diagnose')).connection_retained,true);
});

test('size reads do not overwrite the last screenshot diagnostics',async () => {
  const b = browser();
  await b.run();
  await b.run('size');
  const info = await b.run('diagnose');
  assert.equal(info.last_capture.operation,'screenshot');
  assert.equal(info.last_capture.pending,false);
});
