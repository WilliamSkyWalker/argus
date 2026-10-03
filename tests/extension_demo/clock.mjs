// Deterministic timers for extension tests; never wait for wall-clock deadlines.
export class Clock {
  now = 1000;
  next = 0;
  timers = new Map();
  setTimeout = (fn, ms) => {
    const id = ++this.next;
    this.timers.set(id, {at:this.now+ms, fn});
    return id;
  };
  clearTimeout = id => this.timers.delete(id);
  Date = class extends Date { static now = () => this.owner.now; };
  constructor() { this.Date.owner = this; }
  async flush() {
    // Drain nested async Chrome API mocks without advancing simulated time.
    await new Promise(resolve => setImmediate(resolve));
  }
  async advance(ms) {
    await this.flush();
    const end = this.now + ms;
    while (true) {
      const entry = [...this.timers].filter(([,t]) => t.at <= end)
        .sort((a,b) => a[1].at-b[1].at || a[0]-b[0])[0];
      if (!entry) break;
      const [id,timer] = entry;
      this.timers.delete(id); this.now = timer.at; timer.fn();
      await this.flush();
    }
    this.now = end;
    await this.flush();
  }
}
