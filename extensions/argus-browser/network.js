// Bounded, in-memory CDP network journal. Independent of visual observations.
const MAX_EVENTS = 1000, MAX_BYTES = 1024 * 1024, MAX_TOTAL_BYTES = 8 * MAX_BYTES;
const MAX_TEXT = 32768, MAX_REQUESTS = 512;
const bytes = value => new TextEncoder().encode(JSON.stringify(value)).length;
export class NetworkJournal {
  constructor(send) { this.send = send; this.tabs = new Map(); }
  begin(tabId) {
    let capture = this.tabs.get(tabId);
    if (capture?.active) return capture;
    capture = {id:crypto.randomUUID(), active:true, reason:null, events:[], requests:new Map(),
      seq:0, dropped:0, untracked:0, bytes:0, pending:0};
    this.tabs.set(tabId, capture);
    return capture;
  }
  end(tabId, reason = 'stopped') {
    const c = this.tabs.get(tabId);
    if (c) { c.active = false; c.reason = reason; c.requests.clear(); }
  }
  clear(tabId) {
    const c = this.tabs.get(tabId);
    if (c) { c.events = []; c.bytes = 0; c.dropped = 0; c.id = crypto.randomUUID(); }
  }
  reset() { this.tabs.clear(); }
  append(c, event) {
    if (!c.active) return;
    // Bound every field, including large headers, post bodies and stream frames.
    let truncated = false;
    const text = JSON.stringify(event, (_key, value) => {
      if (typeof value === 'string' && value.length > MAX_TEXT) {
        truncated = true; return value.slice(0, MAX_TEXT);
      }
      return value;
    });
    let entry = JSON.parse(text);
    if (bytes(entry) > 65536) {
      entry = {kind:event.kind, request_id:event.request_id, url:event.url?.slice(0,2048),
        data_preview:text.slice(0,MAX_TEXT)};
      truncated = true;
    }
    entry = {...entry, seq:++c.seq, time:Date.now()/1000, truncated};
    const size = bytes(entry);
    c.events.push({entry,size}); c.bytes += size;
    const evict = capture => { const old = capture.events.shift(); capture.bytes -= old.size; capture.dropped++; };
    while (c.events.length > MAX_EVENTS || c.bytes > MAX_BYTES) evict(c);
    // Also cap total retention across all tabs.
    while ([...this.tabs.values()].reduce((sum,t) => sum+t.bytes,0) > MAX_TOTAL_BYTES) {
      const oldest = [...this.tabs.values()].filter(t => t.events.length)
        .sort((a,b) => a.events[0].entry.time-b.events[0].entry.time)[0];
      evict(oldest);
    }
  }
  read(tabId, {after=0, limit=100, url='', kind='', capture_id=null} = {}) {
    if (!Number.isSafeInteger(after) || after < 0 || !Number.isInteger(limit) || limit < 1 || limit > 200 ||
        typeof url !== 'string' || typeof kind !== 'string') throw new Error('Invalid network query');
    const c = this.tabs.get(tabId);
    if (!c) return {active:false, reason:'not_started', capture_id:null, events:[], next_cursor:0, dropped:0};
    if (capture_id && capture_id !== c.id) throw new Error('Capture changed; restart polling with cursor 0');
    const selected = c.events.map(e => e.entry).filter(e => e.seq > after &&
      (!url || (e.url || '').includes(url)) && (!kind || e.kind.startsWith(kind))).slice(0,limit);
    return {active:c.active, reason:c.reason, capture_id:c.id, events:selected,
      next_cursor:selected.length === limit ? selected.at(-1).seq : c.seq,
      oldest_cursor:c.events[0]?.entry.seq ?? c.seq+1, dropped:c.dropped,
      pending_bodies:c.pending, untracked_requests:c.untracked, retained:c.events.length};
  }
  async event(tabId, method, p) {
    const c = this.tabs.get(tabId);
    if (!c?.active || !method.startsWith('Network.')) return;
    const id = p.requestId;
    const put = (url,type) => {
      const r = {url:String(url).slice(0,MAX_TEXT), type, start:p.timestamp};
      c.requests.set(id,r);
      while (c.requests.size > MAX_REQUESTS) { c.requests.delete(c.requests.keys().next().value); c.untracked++; }
      return r;
    };
    let r = c.requests.get(id);
    const emit = (kind, data={}) => this.append(c, {kind, request_id:id, url:r?.url,
      timestamp:p.timestamp, ...data});
    if (method === 'Network.requestWillBeSent') {
      if (p.redirectResponse) emit('http.redirect', {status:p.redirectResponse.status,
        url:p.redirectResponse.url, headers:p.redirectResponse.headers});
      r = put(p.request.url,p.type);
      emit('http.request', {method:p.request.method, headers:p.request.headers,
        post_data:p.request.postData, has_post_data:p.request.hasPostData || false, resource_type:p.type});
    } else if (method === 'Network.webSocketCreated') {
      r = put(p.url,'WebSocket'); emit('ws.open');
    } else if (!r) {
      return; // Started mid-flight, or correlation was evicted: never misattribute events.
    } else if (method === 'Network.responseReceived') {
      r.type = p.type;
      emit('http.response', {status:p.response.status, headers:p.response.headers,
        mime_type:p.response.mimeType, protocol:p.response.protocol,
        from_cache:!!p.response.fromDiskCache, from_service_worker:!!p.response.fromServiceWorker});
      // Fetch-based SSE/NDJSON streams do not emit EventSource messages.
      if (p.type !== 'EventSource' && /^(text\/event-stream|application\/(x-)?ndjson)/i.test(p.response.mimeType || '')) {
        r.streamPending = true; r.chunks = [];
        const captureId = c.id;
        try {
          const initial = await this.send(tabId,'Network.streamResourceContent',{requestId:id});
          if (this.tabs.get(tabId) !== c || c.id !== captureId) return;
          if (initial.bufferedData) emit('stream.chunk', {data:initial.bufferedData, base64_encoded:true});
          for (const data of r.chunks) emit('stream.chunk', {data, base64_encoded:true});
        } catch (error) {
          if (this.tabs.get(tabId) === c && c.id === captureId) emit('stream.unavailable', {error:String(error.message || error)});
        }
        finally { r.streamPending = false; r.chunks = []; }
      }
    } else if (method === 'Network.dataReceived' && p.data) {
      if (r.streamPending) {
        if (r.chunks.length < 16) r.chunks.push(p.data.slice(0,MAX_TEXT));
        else emit('stream.gap', {reason:'pending_chunk_queue_full'});
        if (p.data.length > MAX_TEXT) emit('stream.gap', {reason:'chunk_truncated'});
      } else emit('stream.chunk', {data:p.data, base64_encoded:true});
    } else if (method === 'Network.loadingFailed') {
      emit('http.failed', {error:p.errorText, canceled:p.canceled, duration_ms:(p.timestamp-r.start)*1000});
      c.requests.delete(id);
    } else if (method === 'Network.loadingFinished') {
      emit('http.finished', {encoded_bytes:p.encodedDataLength, duration_ms:(p.timestamp-r.start)*1000});
      c.requests.delete(id);
      if (r.type === 'EventSource') return;
      if (c.pending >= 16) { emit('http.body', {unavailable:'body_queue_full'}); return; }
      c.pending++;
      const captureId = c.id;
      try {
        const body = await this.send(tabId,'Network.getResponseBody',{requestId:id});
        if (this.tabs.get(tabId) === c && c.id === captureId) emit('http.body', {body:body.body, base64_encoded:body.base64Encoded});
      } catch (error) {
        if (this.tabs.get(tabId) === c && c.id === captureId) emit('http.body', {unavailable:String(error.message || error)});
      } finally { c.pending--; }
    } else if (method === 'Network.eventSourceMessageReceived') {
      emit('sse.message', {event_name:p.eventName, event_id:p.eventId, data:p.data});
    } else if (method === 'Network.webSocketWillSendHandshakeRequest') {
      emit('ws.request', {headers:p.request.headers});
    } else if (method === 'Network.webSocketHandshakeResponseReceived') {
      emit('ws.response', {status:p.response.status, headers:p.response.headers});
    } else if (method === 'Network.webSocketFrameSent' || method === 'Network.webSocketFrameReceived') {
      emit(method.endsWith('Sent') ? 'ws.sent' : 'ws.received',
        {opcode:p.response.opcode, data:p.response.payloadData, base64_encoded:p.response.opcode !== 1});
    } else if (method === 'Network.webSocketFrameError') {
      emit('ws.error', {error:p.errorMessage});
    } else if (method === 'Network.webSocketClosed') {
      emit('ws.closed'); c.requests.delete(id);
    }
  }
}
