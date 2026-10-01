# Unified device interface

Use `saygo device` for mobile, desktop and browser control. Each connected target
has a named session; shared actions use `--session` (`--serial` remains an alias).
Standalone `saygo mobile`, `saygo devices`, `saygo setup`, and
`saygo.integrations.browser_bridge bind` have been removed. Use `device list`, `device connect`,
`device install`, and `device boot` instead. `device list` always emits JSON.
`device start/stop` remain within this unified command group. The browser bridge's
`install/host` commands remain for native-host registration and Chrome startup.

```bash
python3 -m saygo.cli device list
python3 -m saygo.cli device sessions

# Mobile: physical device or booted simulator
python3 -m saygo.cli device connect --platform android --device DEVICE_ID --session phone
python3 -m saygo.cli device connect --platform ios --device UDID --session iphone

# Existing browser (install/connect extension first)
python3 -m saygo.cli device connect --platform browser --backend extension \
  --bridge-directory /path/to/browser-bridge --session web
# Or create/reconnect an Saygo-managed Chromium session
python3 -m saygo.cli device connect --platform browser --backend playwright --session test-web

# Desktop: Windows window-title substring / macOS application name
python3 -m saygo.cli device connect --platform desktop --app "Example App" --session desktop

# Same commands across all connected targets
python3 -m saygo.cli device screenshot --session phone --out screen.png
python3 -m saygo.cli device tap 100 200 --session phone
python3 -m saygo.cli device input "Hello" --session web
python3 -m saygo.cli device key enter --session desktop
python3 -m saygo.cli device scroll down --session web
python3 -m saygo.cli device open https://example.com --session web

# Simulator provisioning uses the same entry point
python3 -m saygo.cli device install --platform android --boot --connect
python3 -m saygo.cli device install --platform android --host auto --dry-run
python3 -m saygo.cli device install --platform ios --boot --connect
python3 -m saygo.cli device boot --platform android Saygo

# Browser-specific actions
python3 -m saygo.cli device pages --session web
python3 -m saygo.cli device new-page https://example.com --session web
python3 -m saygo.cli device select-page PAGE_ID --session web
python3 -m saygo.cli device close-page PAGE_ID --session web

python3 -m saygo.cli device disconnect --session web
```

`list` discovers local phones/simulators and desktop windows, and lists saved
browser bindings; it does not attach to every browser or take screenshots.
`sessions` lists saved configuration, not a live health check. Diagnostics for an
unavailable platform do not hide results from another platform.

`install/boot --host auto` can provision Android on the Windows host when WSL has
no usable KVM. `--host local` opts out; `--host windows` selects it explicitly.
See [host installation](mobile.md) for SDK consent, progress and prerequisites.

`disconnect` marks the session unavailable until an explicit `connect`. It ends
an Appium session, but does not close the phone, simulator, desktop application
or browser. Browser endpoint/selection are retained for reconnect. It does not
interrupt a controller already executing in another process or revoke the browser
extension globally; use the extension's Release button for that. The legacy
`device stop` retains its stronger managed-browser shutdown behavior.

Desktop control is window-scoped and supports macOS/Windows, including WSL's
Windows runner. It can move foreground focus. Linux desktop control is not
implemented. Closing a short-lived command tears down only its controller.

The shared command names do not erase platform semantics: `open` takes a URL for
browsers and an app/package target for other platforms; supported keys differ.
Tap/swipe coordinates are logical device/window pixels, matching `screen_size`.
Retina screenshot pixels may differ; inspect the returned scale. Runtime actions
continue to use percentages and observation guards. Standalone CLI actions do
not acquire Runtime workflow resource locks.

Runtime can use the same saved desktop session, as it already does for mobile
and browser sessions:

```json
{"phone": {"kind": "android", "session": "phone"},
 "web": {"kind": "browser", "session": "web", "backend": "extension"},
 "desktop": {"kind": "windows", "session": "desktop"}}
```

Use `kind: mac` for a Mac. Existing desktop resources with an inline `app` remain
supported. All desktop aliases use the same physical-desktop Runtime lock.

See [mobile setup](mobile.md) and [browser extension setup](browser-extension.md)
for installation requirements. This unifies CLI/session routing; existing MCP
registration is unchanged. Tests in `tests/control_demo` use mocked platform
adapters; real mobile and desktop hardware validation remains environment-dependent.

Implementation boundaries: `platforms/device_session.py::release_controller`
owns temporary controller cleanup for the CLI, connection management and Runtime.
It releases desktop controllers, disconnects Playwright/extension controllers,
and stops temporary Selenium services while preserving persistent sessions.
`mobile.py::provision_runtime` shares SDK, Appium and optional boot orchestration
between native installs and the Windows worker; each host adapter owns connection
setup. QA Grid startup uses `platforms/selenium_grid.py::cleanup_grid_sessions`
from both single-browser and parallel runs.
Desktop drivers share pointer actions, scrolling, key aliases, clipboard input
and long-press dispatch in `platforms/desktop.py::DesktopPlatform`. Native
adapters retain window discovery/capture, coordinate conversion, clipboard
access and paste modifiers (Command on macOS, Ctrl on Windows). Tests cover
negative window origins, Retina scale, Unicode paste and clipboard fallback
without sending input to a real desktop.

### Desktop application lifecycle

Windows connections (native and WSL) share a process-aware resolver. They reuse
existing processes, restore an existing hidden/minimized window, and launch only
when no matching process exists. Localized installed-app names are resolved via
Start Menu metadata where available. `--process` can identify an executable when
its display name and window title differ; `--launch` supplies a launch executable.
Running without an accessible window returns `waiting_for_human`, never repeated
launch attempts. A successful connection returns process/window identity and
`launched`/`restored` flags; it does **not** assert login readiness.

```bash
saygo device connect --platform windows --app Example --process Example --launch 'C:\Example\Example.exe' --session desktop
```

An explicit new-window request is different from reconnecting. Use `--new-window`
with `--launch` and application-supported `--new-window-arg=VALUE` arguments.
Saygo makes one attempt and verifies that another window appeared. It never
assumes that starting another process means a new window. Missing arguments are
rejected before launch; ordinary screenshots/actions do not repeat this intent.
These explicit lifecycle options currently apply to Windows.

When visual observation finds a login/payment/manual-interaction page, the caller
can persistently pause a standalone desktop session:

```bash
saygo device handoff --session desktop --reason login --instructions 'Complete login in the existing window'
saygo device resume --session desktop --note 'Login completed'
```

Device actions and reconnect are blocked while the handoff is pending. Resume
must successfully capture a new screenshot before clearing the pause and returns
`needs_observation`; inspect that image before continuing. This is a persisted
control state, not a conversation reminder. Window metadata cannot classify login
screens: visual readiness remains the agent's responsibility. Workflow runs use
`workflow pause/resume` and their existing human/verification steps instead.

### Windows background control (experimental)

```bash
saygo device connect --platform windows --app 'Example App' --session work --background
saygo device screenshot --session work --out /tmp/work.png
saygo device tap 120 90 --session work
saygo device input 'Example text' --session work
```

`--background` selects the PowerShell/Win32 runner on native Windows; WSL already
uses this runner. It captures the target with PrintWindow and dispatches input to
that window's controls without global mouse, keyboard or clipboard injection.
Missing PowerShell, a closed window or unavailable capture produces an error;
there is no automatic foreground or desktop-screenshot fallback.

Standard Edit controls position the caret by the requested coordinates without
setting OS focus. Standard/WinForms buttons use their window-bound accessibility
action. Other controls receive window messages and may ignore them or activate
their own window; background support is application-dependent, especially for
GPU-rendered and custom controls. Successful dispatch is not proof of a UI change.
Minimized windows may need restoring; this is not a headless or locked-screen mode.

Tap the intended input control first. Saved device sessions retain its binding
across CLI calls and validate window/process/class membership on reconnect;
missing or invalid bindings are rejected rather than guessing another field.
A replaced control can reuse a handle, so verify the current screenshot after UI
changes and tap again. Ctrl+A supports Edit/RichEdit controls; other modifier
shortcuts are rejected. Use a visual button or human handoff when unsupported.

Opt-in background regression on an unlocked Windows/WSL desktop:

```bash
SAYGO_TEST_WINDOWS=1 python3 -m unittest discover -s tests/control_demo -p test_background_live.py -v
```

The test creates only its own non-activating fixture and cover windows.
It verifies occluded capture, two text fields, Unicode input, button clicks,
coordinates, restored bindings, invalid targets, and zero target activation.
It also reports cursor/foreground/clipboard changes; concurrent human activity can
change these. Set `SAYGO_TEST_WINDOWS_IDLE=1` on an idle desktop to assert all three
remain unchanged. The separate foreground test is skipped unless
`SAYGO_TEST_WINDOWS_FOREGROUND=1` is explicitly set; it moves the mouse and focus.
Tests do not call an LLM or establish compatibility with every Windows app.

### Explicit Windows foreground actions

Use the same `saygo device` entry point when a custom/GPU application cannot be
operated or observed reliably in the background. `--foreground` applies only to
that command; it does not change the saved session's background preference.

```bash
# Restore the existing bound window and capture its visible contents.
saygo device focus --session work --out /tmp/window.png

# Prepare and inspect a draft; this does not click Send.
saygo device type-send 'Example message' --session work --foreground --replace --prepare-only --input-x 400 --input-y 620 --out /tmp/draft.png

# After visually confirming the recipient and draft, click Send and capture the result.
saygo device tap 800 690 --session work --foreground --out /tmp/result.png
```

Coordinates are examples, not application-specific defaults. `--replace` selects
all in the input field before typing. Without `--prepare-only`, `type-send`
requires both send coordinates and performs the authorized submission in one
controller connection. Its result reports `submitted` and `requires_observation`;
this is dispatch plus a screenshot, not an independently verified delivery.
Errors after attempting submission include `submission_attempted: true`: inspect
the result before retrying to avoid duplicates.

Foreground actions check the bound window is foreground before injecting input,
reject taps covered by another window, and use Unicode input without changing the
clipboard. They do move the mouse/focus; avoid simultaneous desktop interaction.
Foreground screenshots capture actual visible pixels, so overlays may appear.
The background path never automatically switches to foreground mode.

These commands reuse the fixed CLI authorization boundary instead of requiring
application-specific temporary scripts. Approval decisions still belong to the
calling agent's environment; Saygo cannot bypass or guarantee the absence of
sandbox/OS approval prompts. Device commands themselves do not call an LLM:
the calling agent inspects their screenshots; `saygo run` uses the configured LLM.

Validation note: the foreground replacement path remains pending live verification
after its latest change. Background validation does not exercise global input.

## macOS background mode

Install the `mac` extra (including the ApplicationServices PyObjC binding), open
the intended app manually, and keep its window on the current desktop. Connect
with an explicit background session:

```sh
saygo device list --platform mac
saygo device connect --platform mac --app 'Example App' --background --session mac-bg
# If the app has multiple eligible windows, add --window-id from device list.
saygo device capabilities --session mac-bg
saygo device screenshot --session mac-bg
saygo device act '{"type":"tap","x":50,"y":40,"coordinate_space":"percent"}' \
  --session mac-bg --observation-id OBSERVATION_ID --observe-after
```

MCP uses `device_connect(platform="mac", session="mac-bg",
options={"app":"Example App", "background":true})`. CLI, MCP and interactive
tasks share the saved background flag, process and window identity. A new
controller must reattach to that window; it cannot silently pick a replacement
after the app or window closes. Use a new session to choose foreground mode.
The desktop connection dialog also exposes the background option.

Background capture never activates or launches an app. The model still decides
from screenshots. A coordinate tap executes the native control's `AXPress`;
right-click requires `AXShowMenu`. The driver performs only an app-scoped hit test
and bounded native window/ancestor checks to execute the chosen coordinates; it
does not return a UI tree, control text or semantic element locator to the model.
Unsupported/custom-drawn controls fail without sending a global mouse event.

Text is sent as process-directed Unicode key events, without using the clipboard.
`input` and unmodified `press_key` require exactly one eligible application window
and an existing focused control belonging to that window. This mode does not
focus a text field by activating the application. Printable text is supported;
use `press_key` for control keys. Shortcuts, dragging, double-click, hover,
long-press and app/URL opening are deliberately unsupported. There is no global
input or foreground fallback, including through legacy commands.

Scrolling requires a native vertical scrollbar. `scroll_at` targets the area
under its coordinates; `scroll_up/down` use the window centre. The capability
reports `scroll_at_unit: native_steps`: positive amounts scroll up and negative
amounts down, using native increment/decrement actions when available. For a
scrollbar exposing a writable normalized value instead, each step changes that
value by 0.05, clamped to [0,1]. It is not a pixel or wheel-notch guarantee.

Screen Recording and Accessibility permissions must be granted to the process
hosting Saygo. Locked/inactive consoles, minimized/hidden windows, windows on
another desktop Space and ambiguous native window mappings are rejected. App
callbacks can still activate themselves, and user input can race with automation;
if the foreground app or pointer changes during an operation, Saygo reports an
uncertain result and does not restore focus, replay input, or claim success.
The existing desktop resource lock remains conservative and serializes Saygo
desktop tasks. It does not lock out a human or other applications.

Validation on an Intel Mac with macOS 12.7.6 and Python 3.14.8: the isolated native
test panel accepted a background button action, Chinese/emoji input and deletion,
and native scrolling, while frontmost PID, pointer and clipboard change count
remained unchanged. Separate CLI processes and an MCP stdio session also passed
connect, observe and observed-action checks against that panel (five live tests).
This does not certify arbitrary apps, other macOS releases,
minimized windows or other Spaces. Run the explicit GUI checks with:

```sh
SAYGO_TEST_MAC_BACKGROUND=1 python3 -m unittest discover -s tests/mac_demo -p test_background_live.py -v
```

The test creates and closes its own panel; it does not open user documents. Keep
the console unlocked and avoid moving the pointer during the input assertions.
Offline contracts are in `tests/control_demo/test_mac_background.py`.
