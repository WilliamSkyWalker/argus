# Saygo 0.4.12 — Background tab scrolling and automated store submission

- Scroll inactive tabs with mouse-wheel input and temporary logical focus emulation, without activating the tab or browser window.
- Reset focus emulation after completion, failure or timeout. Retain the pending-input lock and reject stale page/control requests without replaying input or disconnecting the extension.
- Add 12 rounds of tab-switch, screenshot, click and targeted-scroll integration coverage, plus short idle capture in an inactive tab.
- Keep foreground, background, covered-window and inactive-tab release checks. Minimized windows and multi-hour endurance runs are outside the requested acceptance scope.
- Retain screenshot timeout guidance and bounded diagnostics from 0.4.11, and fix report rendering compatibility with Python 3.11.
- Submit Chrome Web Store packages automatically from release tags after regression checks. Google publishes the extension after review approval.

## Upgrade

```sh
pipx upgrade saygo-agent-control --index-url https://pypi.org/simple --pip-args="--no-cache-dir"
saygo setup --client codex
```

Restart the agent client after setup. For unpacked extensions, replace the contents of the currently loaded extension folder with the development extension ZIP. Manually reload Saygo Browser in chrome://extensions or edge://extensions, then click Connect local bridge. Keep that folder. Store installations receive the update after Google approves and publishes it.

## Known limits

The original Chrome screenshot API is retained. These changes do not establish a fix for every intermittent Chrome screenshot stall, including the reported multi-hour Windows incident. A timeout does not cancel Chrome's outstanding command; its late image is discarded. No automatic disconnect, reconnect or input replay is used. Minimized windows are excluded from acceptance, and multi-hour endurance testing remains excluded. Full Windows background-tab acceptance is not yet established.

Chrome Web Store submission is distinct from approval; check the release workflow for the submitted review state. Desktop binaries are not part of this release workflow.
