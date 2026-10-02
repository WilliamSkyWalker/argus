# Saygo 0.4.9 — Reliable browser scrolling and persistent connections

- Keep the browser bridge open after request timeouts, malformed or oversized responses, and handshake errors. Return errors without automatically replaying input; ignore late responses by request ID.
- Allow queries and screenshots while an earlier input is pending. Reject overlapping input until the underlying call finishes, and include the stalled operation in timeout diagnostics.
- Use complete mouse scroll gestures for viewport and targeted pane scrolling. This avoids the raw wheel acknowledgement stall observed during live browser testing.
- Protect the last browser tab from automated closing, and prevent stopping an extension session from terminating Chrome through stale process metadata.
- Preserve the installed extension ID during upgrades unless explicitly overridden.
- Account for long-press duration in extension, bridge and client timeouts.
- Teach external agents and built-in models to start around half a pane, retain overlap, reduce to 10–20% near a target, and adjust after observing the result. Expose browser scroll units in capabilities and observations; provide before/after scroll images to the desktop model.
- Enable configurable targeted scrolling through the built-in QA and desktop action paths.

## Upgrade

```sh
pipx upgrade saygo-agent-control --index-url https://pypi.org/simple --pip-args="--no-cache-dir"
saygo setup --client codex
```

Restart the agent client after setup. The attached versioned installer can also update managed installations.

Replace the contents of the currently loaded browser extension folder with the development extension ZIP. Manually reload Saygo Browser in chrome://extensions or edge://extensions, then click Connect local bridge. Keep the loaded folder. Update both runtime and extension to obtain all fixes.

The extension's release button remains the explicit disconnect control. Browser exit, extension reload or an external transport failure can still end a connection.

## Validation

Control, runtime, desktop and bridge tests cover request recovery, late responses, long-press budgets, extension ID preservation, scroll capabilities and model context. Extension tests cover handshake errors, pending input, last-tab protection, scroll direction and fractional amounts. Optional GUI and hardware tests are not claimed as executed.

Live Windows Chrome/WSL verification confirmed targeted list scrolling down and back up, successful command responses and an intact connection. This does not establish equivalent behavior on every browser or operating system.

Desktop binaries and Chrome Web Store publication are not included.
