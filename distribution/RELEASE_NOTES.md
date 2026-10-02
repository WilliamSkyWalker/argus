# Saygo 0.4.10 — Background screenshots and capture diagnostics

- Capture browser screenshots and viewport sizes without activating tabs, focusing windows, or restoring minimized windows. Explicit page selection retains its foreground behavior.
- Keep the existing `Page.captureScreenshot` interface, PNG surface capture and viewport clip parameters.
- Prevent overlapping viewport requests for the same page while an underlying Chrome command is pending. Other pages and diagnostic reads remain available.
- Discard expired capture results and stop subsequent capture steps after timeout. Release pending ownership when the underlying call completes, without disconnecting the extension or replaying input.
- Report tab activity, window state, focus, debugger attachment, and screenshot stage/timing through browser diagnostics. Preserve screenshot evidence across subsequent size reads.

## Upgrade

```sh
pipx upgrade saygo-agent-control --index-url https://pypi.org/simple --pip-args="--no-cache-dir"
saygo setup --client codex
```

Restart the agent client after setup. Replace the contents of the currently loaded extension folder with the development extension ZIP. Manually reload Saygo Browser in chrome://extensions or edge://extensions, then click Connect local bridge. Keep that folder. Update both runtime and extension for complete diagnostics.

## Validation and limits

Local checks passed: 13 Python bridge tests and all four JavaScript extension test files, including capture timeout, late-result, pending ownership and diagnostic regression cases.

Windows Chrome 154.0.8037.93 with the extension and WSL MCP client returned fresh screenshots for background, covered, minimized and inactive-tab states. After approximately 84 seconds without requests, the minimized test window still produced a fresh screenshot and the connection remained available. Background observations took approximately 1.4–1.6 seconds in this local sample. No alternate capture source, browser throttling flags, heartbeat or automatic reconnect was needed.

The previously reported intermittent screenshot timeout was not reproduced; this release does not claim to have established or eliminated its underlying cause. A Chrome command that never completes remains pending rather than being canceled by detaching the debugger. Lock-screen, suspend, long-term idle and other operating systems were not validated by these live checks.

Desktop binaries and Chrome Web Store publication are not included.
