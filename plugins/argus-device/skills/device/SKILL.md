---
name: device
description: Operate connected phones, browser pages and desktop windows through Argus visual sessions. Use for UI tasks, reproducing bugs and cross-device workflows driven by an external programming agent.
---

# Visual operation with Argus

Use the same named session in CLI and MCP. `serial` in legacy tools is a session alias, not necessarily a hardware serial. Discover saved bindings with `device_sessions` or `argus device sessions`; connect explicitly with `device_connect` or `argus device connect`. Never replace an expired session implicitly.

Query `device_command(command="capabilities", session=...)` or `argus device capabilities --session ...` before relying on an optional action. Capabilities describe the backend; they do not prove the current application accepted input.

Prefer visible buttons, menus and controls over keyboard shortcuts, especially in background mode. For example, save through File → Save when available. Use a shortcut only when the UI route is unavailable or clearly inefficient and the current backend and mode explicitly support that specific shortcut; support for `press_key` alone is not sufficient. If a shortcut fails, re-observe and look for a UI route instead of blindly retrying or switching to foreground mode without user authorization. Verify the result through a new observation; dispatched input does not establish success.

Observe → decide one action → observe again:

- MCP: `device_observe(session)` returns image content and metadata. `device_act(session, action, observation_id, observe_after=true)` executes a step with a fresh screenshot.
- CLI: `argus device screenshot --session NAME`, read the returned image, then `argus device act '{"type":"tap","x":50,"y":40,"coordinate_space":"percent"}' --session NAME --observation-id ID --observe-after`.
- Coordinates may be `screen`, `percent` (0–100), `image`, or `crop`. Image/crop coordinates require the source observation ID. Argus performs the conversion; do not manually multiply by screenshot scale. `screenshot --crop LEFT TOP RIGHT BOTTOM` / `device_observe(crop=[...])` also returns a 2× crop with its mapping.
- A changed target or expired observation requires a fresh observation and a new decision. A stable frame or `dispatched: true` does not verify business success. Inspect the returned result. `wait` supports `stable` / `change` with a timeout, without claiming task completion.

For work spanning resources or agent restarts, use `agent_task` or `argus task`:

1. Create with resource-to-session bindings, e.g. `{"phone":"test-phone","mail":"test-mail","admin":"test-admin"}`. Save the returned task ID.
2. `observe` a resource; read its screenshot. `submit` one action with that observation ID and a unique `request_id`. Optional `note` records the agent's reasoning separately from execution facts.
3. Reuse the same request ID only to retrieve a lost response to identical input. It never dispatches twice. After a restart, inspect `status`, `events` and `recover`.
4. `needs_review` means an input may already have taken effect. Observe the actual result and use `resolve` with evidence and `completed` / `not_executed`. Even `not_executed` requires a new observation and explicit new submission; do not blindly replay.
5. For manual login or other human work, use task `handoff` with instructions. After the user returns control, `resume` with a note; Argus re-observes. Verify the resulting state yourself.
6. A task owns its resources while active, including during handoff and review. Use its task interface; ordinary device commands and other tasks will report busy. `task list` finds unfinished tasks; explicit `cancel` with a note releases an abandoned task without erasing uncertain actions.
7. `finish` with evidence after verifying the requested result. `timeline` gives a concise history; `events` gives the complete execution facts; `export` creates a ZIP with task state, events and screenshots.

A single session also supports `device_handoff` / `device_resume` and `argus device handoff/resume`. Automatic input stays blocked during handoff. An unresolved crash must be checked before retrying a non-idempotent action.

If a screenshot is unavailable, run `argus doctor --session NAME`. The doctor does not send input. On mobile, a locked or sleeping screen may be black; request manual unlock when needed. Operate within the user's requested task and existing authorization.

MCP and CLI expose the same service; no LLM API key is needed. The installable command is `argus`; a checkout can use `python -m argus.cli` with the same arguments. Select the MCP `device` profile for external agent work. Mobile-only installation/boot commands remain available when needed.
