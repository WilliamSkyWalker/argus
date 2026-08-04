# argus-runner

Brings [argus](https://github.com/WilliamSkyWalker/argus) (a vision-driven AI QA agent) into Claude Code: **feed it cases → it watches the screen and executes → it self-judges pass/fail → you get an HTML report with screenshots**.

Two ways to run:
- **`argus run` (MCP tools `run_target` / `run_case`)** — argus's own agent loop runs batch regression, with multi-device parallelism, sharding, and background runs you can poll. Decisions come from the LLM you configure (`LLM_API_KEY`).
- **`/argus-drive` (skill)** — **Claude is the brain**, the `argus device` CLI is the hands and eyes, and each conversation turn is one iteration of the loop. Best for tuning prompts, debugging a single case, or small batches; supports resume and emits the same HTML report.

## Install
```
/plugin marketplace add WilliamSkyWalker/argus
/plugin install argus-runner@argus
```

Then set up argus itself, the system dependencies, and an LLM key (a plugin cannot install these):
```bash
git clone https://github.com/WilliamSkyWalker/argus.git
export ARGUS_HOME=/absolute/path/to/argus
pip3 install openai Pillow uiautomator2 selenium Appium-Python-Client mcp
npm i -g appium@3 && appium driver install uiautomator2      # Node LTS; add xcuitest@latest for iOS
cd "$ARGUS_HOME" && python3 -m argus.cli init                # writes .env — fill in LLM_API_KEY
```
Run **`/argus-doctor`** in Claude Code to settle everything that's missing in one pass (including the LLM key and the `tests/` directory).

## Use
```
What test targets do I have?                          → list_targets / list_cases
Run the P0 cases of <target> on these two devices     → run_target (parallel + APK install)
What's the status of that run?                        → get_run_status / get_report
/argus-drive tests/<target>/cases/foo.feature TC-001  → Claude as the brain, single-case debug
```

## Case format (essentials)
- **`.feature` (Gherkin, preferred)**: Feature/Background/Scenario(Outline) + Examples. Tags drive priority and platform: `@P0/@P1/@P2`, `@auto/@partial/@manual`, `@android/@ios/@browser`, `@TC-XXX`, `@reset:pm_clear|relaunch|none`.
- **`.md` (TDD style)**: `### TC-XXX` blocks with `- **Priority/Platform/Steps**`.
- Cases should be **self-contained** (put the precondition state in Background); hints describe direction, never coordinates; split Then into individually verifiable bullets. **Assertions that cannot be verified visually** (analytics events, backend state, system clock, notification shade, cross-app deeplinks) are deliberately failed rather than assumed — rewrite them or tag them `@manual`.

## What's inside
- **skill `argus-drive`** — the Claude-as-brain driving loop (state reuse across scenarios, per-feature journal, resume, report rendering)
- **command `/argus-doctor`** — full dependency check
- **MCP server `argus-runner`** — `list_targets` `list_cases` `run_target` `run_case` `get_run_status` `get_report` `cancel_run` `list_runs` plus every device primitive (`device_screenshot/tap/swipe/input/type_send/key/launch`, `list_devices`, `install_apk`, `adb_reconnect`, `setup_simulator`)

## Relationship to argus-device
This plugin's MCP server is a **superset** of `argus-device`. If you only want Claude to operate a device by hand (no suites, no LLM key), install `argus-device` instead — installing both gives you duplicate tools.

> Note: the `argus-drive` skill body is currently written in Chinese (inherited from the upstream repo). It is model-facing instruction text and works as-is; an English version is planned.
