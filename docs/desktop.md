# Saygo Desktop (first development build)

Saygo Desktop uses **Qt 6 Widgets + PySide6 (Python)**. Qt is the UI framework;
Python is the implementation language. This directly reuses the existing control
service and durable InteractiveRuntime. It does not depend on pyenv. Packaged users
do not install Python, pip or a programming Agent.

## Technology choice

| Choice | Fit for this repository |
|---|---|
| Qt + PySide6 | Native desktop widgets; one Python codebase; direct Runtime reuse. Chosen for the first version. |
| Tauri + web UI | Attractive web UI ecosystem; also needs Rust and a separately packaged Python sidecar for each architecture. |
| pywebview | Python-friendly HTML shell; adds web assets and platform-specific webview deployment to maintain. |

References: [Qt for Python](https://doc.qt.io/qtforpython-6),
[Tauri sidecars](https://v2.tauri.app/develop/sidecar/),
[pywebview packaging](https://pywebview.flowrl.com/guide/freezing).

## Start from source (developers)

```sh
python3 -m pip install '.[desktop]'
saygo-desktop
```

No pyenv is required. An isolated environment is optional for development. On
Windows add the `windows` extra; on macOS add `mac` to install input drivers. Mobile
dependencies remain optional. WSL can run the Linux GUI through WSLg; the intended
Windows distribution is a native Windows build.

## User flow

The main window is a conversation: recent tasks live in the left sidebar,
messages and progress appear in the center, and the composer stays at the bottom.
Open model settings and device connections from the sidebar. Enter sends a task;
Shift+Enter inserts a newline. During human handoff, reply in the composer to
continue the current task. Uncertain actions still require explicit review.
The latest device screenshot is available through the observation button.

1. Enter an OpenAI-compatible API base URL, vision model name and API Key. Test
   image input. HTTP is allowed only for local loopback endpoints; remote providers
   must use HTTPS. The client never automatically retries a model request.
2. Choose session-only credentials or the OS credential store. Keys never enter
   `desktop.json`, task records or exported reports. If a secure keyring backend
   is unavailable, uncheck Remember and use a session-only key. Other provider
   settings live under `SAYGO_HOME_DIR` or `~/.saygo`.
3. Connect a named browser/desktop/mobile session. The browser bridge button
   registers the development extension identity and prepares extension files;
   load that directory in Chrome and click Connect. This replaces an existing
   registration for the same native-host name, so use it when updating both sides.
   Select a browser page by its title in the page selector before starting a task.
4. Select one or more sessions, describe a task, and run it. The general-task model
   loop uses screenshots, percent coordinates and the existing action validation,
   resource locks, durable dispatch intent and record export.
5. Pause for human control, resume with a note, or stop. Pause/stop waits for the
   current API/device call to return and blocks the next dispatch; it cannot undo
   input already sent. A model request has a 30-second timeout and a run is capped
   at 30 steps before handoff. An uncertain action stops automatic execution.
6. Reopen the app, load the task and re-observe. Resolve uncertain actions manually
   with evidence before continuing. No automatic action replay happens at startup.

Completion is a model judgement based on screenshots, recorded as agent evidence;
it is not an independent business-system assertion. Screenshots and task text are
sent to the configured model provider. The first build does not implement native
Anthropic/Gemini API formats (use an OpenAI-compatible endpoint), streaming chat,
automatic updates, signed installers or a graphical mobile toolchain wizard.

## Build for distribution

```sh
python3 -m pip install pyinstaller
python3 scripts/build_desktop.py
python3 scripts/package_desktop.py
```

Build on each target OS; do not rename a Linux executable to `.exe`. Outputs are
in `dist/desktop/SaygoDesktop` (macOS also produces an `.app`). Distribute the whole
folder, not just the executable. Qt, Python and extension assets are included.
Windows uses a windowed `SaygoDesktop.exe` and a separate `SaygoNativeHost.exe`
for Chrome native messaging over stdio. Keep both executables and `_internal`
together. Opening the desktop app does not open a console window.

`package_desktop.py` creates a verified DMG with an Applications shortcut on macOS,
a ZIP containing the executable and dependencies on Windows, and tar.gz on Linux.
Each archive has a SHA256 checksum. Output is in `dist/installers`.

The manual desktop workflow builds Windows/macOS/Linux artifacts. This does not
sign or notarize them. Before public distribution, include dependency license
notices, test fresh machines and package/sign per platform. Qt's license choices
and obligations still apply; see the official Qt documentation.

## Verification boundary

Linux/WSL: Qt offscreen startup, screenshot rendering, core pause/recovery tests
and local standalone packaging are checked during development.
Windows x64: a native Python 3.13 build passed ten desktop tests including chat
interaction tests. The frozen GUI passed visible Qt startup; PE headers confirm
a windowed GUI and a separate console host, which passed the native-messaging handshake. A
portable ZIP was produced. Clean-machine installation and live task execution
remain unverified. Windows users do not need WSL, Python or pyenv.
macOS: the DMG packaging script and CI workflow are prepared, but no macOS build
or runtime test has been performed yet. DMG support is not yet validated.
The app can run on Linux, but Linux local-window automation is not implemented by
the current Saygo drivers; browser extension and supported mobile paths remain
available. A live provider task needs a user-supplied model and connected test app.
