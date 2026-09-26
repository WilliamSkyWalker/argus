# Unified device interface

Use `argus device` for mobile, desktop and browser control. Each connected target
has a named session; shared actions use `--session` (`--serial` remains an alias).
Standalone `argus mobile`, `argus devices`, `argus setup`, and
`argus.browser_bridge bind` have been removed. Use `device list`, `device connect`,
`device install`, and `device boot` instead. `device list` always emits JSON.
`device start/stop` remain within this unified command group. The browser bridge's
`install/host` commands remain for native-host registration and Chrome startup.

```bash
python3 -m argus.cli device list
python3 -m argus.cli device sessions

# Mobile: physical device or booted simulator
python3 -m argus.cli device connect --platform android --device DEVICE_ID --session phone
python3 -m argus.cli device connect --platform ios --device UDID --session iphone

# Existing browser (install/connect extension first)
python3 -m argus.cli device connect --platform browser --backend extension \
  --bridge-directory /path/to/browser-bridge --session web
# Or create/reconnect an Argus-managed Chromium session
python3 -m argus.cli device connect --platform browser --backend playwright --session test-web

# Desktop: Windows window-title substring / macOS application name
python3 -m argus.cli device connect --platform desktop --app "Example App" --session desktop

# Same commands across all connected targets
python3 -m argus.cli device screenshot --session phone --out screen.png
python3 -m argus.cli device tap 100 200 --session phone
python3 -m argus.cli device input "Hello" --session web
python3 -m argus.cli device key enter --session desktop
python3 -m argus.cli device scroll down --session web
python3 -m argus.cli device open https://example.com --session web

# Simulator provisioning uses the same entry point
python3 -m argus.cli device install --platform android --boot --connect
python3 -m argus.cli device install --platform android --host auto --dry-run
python3 -m argus.cli device install --platform ios --boot --connect
python3 -m argus.cli device boot --platform android Argus

# Browser-specific actions
python3 -m argus.cli device pages --session web
python3 -m argus.cli device new-page https://example.com --session web
python3 -m argus.cli device select-page PAGE_ID --session web
python3 -m argus.cli device close-page PAGE_ID --session web

python3 -m argus.cli device disconnect --session web
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
