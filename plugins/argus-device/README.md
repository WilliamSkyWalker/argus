# argus-device

Visual control for Android/iOS, browser pages and desktop windows. An external programming agent decides each step; Argus provides shared CLI/MCP sessions, observations, actions, human handoff and durable task records. No LLM API key or test suite is required.

## Install

Install the plugin in Claude Code:

```
/plugin marketplace add WilliamSkyWalker/argus
/plugin install argus-device@argus-plugins
```

Install Argus into the Python environment used by the plugin. For this source version:

```bash
pip install '/path/to/argus[browser,mcp]'
python -m playwright install chromium
```

You can also install a built `argus_agent_control` wheel with the same extras. This source change does not publish a PyPI release. Only add `mobile` when using Android/iOS, `selenium` for that browser backend, and `mac` or `windows` for native desktop dependencies. WSL Windows control uses the bundled PowerShell runner.

An installed package supplies `argus` and `argus-mcp`; `ARGUS_HOME` is unnecessary. A source checkout without installation can still use `ARGUS_HOME`. Keep the agent's working directory at the user's project.

Mobile users additionally prepare the selected device toolchain with `argus device install --help`. Browser and desktop users do not need Appium. Run `/argus-device:doctor --session NAME` for an existing connection, or `--platform browser|android|ios|desktop` for dependency checks.

## Operate

Use the bundled [device skill](skills/device/SKILL.md) for the common protocol. Start by explicitly connecting a named session and querying its capabilities. Observe the image, decide one action, and inspect its result. Argus handles percent/image/crop coordinate conversion; a dispatched action is not a verified business result.

MCP exposes `device_sessions`, `device_connect`, `device_observe`, `device_act`, `device_command`, `device_handoff`, `device_resume`, and `agent_task`. Compatible `device_screenshot`, `device_tap`, `device_swipe`, `device_input`, `device_type_send`, `device_key` and `device_launch` names remain available. Their `serial` argument refers to a saved session across all platforms.

For cross-resource work, create an interactive task. It owns its resources until completion/cancellation and saves action intents before input. Request IDs prevent replay after lost responses. Recover after an agent restart; reconcile `needs_review` using observed evidence. Manual login uses handoff/resume. Task timeline and evidence export keep execution facts separate from agent notes.

The CLI offers the same protocol through `argus device` and `argus task`. For other MCP clients, launch `argus-mcp --profile device`. Autonomous QA remains available separately through `argus run` with its own model configuration.
