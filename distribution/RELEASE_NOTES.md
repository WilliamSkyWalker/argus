# Saygo 0.4.8 — Browser pointer actions

- Add hover, double-click, right-click and configurable long-press actions to the connected browser extension backend.
- Check mouse action coordinates against the active browser viewport before dispatch.
- Reload the browser extension after updating the runtime files.

## Previous release: Saygo 0.4.7 — Scroll the intended browser pane

- Add `scroll_at` to the browser extension backend and the shared CLI/MCP action interface.
- Target a viewport coordinate or an observation-mapped percentage/image/crop coordinate. Positive amounts scroll up; negative amounts scroll down; one unit requests 100 CSS pixels.
- Validate wheel amounts and viewport bounds before sending input. Existing centered scrolling and mouse dragging keep their behavior.

## Upgrade

```sh
pipx upgrade saygo-agent-control
saygo setup --client codex
```

The attached versioned installer can also update a managed installation. Restart the agent client after setup.

This feature changes browser extension code: replace the loaded extension files with the attached development extension ZIP, preserving the folder, then Reload Saygo Browser from chrome://extensions or edge://extensions and click Connect local bridge. Keep the loaded directory. Both the runtime and extension must be updated to use scroll_at.

Choose a point inside the intended pane, then call device_act with a fresh observation ID and an action such as {"type":"scroll_at","x":35,"y":60,"coordinate_space":"percent","amount":-3}. Observe afterwards to verify movement. At a pane boundary, the browser may scroll an ancestor.

Validation covers coordinate conversion, capability exposure, both scroll directions, fractional amounts, and rejection of invalid input. Live verification of the BOSS page is pending extension reload.

Desktop installers and Chrome Web Store publication are not included.
