# Saygo 0.4.11 — Screenshot timeout guidance and diagnostics

- Explain when Chrome's screenshot interface does not respond in time. Keep the browser connected and ask the user to retry observation later, without repeating clicks or messages.
- Distinguish an outstanding capture from a completed, expired request in diagnostic guidance.
- Correlate screenshots with bridge request IDs, timestamps and bounded command timelines. Report aggregate CDP completion, failure and pending counts without retaining command payloads or images.
- Reject screenshots invalidated by navigation, tab closure or control release.
- Expand deterministic lifecycle, concurrent bridge, background screenshot and scroll regressions; require core and real-browser checks before publication.

## Upgrade

```sh
pipx upgrade saygo-agent-control --index-url https://pypi.org/simple --pip-args="--no-cache-dir"
saygo setup --client codex
```

Restart the agent client after setup. Replace the contents of the currently loaded extension folder with the development extension ZIP. Manually reload Saygo Browser in chrome://extensions or edge://extensions, then click Connect local bridge. Keep that folder.

## Known limits

This release adds diagnosis and recovery guidance; it does not fix the underlying intermittent Chrome screenshot stall. Local Linux minimized-window regressions still reproduce the stall and remain release-blocking checks. A timeout does not cancel Chrome's outstanding command. Its late image is discarded, and no automatic disconnect, reconnect or input replay is used. Multi-hour endurance testing is excluded.

Desktop binaries and Chrome Web Store publication are not included.
