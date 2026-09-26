# argus-device

Visual control for Android/iOS, browser pages and desktop windows. An external programming agent decides each step; Argus provides shared CLI/MCP sessions, observations, actions, human handoff and durable task records. No LLM API key or test suite is required.

## Install for Claude Code or Codex

From this checkout, run one installer in WSL/Linux or macOS (Python 3.10+ and the selected client CLI must already be available):

```bash
python3 scripts/install_agent_plugin.py --client both
# Or --client claude / --client codex
```

The installer creates an isolated Python environment, installs MCP dependencies, and registers a native `argus-device@argus-managed` plugin using each client's plugin CLI. The plugin includes the shared skill and an MCP server with an absolute interpreter path. It works from ordinary project directories without `ARGUS_HOME`, `PYTHONPATH`, manual MCP configuration, or a prompt asking the agent to read a file. Nothing is downloaded during MCP startup.

Start a new client session after installation and say: **“Use Argus to inspect my connected devices, then help me operate a test application.”** The agent discovers sessions, connects the requested target and follows the bundled observation/action protocol. Client permission prompts still apply.

MCP does not support Playwright. Browser tasks use the extension backend; saved Playwright sessions are listed separately as unavailable through MCP. Use `--install-browser` only to add Playwright/Chromium for separate CLI work, or `--mobile` to add mobile Python dependencies. Android/iOS toolchains, application login and OS input permissions remain platform setup requirements. Native Windows/macOS installation is not yet live-verified; WSL installation and both clients' plugin ingestion are tested. No model API key is required by Argus.

The installer also runs as a standalone downloaded Python file: outside a checkout it downloads the repository's `main` archive without requiring Git. This is a source-channel installer, not a signed binary or a published package-index release. Its remote path becomes available only after this change is published.

Managed files live under `~/.local/share/argus/agent-plugin`. Re-run the installer after updating the source to install a new runtime and refresh the plugin cache. Existing runtimes are retained for running clients; session/task data stays under `~/.argus`. `--prepare-only` prepares files without registering clients. `--root PATH` selects another managed installation directory.

Do not enable an older `argus-device@argus-plugins` installation or a manually configured Argus MCP server alongside the managed plugin: use one integration per client to avoid duplicate tools. The installer does not remove unrelated or existing client configurations.

The older Claude marketplace installation still exists for manually managed Python environments:

```text
/plugin marketplace add WilliamSkyWalker/argus
/plugin install argus-device@argus-plugins
```

That route alone does not provision Python dependencies. Prefer the managed installer for the complete setup.

## Operate

Use the bundled [device skill](skills/device/SKILL.md) for the common protocol. Start by explicitly connecting a named session and querying its capabilities. Observe the image, decide one action, and inspect its result. Argus handles percent/image/crop coordinate conversion; a dispatched action is not a verified business result.

MCP exposes `device_sessions`, `device_connect`, `device_observe`, `device_act`, `device_command`, `device_handoff`, `device_resume`, and `agent_task`. Compatible `device_screenshot`, `device_tap`, `device_swipe`, `device_input`, `device_type_send`, `device_key` and `device_launch` names remain available. Their `serial` argument refers to a saved session across all platforms.

For cross-resource work, create an interactive task. It owns its resources until completion/cancellation and saves action intents before input. Request IDs prevent replay after lost responses. Recover after an agent restart; reconcile `needs_review` using observed evidence. Manual login uses handoff/resume. Task timeline and evidence export keep execution facts separate from agent notes.

The CLI offers the same protocol through `argus device` and `argus task`. For other MCP clients, launch `argus-mcp --profile device`. Autonomous QA remains available separately through `argus run` with its own model configuration.
