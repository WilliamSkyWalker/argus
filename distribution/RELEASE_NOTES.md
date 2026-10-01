# Saygo 0.4.5 — macOS background control

- Add explicit macOS background sessions through CLI, MCP and the desktop connection dialog, with persisted process and window identity.
- Capture windows without activation; use native control actions, process-directed Unicode keys and native vertical scrolling without global-input fallback or clipboard changes.
- Reject unsupported controls, stale window bindings and unavailable windows. Capabilities and diagnostics describe the background mode and its limitations.
- Include the ApplicationServices binding in the `mac` extra.

Five live tests passed on Intel macOS 12.7.6 using an isolated native panel: button actions, Unicode input/delete, scrolling, CLI reconnect and MCP stdio. Foreground application, pointer and clipboard remained unchanged. Apple Silicon, newer macOS versions and arbitrary application workflows remain unverified. Windows must remain on the current desktop, non-minimized, with Screen Recording and Accessibility permissions granted.

## Install or upgrade from PyPI

```sh
python -m pip install --upgrade 'saygo-agent-control[mac,mcp]'
saygo setup --client codex
```

For existing pipx installations:

```sh
pipx upgrade saygo-agent-control
saygo setup --client codex
```

Restart the agent client after setup to load the updated managed runtime. Background control is opt-in with `saygo device connect --platform mac --app 'Example App' --background --session mac-bg`; see `docs/control.md` for supported actions and limitations.

This release contains CLI and Agent integration packages. Desktop installers and Chrome Web Store publication are not included.
