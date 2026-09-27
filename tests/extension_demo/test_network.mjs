import test from 'node:test';
import assert from 'node:assert/strict';
import {webcrypto} from 'node:crypto';
import {readFile} from 'node:fs/promises';
globalThis.crypto ||= webcrypto;
// Import the browser ES module without imposing package.json on the repository.
const source = await readFile(new URL('../../extensions/saygo-browser/network.js',import.meta.url),'utf8');
const {NetworkJournal} = await import('data:text/javascript;base64,'+Buffer.from(source).toString('base64'));
const request = (id='1',type='Fetch') => ({requestId:id,timestamp:1,type,request:{url:'https://example.test/api',method:'POST',headers:{a:'b'},postData:'hello'}});

test('HTTP bodies, redirect, failures, cursors and tab isolation', async () => {
  const j = new NetworkJournal(async () => ({body:'{"ok":true}',base64Encoded:false}));
  j.begin(1); j.begin(2);
  await j.event(1,'Network.requestWillBeSent',request());
  await j.event(1,'Network.requestWillBeSent',{...request(),redirectResponse:{status:302,url:'https://example.test/old'}});
  await j.event(1,'Network.responseReceived',{requestId:'1',type:'Fetch',response:{status:200,mimeType:'application/json'}});
  await j.event(1,'Network.loadingFinished',{requestId:'1',timestamp:2,encodedDataLength:12});
  const rows=j.read(1).events;
  assert.equal(rows.at(-1).body,'{"ok":true}');
  assert.equal(rows.at(-2).duration_ms,1000);
  assert.equal(rows[1].kind,'http.redirect');
  assert.equal(j.read(2).events.length,0);
  assert.equal(j.read(1,{after:rows.at(-1).seq}).events.length,0);
  assert.equal(j.read(1,{url:'absent'}).next_cursor,rows.at(-1).seq);
  await j.event(1,'Network.requestWillBeSent',request('2'));
  await j.event(1,'Network.loadingFailed',{requestId:'2',timestamp:2,errorText:'failed'});
  assert.equal(j.read(1).events.at(-1).kind,'http.failed');
});

test('WebSocket text/binary frames, SSE messages and fetch streaming', async () => {
  const j=new NetworkJournal(async () => ({bufferedData:'YWJj'})); j.begin(1);
  await j.event(1,'Network.webSocketCreated',{requestId:'w',url:'wss://example.test/ws'});
  await j.event(1,'Network.webSocketFrameSent',{requestId:'w',response:{opcode:1,payloadData:'hello'}});
  await j.event(1,'Network.webSocketFrameReceived',{requestId:'w',response:{opcode:2,payloadData:'AQI='}});
  await j.event(1,'Network.webSocketClosed',{requestId:'w'});
  await j.event(1,'Network.requestWillBeSent',request('s','EventSource'));
  await j.event(1,'Network.eventSourceMessageReceived',{requestId:'s',eventName:'update',eventId:'2',data:'payload'});
  assert.equal(j.read(1,{kind:'ws'}).events[2].base64_encoded,true);
  assert.equal(j.read(1,{kind:'sse'}).events[0].data,'payload');
  await j.event(1,'Network.requestWillBeSent',request('f'));
  await j.event(1,'Network.responseReceived',{requestId:'f',type:'Fetch',response:{mimeType:'text/event-stream'}});
  await j.event(1,'Network.dataReceived',{requestId:'f',data:'ZGVm'});
  assert.deepEqual(j.read(1,{kind:'stream'}).events.map(e=>e.data),['YWJj','ZGVm']);
});

test('bounds, capture identity and stop discard late bodies', async () => {
  let resolve;
  const j=new NetworkJournal(() => new Promise(r=>resolve=r)); j.begin(1);
  await j.event(1,'Network.requestWillBeSent',request());
  const pending=j.event(1,'Network.loadingFinished',{requestId:'1',timestamp:2});
  const capture=j.read(1).capture_id;
  j.end(1); resolve({body:'late'}); await pending;
  assert.equal(j.read(1).events.some(e=>e.kind==='http.body'),false);
  j.begin(1);
  assert.throws(()=>j.read(1,{capture_id:capture}),/Capture changed/);
  for(let i=0;i<1200;i++) await j.event(1,'Network.requestWillBeSent',request(String(i)));
  assert.equal(j.read(1).retained,1000);
  assert.equal(j.read(1).dropped,200);
  assert.ok(j.tabs.get(1).requests.size<=512);
  const id=j.read(1).capture_id; j.clear(1);
  assert.notEqual(j.read(1).capture_id,id);
  assert.equal(j.read(1).events.length,0);
  assert.throws(()=>j.read(1,{limit:201}),/Invalid/);
});

test('body failures and truncation are explicit', async () => {
  const j=new NetworkJournal(async()=>{throw new Error('evicted');}); j.begin(1);
  await j.event(1,'Network.requestWillBeSent',request());
  await j.event(1,'Network.loadingFinished',{requestId:'1',timestamp:2});
  assert.equal(j.read(1).events.at(-1).unavailable,'evicted');
  await j.event(1,'Network.webSocketCreated',{requestId:'w',url:'wss://example.test/ws'});
  await j.event(1,'Network.webSocketFrameReceived',{requestId:'w',response:{opcode:1,payloadData:'x'.repeat(100000)}});
  const row=j.read(1).events.at(-1);
  assert.equal(row.truncated,true); assert.equal(row.data.length,32768);
});
