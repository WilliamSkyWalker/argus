# External agent operation

Argus shares persistent sessions between CLI and MCP for supported backends. Playwright is CLI-only: MCP rejects Playwright connections, observations, actions and task execution, and lists these bindings under `unavailable_sessions`. MCP browser connections default to the extension backend. The device profile needs no model API key. New input commands do not implicitly create Android sessions if a binding is missing or expired.

## Install in a project

For external Claude Code/Codex operation, prefer the managed plugin installer:

```bash
python3 scripts/install_agent_plugin.py --client both
```

It installs the runtime, native plugins, shared skill and MCP configuration together. Restart the client and describe the UI task; no manual skill-loading prompt or MCP path configuration is needed. See [plugin installation](../plugins/argus-device/README.md) for prerequisites, platform extras and validation limits. The commands below are the lower-level Python-package installation route.

Build/install this checkout with Python 3.10+:

```bash
python -m venv .venv
. .venv/bin/activate
pip install '/path/to/argus[browser,mcp]'
python -m playwright install chromium
argus --help
```

The distribution name is `argus-agent-control`; the executable is `argus`. This change provides packaging but does not publish a PyPI release. For a browser-only setup, `browser,mcp` installs no Appium toolchain. Extras: `mobile`, `selenium`, `windows`, `mac`, `qa`. Mobile users can then run `argus device install --help` for the device toolchain. WSL uses the bundled PowerShell runner for Windows control.

An MCP client can start `argus-mcp --profile device` using the environment's executable path. Keep the client's working directory at the user's project. The plugin launcher also accepts an installed package; `ARGUS_HOME` is only needed for a checkout that is not installed.

## Sessions and observations

```bash
argus device connect --platform browser --backend extension --bridge-directory /path/to/bridge --session mail
argus device connect --platform android --device DEVICE_ID --session phone
argus device connect --platform windows --app 'Test Admin' --session admin
argus device capabilities --session mail
argus doctor --session mail
argus device screenshot --session mail
argus device act '{"type":"tap","x":50,"y":40,"coordinate_space":"percent"}' \
  --session mail --observation-id OBSERVATION_ID --observe-after
```

`device_act` takes the same action object. `device_observe`, `device_act` with a resulting observation, and task observe/submit/recover/resume return MCP image content; legacy `device_screenshot` returns metadata and a local path. Screenshots include a unique ID, timestamp, session, target, image dimensions, screen dimensions and image-to-screen mapping. `image` and `crop` coordinate spaces require a source observation ID. Crops are image-pixel `[left,top,right,bottom]` rectangles, magnified 2× with an explicit mapping back to screen coordinates.

Actions use `tap`, `swipe`, `input`, `press_key`, `open_url`, `open_app`, `scroll_up`, `scroll_down` and backend-dependent `double_click`, `right_click`, `hover`, `hotkey`, `long_press`, `scroll_at`. Query capabilities. `swipe` implements a drag on pointer backends. `hotkey` uses `keys: ["ctrl","a"]`; `long_press` uses seconds in `duration`; `scroll_at` uses `x`, `y`, and signed `amount` in wheel notches (positive up). Unsupported actions return errors.

`argus device wait --session mail --mode stable --timeout 5` waits for stability; `--mode change` waits for change. A timeout returns `condition_met: false, timed_out: true`. Screenshot calls remain subject to the backend's own I/O timeout. A stable frame is not business success. Action results expose `business_success: null` and require external verification.

Visual revalidation compares decoded pixel changes, permitting small changes such as a distant caret; it additionally checks a small region around each pointer target. Changes to that region, page identity, URL or dimensions require re-observation. Desktop observations also include window ID, process ID and window bounds; supported desktop adapters recheck these before input and reject a changed target. Native foreground adapters check the foreground target too. These checks cannot prevent unrelated programs or people from changing focus during input. Pixel comparison is a heuristic, not proof that the business target is unchanged. All visual observations expire for action submission after 30 seconds.

## Interactive cross-device tasks

Bind project aliases once (stored in `.argus/resources.json`):

```bash
argus resources bind phone phone
argus resources bind mail mail
argus resources bind admin admin
argus task create
# Or: argus task create '{"phone":"phone","mail":"mail","admin":"admin"}'
argus task observe TASK_ID --resource phone
argus task submit TASK_ID --resource phone --request-id register-focus \
  --observation-id OBSERVATION_ID \
  --action '{"type":"tap","x_pct":50,"y_pct":40}' --note 'Focus the registration form'
argus task observe TASK_ID --resource mail
argus task handoff TASK_ID --instructions 'Please sign into the test mailbox'
argus task resume TASK_ID --note 'User returned control after login'
argus task recover TASK_ID
argus task timeline TASK_ID
argus task events TASK_ID
argus task finish TASK_ID --note 'Verified the activation and admin result'
argus task export TASK_ID --out evidence.zip
```

MCP `agent_task(command, task_id, options)` exposes these operations. `create` accepts `options.bindings`; `submit` accepts `resource`, `action`, `observation_id`, `request_id` and optional `note`. Persist the returned task ID in the agent's own context. Status and execution events live in Runtime's SQLite store, defaulting to `$ARGUS_HOME_DIR/runtime` (`~/.argus/runtime`). `ARGUS_RUNTIME_DIR` overrides the task store; all clients must use the same value. Session state and global operation locks use `$ARGUS_HOME_DIR` regardless of working directory.

Dispatch intent is committed before input. Lost responses never cause automatic replay: an accepted request ID cannot dispatch twice. Interrupted dispatch becomes `needs_review`; `resolve --outcome completed|not_executed --note ...` reconciles it. Both outcomes invalidate old observations. Submitting a new action is an explicit decision. An `idle` task with an error rejected its action before dispatch; inspect the event history. A failed screenshot after acknowledged input is recorded separately in the action's `evidence.error`, without marking the input uncertain or replaying it.

Task handoff blocks its bound sessions. Resume obtains fresh screenshots and leaves business verification to the agent. Individual session handoff works across mobile, browser and desktop too. Tasks and workflows retain global resource ownership through idle, handoff and uncertain-result states. Ordinary device commands and other tasks cannot use those resources until `finish` or explicit `cancel --note ...`; use the owning task interface. `argus task list` locates unfinished tasks. OS locks also serialize in-flight operations across entrances and across all local desktop windows. These locks protect Argus operations, not unrelated programs or a human using the host.

All device operations write local JSONL facts under `$ARGUS_HOME_DIR/operations`; mutating commands capture before/after images when available. Page creation, selection and closing also record the selected page and page list; closing the selected page records screenshot unavailability without selecting another page. Interactive tasks and planned workflows share durable before/after evidence: `action_dispatching.before` records the source observation, and `action_evidence` records the resulting observation or capture error, including after a dispatch exception. Agent notes are separate `agent_annotation` events. Export includes state, execution facts and captured screenshots; it may contain application data, input text and messages from the task.

## Validation status

| Path | Validation in this change |
|---|---|
| CLI/MCP session sharing, mapping, handoff, uncertain dispatch | Offline contract tests |
| Cross-resource task recovery and request deduplication | Offline tests with simulated devices |
| Browser / Playwright | CLI/Runtime only; MCP rejects this backend. Local Chromium two-page task, subprocess recovery and handoff tested |
| Installed wheel outside checkout | Clean Python 3.12 virtual environment without system site packages; `browser,mcp` extras, CLI/doctor/MCP startup and live Chromium integration passed; no Appium, Selenium or OpenAI installed |
| Android / iOS physical devices | Existing adapters; new complete task path requires device validation |
| Windows via WSL PowerShell runner | Isolated live background fixture: input, reconnect, window ID/process/bounds rejection; foreground, cursor and clipboard preserved. Foreground integration not run |
| Native Windows / macOS desktop | Window identity checks implemented; native adapters require host validation |
| Browser extension / Selenium | Existing adapter tests; advanced actions vary by backend |

The isolated installation run used Pillow 12.3.0, Playwright 1.63.0 and MCP 2.2.0 with an existing host Chromium binary. It verifies Python dependency isolation and operation outside the checkout, not browser/system dependency installation on a fresh operating system. The wheel is installable but has not been published to a package index.

The real phone registration → mailbox activation → desktop administration acceptance scenario is not certified by simulated tests. It requires designated test applications/accounts and a real-device run. `doctor` checks connection, capture and local image decoding; it reports input permission as untested unless you explicitly pass `--probe-action` with an action on a chosen harmless target; the resulting screenshot still requires visual verification. Platform support should remain experimental until the relevant real run is recorded.
