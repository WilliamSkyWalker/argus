# Architecture and module layout

`argus/` is organized by responsibility. Root modules are the stable CLI entry
point (`cli.py`), shared configuration (`config.py`), logging (`logger.py`) and
a compatibility command for the browser bridge (`browser_bridge.py`). Package
initializers do not eagerly import engines or optional host dependencies.

| Package | Responsibility |
| --- | --- |
| `qa/` | Gherkin parsing, scenario planning, visual decision loop, verdict validation, failure analysis and reports |
| `vision/` | Image grids, element grounding and stable-frame sampling |
| `devices/` | Unified device management, discovery, emulator/toolchain provisioning and Windows mobile worker/relay |
| `platforms/` | Device action and screenshot adapters: Appium, browsers, desktop and experimental RDP; persistent session attachment |
| `runtime/` | Durable multi-resource workflows, locks, human handoff and read-only SQLite resources |
| `integrations/` | Figma API/MCP adapters and the browser extension native messaging bridge |
| `skills/` | Optional screenshot analysis pipeline components |
| `probes/` | Nonvisual assertion plugins and subprocess protocol |
| `mcp/` | MCP server entry point and external MCP client |
| `drive/` | Agent-driven execution journal rendering |

## Execution paths

- QA: `cli` → `qa.gherkin` → `qa.planner` → `qa.agent`/`qa.brain` → `platforms`.
  The loop uses `vision`, screenshot `skills` and optional `probes`; `qa.report`
  renders results and `qa.healer` analyzes failures.
- Device control: `cli` → `devices.control` → `platforms.device_session` → native
  adapter. `devices.mobile` owns discovery and provisioning, with `mobile_host`
  choosing local or Windows execution.
- Workflow: `runtime.engine` → `runtime.resources` → platform adapters or SQLite.
  It is independent of the QA scenario loop and retains its durable action and
  human-handoff semantics.
- Browser extension: `platforms.browser_extension` →
  `integrations.browser_bridge` → native messaging extension.

Shared desktop actions live in `platforms.desktop`; native desktop subclasses
supply window management, screenshots, coordinate conversion and clipboard
access. `platforms.device_session.release_controller` owns temporary controller
cleanup. `devices.mobile.provision_runtime` owns shared installation orchestration.

## Entry points and deployment

Existing commands remain available: `python -m argus.cli`,
`python -m argus.mcp.server`, `python -m argus.drive.render`, and
`python -m argus.browser_bridge`. The browser bridge implementation also runs as
`python -m argus.integrations.browser_bridge` or directly as a standalone script.
Internal Python imports use the new package paths; they are not compatibility
aliases at the package root.

The Windows mobile bundle explicitly includes only `argus/__init__.py`, logging,
the `devices` initializer, mobile provisioning, toolchain code, its worker and the
Node console-suppression preload. It excludes repository configuration and
credentials. Its isolated subprocess regression test uses no installed packages.
The worker adds the copied package root to its import path; the relay launches
from the repository root. Keep these paths in sync when changing package layout.

## Regression checks

Run unittest discovery for `tests/control_demo`, `tests/mobile_demo`,
`tests/runtime_demo`, `tests/browser_demo` and `tests/extension_demo`.
Real browser tests require `ARGUS_TEST_CHROME`; mocked tests do not establish
real desktop or mobile device compatibility.
