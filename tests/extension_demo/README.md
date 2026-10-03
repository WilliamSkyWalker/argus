# Browser extension checks

Offline checks:

```sh
node --test tests/extension_demo/test_*.mjs
python3 -m unittest discover -s tests/extension_demo -p test_bridge.py -v
```

`test_capture.mjs` uses mocked window states (including minimized) to ensure
requests never activate tabs or change focus. These are control-policy checks,
not proof of minimized-window support. It also covers per-page pending ownership, late results, no post-timeout capture
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

Check foreground, background but visible, fully covered, and inactive tab states.
Confirm the timestamp changes, the image matches the fixture, and the target
does not become active/focused. Leave the target tab inactive in a normal window
with no Saygo requests for 35 seconds and capture again. Compare bridge identity
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

## Automated regression gates

The shared `.github/workflows/checks.yml` is required by both release workflows.
Pull requests affecting any tests or workflow files trigger the package workflow.
It runs all offline control, runtime, desktop, CLI, mobile, browser and bridge
suites in separate processes and state directories, plus all extension JavaScript
suites. Hardware-only tests remain explicit opt-ins; they are not counted as
executed simply because discovery succeeded.

```sh
python -m pip install '.[mcp,browser,desktop,qa,selenium,mobile]'
python scripts/check_core_tests.py
node --test tests/extension_demo/test_*.mjs
```

A separate Linux job runs the real extension/native host in headed Chromium under
Xvfb and Openbox. It checks foreground, background, covered and inactive
tab captures with exact fixture colors; window/tab state must stay unchanged.
It also checks a 35-second idle interval with an inactive tab in a normal window,
12 consecutive captures, actual scroll
movement in independent panes, fractional fine adjustments, reverse direction,
and top/bottom boundaries. Browser integration failures block publication.

```sh
python -m playwright install --with-deps chromium
# Install xvfb, openbox and x11-utils with the system package manager.
xvfb-run -a -s '-screen 0 1600x1000x24' python scripts/check_browser_tests.py
```

Use an isolated virtual display. The live runner requires a functioning window
manager and fails if it cannot start. Minimized windows are excluded from
acceptance by the requested scope; inactivity coverage remains on background
tabs in normal windows. Fixture JavaScript and scroll offsets are
only test setup/oracles, never production perception. Multi-hour endurance tests
are deliberately excluded. Native Windows/macOS background browser behavior and
lock-screen/suspend still require dedicated platform environments.

The deterministic capture tests also cover navigation (including same-URL reload),
closing a tab, manual release while a capture is pending, simultaneous clients,
viewport changes, and cleanup of transient tabs. Simulated time advances the
extension's real deadline values; tests do not rely on 30–50 ms wall-clock races.
Navigation, closure and release must discard the outstanding image rather than
returning an observation from an obsolete page/control state.

Adaptive scroll tests validate model input images, capabilities, and dispatched
amounts using a scripted model. They do not establish that every real model will
choose an appropriate amplitude; that remains a separate model evaluation.

### Current regression finding

The expanded Linux headed run (Chrome for Testing 153.0.8010.12, Xvfb/Openbox)
reproduces `Page.captureScreenshot` timeouts when the extension target is minimized,
including after the short idle interval. The bridge remains connected. The same
run passes the other window/tab states, scroll assertions and capture burst.
Disabling Playwright viewport emulation did not resolve the failure. Removing the
clip in a disposable extension copy also did not resolve it; production capture
parameters have not been changed on that basis.

Minimized-window support was subsequently excluded from the requested scope.
The release gate no longer tests that state, and the 35-second idle check now
uses an inactive tab in a normal window. The historical failure above is not
resolved and the earlier successful Windows sample is not a support guarantee. The capture invalidation bugs exposed by deterministic
navigation/reload/closure/release tests have been corrected in this checkout.

An additional isolated Linux probe disables Playwright's automatic focus
emulation and switches to a different tab before each bridge operation.
Background screenshot and click succeed, but the first
`Input.synthesizeScrollGesture` times out after 20 seconds. Enabling focus
emulation in a disposable extension does not resolve that timeout. This does not
yet establish the cause of the Windows long-session screenshot incident.

`test_background_tab_input_after_manual_tab_switch` covers this missing case:
12 foreground/background transitions, screenshots, clicks, and alternating
scrolls in one pane. It checks tab/window state, exact fixture scroll offsets,
click counts and bridge identity. It stops sending input after a failed round.
The earlier scroll assertions only exercised the foreground tab. With wheel
input and temporary focus emulation, the new case passes all 12 rounds through
the real extension/native bridge on Linux. The launch also removes Playwright's
three background-throttling overrides. Before the scope change, the complete
headed run took about 146 seconds and failed the minimized and minimized-idle
capture checks; its other capture states and foreground scroll assertions passed.

A separate disposable Windows Chrome 154.0.8037.93 CDP probe reproduces a wheel
timeout without focus emulation. With focus emulation, two rounds pass before
an unlabelled state assertion fails; the follow-up diagnostic run was blocked
by approval timeouts. This is incomplete Windows evidence, not extension
acceptance or proof that the long-session screenshot incident is resolved.
