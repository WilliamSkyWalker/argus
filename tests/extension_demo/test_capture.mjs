import test from 'node:test';
import assert from 'node:assert/strict';
import {readFile} from 'node:fs/promises';
import vm from 'node:vm';
import {Clock} from './clock.mjs';

const source = await readFile(new URL('../../extensions/saygo-browser/background.js', import.meta.url), 'utf8');
function browser({state='normal', active=false, command} = {}) {
  const calls = [];
  const clock = new Clock();
  const events = {};
  const listener = {addListener() {}};
  const tabs = [1,2].map(id => ({id,windowId:id,url:'https://example.test',active,status:'complete',discarded:false}));
  const context = vm.createContext({
    setTimeout:clock.setTimeout, clearTimeout:clock.clearTimeout, Date:clock.Date,
    NetworkJournal:class { tabs=new Map(); end() {} reset() {} },
    chrome:{
      storage:{session:{get:async () => ({epoch:'test',blocked:[]}),set:async () => {}}},
      tabs:{query:async () => tabs,get:async id => tabs.find(t => t.id === id),
        update:async () => {throw new Error('Capture must not activate a tab');},
        onCreated:listener,onUpdated:{addListener(fn) {events.updated=fn;}},onRemoved:{addListener(fn) {events.removed=fn;}}},
      windows:{get:async () => ({state,focused:false}),
        update:async () => {throw new Error('Capture must not focus or restore a window');}},
      debugger:{attach:async () => {},detach:async () => {},onDetach:listener,onEvent:listener,
        sendCommand:async (target,method,params) => {
          calls.push({tab:target.tabId,method,params});
          const result = command?.(target,method,params);
          if (result !== undefined) return result;
          if (method === 'Page.getLayoutMetrics') return {cssVisualViewport:{clientWidth:800,clientHeight:600,pageX:0,pageY:30}};
          if (method === 'Page.captureScreenshot') return {data:'fresh-image'};
        }},
      runtime:{id:"test-extension",onMessage:{addListener(fn) {events.message=fn;}}}
    }
  });
  vm.runInContext(source.replace("import {NetworkJournal} from './network.js';", '')+'\nport = {disconnect() {}}; negotiated = true; networkPaused.add(1); networkPaused.add(2);',context);
  const start = (operation='screenshot',id=1) => {
    const result=context.runRequest(operation,{page_id:`test:${id}`});
    result.catch(() => {});
    return result;
  };
  return {calls,clock,tabs,events,context,start,
    async run(operation='screenshot',id=1) {
      const result=start(operation,id);
      await clock.advance(200);
      return result;
    },
    async timeout(pattern) {
      const result=start();
      const rejection=assert.rejects(result,pattern);
      await clock.advance(20000);
      await rejection;
    }
  };
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
  await b.timeout(/timed out at Page.captureScreenshot/);
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
  const next=b.start();
  await b.clock.advance(200);
  finish({data:'new-image'});
  assert.equal((await next).data,'new-image');
});

test('late layout response cannot initiate a screenshot after expiry',async () => {
  let finish;
  const b = browser({command:(_,method) => {
    if (method === 'Page.getLayoutMetrics') return new Promise(resolve => {finish=resolve;});
  }});
  await b.timeout(/timed out at Page.getLayoutMetrics/);
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
  await b.timeout(/timed out/);
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

for (const event of ['navigation','reload','closed','released']) {
  test(`capture rejects an image after ${event} while Chrome is pending`,async () => {
    let finish;
    const b=browser({command:(_,method) => method === 'Page.captureScreenshot' ?
      new Promise(resolve => {finish=resolve;}) : undefined});
    const result=b.start();
    await b.clock.advance(200);
    const rejection=assert.rejects(result,/changed|closed|released|restarted/i);
    if(event==='navigation') {
      b.tabs[0].url='https://example.test/next';
      b.events.updated(1,{url:b.tabs[0].url},b.tabs[0]);
    } else if(event==='reload') b.events.updated(1,{status:'loading'},b.tabs[0]);
    else if(event==='closed') { b.tabs.shift(); b.events.removed(1); }
    else await new Promise(resolve => b.events.message({type:'release'},{id:'test-extension'},resolve));
    finish({data:'wrong-document'});
    await rejection;
  });
}

test('two clients cannot capture the same page concurrently',async () => {
  let finish;
  const b=browser({command:(_,method) => method === 'Page.captureScreenshot' ?
    new Promise(resolve => {finish=resolve;}) : undefined});
  const first=b.start();
  await b.clock.advance(200);
  await assert.rejects(b.start(),/still pending/);
  assert.equal(b.calls.filter(c => c.method==='Page.captureScreenshot').length,1);
  finish({data:'first-client'});
  assert.equal((await first).data,'first-client');
});

test('viewport changes discard the first frame and use a matching second frame',async () => {
  let metrics=0, shots=0;
  const b=browser({command:(_,method) => {
    if(method==='Page.getLayoutMetrics') return {cssVisualViewport:{clientWidth:++metrics===1?800:900,clientHeight:600,pageX:0,pageY:0}};
    if(method==='Page.captureScreenshot') return {data:`frame-${++shots}`};
  }});
  const shot=await b.run();
  assert.equal(shot.data,'frame-2');
  assert.deepEqual(Array.from(shot.size),[900,600]);
});

test('continuous viewport changes stop after three attempts',async () => {
  let width=800;
  const b=browser({command:(_,method) => method==='Page.getLayoutMetrics' ?
    {cssVisualViewport:{clientWidth:width++,clientHeight:600,pageX:0,pageY:0}} : undefined});
  await assert.rejects(b.run(),/Viewport changed/);
  assert.equal(b.calls.filter(c => c.method==='Page.captureScreenshot').length,3);
  assert.equal((await b.run('diagnose')).connection_retained,true);
});

test('timeout during final metrics cannot return or retry the captured image',async () => {
  let metrics=0,finish;
  const b=browser({command:(_,method) => {
    if(method==='Page.getLayoutMetrics' && ++metrics===2) return new Promise(resolve => {finish=resolve;});
  }});
  await b.timeout(/timed out/);
  finish({cssVisualViewport:{clientWidth:900,clientHeight:600,pageX:0,pageY:0}});
  await b.clock.flush();
  assert.equal(b.calls.filter(c => c.method==='Page.captureScreenshot').length,1);
  assert.match((await b.run('diagnose')).last_capture.error,/expired/);
});

test('closing short-lived capture tabs leaves no retained capture state',async () => {
  const b=browser();
  for(let id=3;id<23;id++) {
    b.tabs.push({id,windowId:id,url:'https://example.test',active:false,status:'complete'});
    await b.run('screenshot',id);
    b.tabs.splice(b.tabs.findIndex(t => t.id===id),1);
    b.events.removed(id);
  }
  assert.equal(vm.runInContext('captureHistory.size',b.context),0);
  assert.equal(vm.runInContext('captureDocuments.size',b.context),0);
  assert.equal(vm.runInContext('viewportPending.size',b.context),0);
  assert.equal(b.clock.timers.size,0);
});
