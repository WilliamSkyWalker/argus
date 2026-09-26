# Existing browser extension backend

Argus can control all HTTP(S) tabs in your existing Chrome/Edge
profile, preserving its cookies and login state. This backend uses a Manifest V3
extension and a Native Messaging host; it does not need a remote debugging port.
Screenshots and coordinate input remain the visual interface; page DOM and cookies
are not extracted for the model.

```text
Argus CLI / Runtime (Linux, macOS, Windows or WSL)
        ↕ private shared directory (one-shot JSON requests)
Native Messaging host (runs on the browser's OS)
        ↕ Chrome native messaging / stdio
Argus Browser extension → all website tabs in the connected profile
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
library; Windows does not need the rest of Argus or Playwright installed.

1. Open `chrome://extensions` (Edge: `edge://extensions`), enable Developer mode,
   and **Load unpacked** → `extensions/argus-browser`. Keep these files at a
   permanent path. Copy the extension's 32-letter ID.
2. Register the native host for that exact extension ID. Run this command on the
   **same OS as Chrome**, using the Python executable you want Chrome to launch.

   Linux/macOS, from the repository:

   ```bash
   python3 -m argus.browser_bridge install \
     --extension-id YOUR_EXTENSION_ID \
     --directory "$HOME/.argus/browser-bridge"
   ```

   Windows PowerShell, with a permanent copy of `argus/browser_bridge.py`:

   ```powershell
   py -3 C:\Argus\browser_bridge.py install `
     --extension-id YOUR_EXTENSION_ID `
     --directory "$env:LOCALAPPDATA\Argus\browser-bridge"
   ```

   Add `--browser edge` for Edge. Windows registration is per user (HKCU);
   administrator access is unnecessary. Linux/macOS registration targets the
   normal Chrome/Edge user data directory. For a custom `--user-data-dir`, copy
   the generated `com.argus.browser.json` into its `NativeMessagingHosts/`
   subdirectory. Managed browser policy can block unpacked extensions or native
   messaging.
3. Open a normal website, then open the Argus extension popup. Click **Connect
   local bridge**. All existing and newly opened website tabs become available;
   no per-tab sharing is required. The popup shows available page IDs.
   Chrome may display its normal debugger control banner when Argus operates.

## Bind to Argus and operate

From the environment where Argus runs:

```bash
python3 -m argus.browser_bridge bind \
  --directory "$HOME/.argus/browser-bridge" --serial daily-web
python3 -m argus.cli device pages --serial daily-web
python3 -m argus.cli device select-page PAGE_ID --serial daily-web
python3 -m argus.cli device screenshot --serial daily-web --out /tmp/page.png
python3 -m argus.cli device navigate https://www.baidu.com --serial daily-web
```

For WSL, use the **Windows directory's WSL path** in `bind`, for example
`/mnt/c/Users/YOUR_USER/AppData/Local/Argus/browser-bridge`. Installing the host
inside WSL alone does not register it with Windows Chrome. A new bind refuses to
overwrite an existing device session. Subsequent device commands reconnect
through the saved directory. No Playwright package is required for this backend.

The first connection auto-selects only when exactly one website tab is available. Otherwise
select explicitly. Page identity includes a browser-session UUID, so restarting
Chrome cannot silently redirect a saved task to a reused numeric tab ID.

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
ARGUS_TEST_CHROME=/path/to/chromium \
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
python3 -m argus.cli device new-page https://example.com --serial daily-web
python3 -m argus.cli device pages --serial daily-web
python3 -m argus.cli device select-page PAGE_ID --serial daily-web
python3 -m argus.cli device close-page PAGE_ID --serial daily-web
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
