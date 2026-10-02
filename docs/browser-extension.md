# Existing browser extension backend

Saygo can control all HTTP(S) tabs in your existing Chrome/Edge
profile, preserving its cookies and login state. This backend uses a Manifest V3
extension and a Native Messaging host; it does not need a remote debugging port.
Screenshots and coordinate input remain the visual interface; page DOM and cookies
are not extracted for the model.

```text
Saygo CLI / Runtime (Linux, macOS, Windows or WSL)
        ↕ private shared directory (one-shot JSON requests)
Native Messaging host (runs on the browser's OS)
        ↕ Chrome native messaging / stdio
Saygo Browser extension → all website tabs in the connected profile
```

The directory transport lets WSL use a host running on Windows through `/mnt/c/`
without opening a network listener. It is intended for one trusted local user,
not network shares or remote multi-user operation. Anyone with write access to
this directory can issue browser commands; anyone with read access can read
screenshots and input in transit. Use a private user directory and restrictive
Windows ACLs. Normal completed requests/responses are removed; interrupted
processes can leave files containing sensitive data. Do not put this directory
in a repository or cloud-synced folder.

## Install on the browser's operating system

Python 3.11+ is required on that OS. The native host uses only the Python standard
library; Windows does not need the rest of Saygo or Playwright installed.

1. Open `chrome://extensions` (Edge: `edge://extensions`), enable Developer mode,
   and **Load unpacked** → `extensions/saygo-browser`. Keep these files at a
   permanent path. Copy the extension's 32-letter ID.
2. Register the native host for that exact extension ID. Run this command on the
   **same OS as Chrome**, using the Python executable you want Chrome to launch.

   Linux/macOS, from the repository:

   ```bash
   python3 -m saygo.integrations.browser_bridge install \
     --extension-id YOUR_EXTENSION_ID \
     --directory "$HOME/.saygo/browser-bridge"
   ```

   Windows PowerShell, with a permanent copy of `saygo/integrations/browser_bridge.py`:

   ```powershell
   py -3 C:\Saygo\browser_bridge.py install `
     --extension-id YOUR_EXTENSION_ID `
     --directory "$env:LOCALAPPDATA\Saygo\browser-bridge"
   ```

   Add `--browser edge` for Edge. Windows registration is per user (HKCU);
   administrator access is unnecessary. Linux/macOS registration targets the
   normal Chrome/Edge user data directory. For a custom `--user-data-dir`, copy
   the generated `com.saygo.browser.json` into its `NativeMessagingHosts/`
   subdirectory. Managed browser policy can block unpacked extensions or native
   messaging.
3. Open a normal website, then open the Saygo extension popup. Click **Connect
   local bridge**. All existing and newly opened website tabs become available;
   no per-tab sharing is required. The popup shows available page IDs.
   Chrome may display its normal debugger control banner when Saygo operates.

## Bind to Saygo and operate

From the environment where Saygo runs:

```bash
python3 -m saygo.cli device connect --platform browser --backend extension \
  --bridge-directory "$HOME/.saygo/browser-bridge" --serial daily-web
python3 -m saygo.cli device pages --serial daily-web
python3 -m saygo.cli device select-page PAGE_ID --serial daily-web
python3 -m saygo.cli device screenshot --serial daily-web --out /tmp/page.png
python3 -m saygo.cli device navigate https://www.baidu.com --serial daily-web
```

For WSL, use the **Windows directory's WSL path** in `device connect --bridge-directory`, for example
`/mnt/c/Users/YOUR_USER/AppData/Local/Saygo/browser-bridge`. Installing the host
inside WSL alone does not register it with Windows Chrome. Connecting refuses to
overwrite a session belonging to another platform or bridge. Subsequent device commands reconnect
through the saved directory. No Playwright package is required for this backend.

The first connection auto-selects only when exactly one website tab is available. Otherwise
select explicitly. Page identity includes a browser-session UUID, so restarting
Chrome cannot silently redirect a saved task to a reused numeric tab ID.

## Background screenshots and timeouts

Screenshot and size requests do not activate tabs, focus windows, or restore
minimized windows. Explicit page selection still brings the selected page forward.
Screenshots use `Page.captureScreenshot` with the existing PNG surface and viewport
clip settings. No alternate capture source or automatic reconnect is used.

Only one viewport request per page may remain outstanding. A request timeout does
not cancel Chrome's underlying command: later requests for that page report the
pending capture instead of piling up more screenshots. Other pages and read-only
diagnostics remain available. When Chrome completes the original command, the
pending guard is released; its expired image is discarded and it cannot start
another capture. A command that never completes remains visible as pending;
the extension does not detach the debugger to clear it.

`saygo device diagnose --session daily-web` reports the current window state,
focus, tab activity, debugger attachment, and last screenshot stage/timing. Update
both the runtime and extension to use these diagnostics. Screenshot timeout errors
also include the recorded state without needing a new runtime.

Native Messaging connections keep the extension worker alive; active debugger
sessions also do so on Chrome 118 and later. See the
[Chrome lifecycle documentation](https://developer.chrome.com/docs/extensions/develop/concepts/service-workers/lifecycle).
This worker lifetime guarantee does not guarantee a screenshot frame or protect
against browser/process exits. No heartbeat-triggered shutdown is used.

## Scroll a specific pane

Use `device_act` with `{"type":"scroll_at","x":30,"y":60,"coordinate_space":"percent","amount":-3}`
and a fresh observation ID to send the wheel event inside the intended pane.
Positive amounts scroll up; negative amounts scroll down. One unit requests
100 CSS pixels, delivered as a mouse scroll gesture at the chosen point. The browser determines the responding scroll container and actual
movement; observe again to verify. At a container boundary, normal browser scroll
chaining can move an ancestor. `scroll_up`/`scroll_down` still target the viewport
center, while `swipe` is a mouse drag. Updated runtime and extension files are
required for `scroll_at`; reload the extension after updating it.

The extension backend also supports the pointer actions `hover`, `double_click`,
`right_click` and `long_press` through `device_act`. `hover` moves the pointer
without clicking, which lets hover menus open before a separate observation.
Long press holds the primary button for the requested duration.

## Runtime and human handoff

Use this resource in an existing workflow:

```json
{"web": {"kind": "browser", "session": "daily-web", "backend": "extension"}}
```

`observe`, `pages`, browser actions, `human`, and resume verification use the same
Runtime protocol. See [the example workflow](../examples/workflows/extension_handoff.json)
and [Runtime documentation](runtime.md). A `human` step leaves the tab available
for manual work; after completing login/payment, return control with
`workflow resume RUN_ID --note "Manual work complete"`. The workflow must verify
the result separately. The example's URL check is only a navigation demonstration,
not proof of a real payment.

- Connecting enables all HTTP(S) tabs across windows in the current browser
  profile. Other profiles, browser internal pages and non-HTTP(S) URLs are excluded.
- New tabs and popups become available automatically. They never silently replace
  the selected task target; select their page IDs explicitly when needed.
- **Release browser and disconnect** detaches the debugger and prevents future
  dispatch. It cannot undo an already dispatched click. Chrome's debugger Cancel
  control blocks that tab until you explicitly disconnect and reconnect.
- Browser-session identity lives in `chrome.storage.session`. Reconnect after
  release/disconnection; after a browser restart, explicitly select again. Pending
  actions are never replayed automatically.
- CLI process exit only disconnects the Python adapter. It does not close Chrome.
  `device stop` removes the saved binding; use the extension popup to revoke
  browser access for all clients.
- Request timeouts, invalid responses and handshake errors must not close the
  extension's native port. Only the user-facing release button actively closes
  that port. Browser exit or an external transport failure can still end it.
  Late responses are ignored by request ID; timed-out input is never replayed.
  Queries and screenshots remain available while input is pending. A second
  input is rejected until the underlying input finishes. Closing the last
  browser tab through the extension is rejected; close it manually instead.
- Runtime locks alias bindings to the same bridge directory. Standalone device
  commands are not governed by Runtime locks; do not operate them concurrently
  with a running workflow. Human changes can still occur between observation and
  input; the existing fresh-screenshot guard is conservative, not transactional.
- No DOM automation, arbitrary script tool, downloads, native dialog handling,
  Firefox support, or automatic extension installation. A timeout after dispatch
  is an uncertain result; Runtime uses `needs_review` rather than retrying it.

## Verification

```bash
python3 -m unittest discover -s tests/extension_demo -v
SAYGO_TEST_CHROME=/path/to/chromium \
  python3 -m unittest discover -s tests/extension_demo -v
```

The optional integration test uses Playwright only as a test harness, a temporary
browser profile/native-host registration, and a localhost fixture. It does not
access a personal profile. Windows host registration, Windows↔WSL transport and navigation/screenshot
have also been manually verified with a real Chrome session.

Chrome API references: [Native Messaging](https://developer.chrome.com/docs/extensions/develop/concepts/native-messaging),
[debugger transport](https://developer.chrome.com/docs/extensions/reference/api/debugger).

## Create, select and close tabs

Playwright and extension sessions both support:

```bash
python3 -m saygo.cli device new-page https://example.com --serial daily-web
python3 -m saygo.cli device pages --serial daily-web
python3 -m saygo.cli device select-page PAGE_ID --serial daily-web
python3 -m saygo.cli device close-page PAGE_ID --serial daily-web
```

`new-page` requires an HTTP(S) URL and returns `created_page_id`. It preserves the
selected task target; use `select-page` to switch to the new tab. Closing the
selected tab requires explicitly selecting another before further input.
Creation works even without a current page selection (Playwright requires an
unambiguous browser context). Extension navigation may still be loading when
creation returns. If creation times out, inspect pages before retrying.

Runtime actions use `{"type":"new_page","url":"https://example.com"}` and return
`created_page_id`. A later `select_page` action can reference it using
`{"$ref":"steps.create.created_page_id"}`. These actions still require a current
observation and use the durable intent/review protocol. An uncertain creation
must be reconciled manually; it is never automatically replayed.

## Network observation (extension 0.3.0+)

Connecting the extension automatically starts passive capture on all controllable
HTTP(S) tabs in this browser profile, including new tabs and popups. There is no
additional Saygo permission dialog. Chrome's own debugger indicator still applies.
The selected session tab determines which journal the CLI reads:

```bash
saygo device network --session web
saygo device network read --session web --url /api --kind http
saygo device network read --session web --kind ws --after 100 --capture-id CAPTURE_ID
saygo device network read --session web --kind sse
saygo device network read --session web --kind stream
saygo device network stop --session web
saygo device network start --session web
saygo device network clear --session web
```

- `http.*`: request URL/method/headers/post data, response status/headers, completion
  duration, failure and response body (or an explicit `unavailable` reason).
  Redirects are separate events sharing the CDP request ID.
- `ws.*`: opening, handshake, sent/received frames, errors and closure. Text frames
  are text; binary frames retain their opcode and base64 payload.
- `sse.message`: native EventSource messages, including event name and event ID.
- `stream.chunk`: fetch-based `text/event-stream` and NDJSON response chunks,
  base64 encoded. Decode and concatenate chunks in event order; transport chunks
  are not necessarily complete SSE messages. Unsupported CDP streaming returns
  `stream.unavailable`. Ordinary HTTP long polling appears as HTTP requests.

Poll using `next_cursor` as the next `--after`, together with `capture_id`.
A changed capture ID means clear/restart: begin again at cursor 0. `--limit` is
1–200; URL and event-kind filters apply before pagination. `dropped` and
`oldest_cursor` expose journal eviction; `untracked_requests` counts evicted
request correlations; `truncated` marks clipped fields.
Retention is bounded to 1,000 events/1 MiB per tab and 8 MiB across tabs; fields
are capped at 32 Ki characters, and active request correlation at 512 entries per
tab. Response bodies are best effort and may be unavailable after cache eviction,
redirects, target changes or excessive simultaneous completions.

`stop` pauses only recording, keeps existing records and does not close network
connections or disable visual control. `start` resumes with a fresh capture.
`clear` removes retained events while keeping the current recording state.
Disconnect/release, extension reload and browser exit discard in-memory records.
Capture is not retroactive: attach races can miss a new tab's first requests;
already-open sockets have no guaranteed history. Separate worker/out-of-process
iframe targets are not attached in this version. This command currently requires
the **extension backend**, not standalone Playwright/Selenium sessions.

Network records can include credentials in headers and payloads; they stay in the
extension's bounded memory until queried through the local bridge. They are not
added to visual model prompts or QA reports automatically. Export CLI JSON only
to an appropriate local destination. Reload the installed extension after updating
all extension files (including `network.js`), then reconnect the bridge.

Implementation uses the [Chrome debugger API](https://developer.chrome.com/docs/extensions/reference/api/debugger)
and [CDP Network events](https://chromedevtools.github.io/devtools-protocol/tot/Network/).
