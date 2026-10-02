# Browser extension checks

Offline checks:

```sh
node --test tests/extension_demo/test_*.mjs
python3 -m unittest discover -s tests/extension_demo -p test_bridge.py -v
```

`test_capture.mjs` covers background/minimized windows without tab activation or
focus changes, per-page pending ownership, late results, no post-timeout capture
dispatch, diagnostic availability, recovery after the original call finishes,
and preservation of screenshot diagnostics across subsequent size reads.

## Manual background capture matrix

Serve `capture_fixture.html` on localhost and open it in a dedicated test window
in the browser profile containing the extension. Bind a separate Saygo extension
session to its explicit page ID. Do not navigate or send input to unrelated tabs.

```sh
python3 -m http.server 8765 --bind 127.0.0.1 --directory tests/extension_demo
saygo device screenshot --session capture-test
saygo device diagnose --session capture-test
```

With WSL, the browser must be able to reach the fixture server; running the server
on Windows avoids host forwarding dependencies. Use the real extension/Native
Messaging route for acceptance. A direct CDP harness only checks Chrome's capture
behavior, not the extension or bridge.

Check foreground, background but visible, fully covered, minimized, and inactive
tab states. Confirm the timestamp changes, the image matches the fixture, and the
target does not become active/focused/restored. Leave the window minimized with no
Saygo requests for more than 60 seconds and capture again. Compare bridge identity
and debugger attachment before/after. Close only fixture tabs when done.

## Observed coverage (2026-10-03)

Windows Chrome 154.0.8037.93 with the extension and WSL MCP client:

| State | Observation result |
| --- | --- |
| Foreground | Fresh screenshot |
| Background, visible | Fresh screenshot, foreground stayed on the cover window |
| Fully covered | Fresh screenshot, foreground stayed on the cover window |
| Minimized | Fresh screenshot, target remained minimized |
| Minimized after about 84 seconds without requests | Fresh screenshot, connection retained |
| Inactive tab | Fresh screenshot, tab remained inactive |

Background MCP observations took approximately 1.4–1.6 seconds, including bridge
and observation processing. A separate disposable-profile CDP experiment returned
20 screenshots across five window/tab conditions with/without a viewport clip;
all completed, approximately 0.03–0.17 seconds per capture. These measurements
are local samples, not latency guarantees. No alternate image source, browser
throttling flags, heartbeat, or automatic reconnect was needed.

The previously reported intermittent capture timeout was not reproduced in this
matrix. This does not prove its underlying cause has been fixed. The timeout
tests use deliberately unresolved mock commands; they validate request handling,
not recovery from a real Chrome renderer hang. Lock-screen, OS suspend, long-term
idle, other Chrome versions, and other operating systems remain unverified.
