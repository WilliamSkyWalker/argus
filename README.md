# Argus

[MIT License](LICENSE) · [中文说明](#中文说明) · [Agent development guide](agent.md)

**Visual control of phones, browser pages and desktop windows, with persistent sessions and task records.**

Argus gives external programming agents a shared CLI/MCP operation layer: observe a screen, choose an action, execute it, and inspect the result. It also provides a standalone Qt desktop application where users configure their own vision-model API, plus a BDD QA runner for regression tests.

## Choose how to use it

| User | Entry point | Model configuration |
|---|---|---|
| Claude Code, Codex, Qoder or QoderCN CLI user | Managed Agent integration: MCP + shared operation Skill | No Argus model API key; the external Agent makes decisions |
| Desktop application user | Argus Desktop (Qt 6 / PySide6) | Configure an OpenAI-compatible vision API URL, model and API key in the app |
| Script or CI user | `argus device`, `argus task`, `argus workflow`, `argus run` | Direct control needs no key; the built-in QA loop needs model credentials |

Argus is currently a **development/beta distribution**. Installable source bundles and native packaging scripts exist; this does not mean the packages are published on PyPI or the Chrome Web Store. See the [distribution guide](distribution/README.md) for release artifacts, updates and uninstall.

## Programming Agent setup

From a checkout, with Python 3.10+ and an installed client:

```sh
python3 scripts/install_agent_plugin.py
```

The installer detects supported clients, prepares an isolated runtime, installs their integration and the shared Skill, and sets up the Chrome/Edge native messaging host. It does not require pyenv. Select clients with `--client claude`, `codex`, `qoder`, `qodercn`, `both` (Claude + Codex), or `all`. Add `--mobile` for mobile dependencies. On native Windows use `py -3` in place of `python3`.

Download the installer and source archive from [GitHub Releases](https://github.com/WilliamSkyWalker/argus/releases/tag/v0.4.0) to install without Git:

```sh
python3 install-argus-0.4.0.py --archive argus-0.4.0.zip
```

For browser control, load the installer-provided extension directory through **Load unpacked** at `chrome://extensions` (or Edge's extension page), then click **Connect local bridge** in its popup. Restart the Agent client and describe a task, for example:

> List the connected sessions, connect the test browser page as `mail`, and show me its current screen before making changes.

**MCP browser control uses the extension backend. Playwright is excluded from MCP**, including existing Playwright sessions and tasks using them. Playwright remains available through CLI/Runtime for managed test browsers. The extension works with existing website tabs and their login state; it does not extract DOM content for visual decisions.

The installer configures Argus-scoped permissions for Claude and Qoder/QoderCN. Codex approval setup and client-policy limits are documented in the [distribution guide](distribution/README.md). Qoder integration covers the CLI; IDE integration is not claimed verified.

For direct package installation during development:

```sh
python3 -m pip install -e '.[mcp]'
argus --help
argus-mcp --profile device
```

Install only the needed extras: `mobile`, `windows`, `mac`, `desktop`, `browser` (CLI Playwright), `selenium`, or `qa`. A browser-extension-only MCP setup does not need the mobile toolchain. See the [operation guide](docs/agent-control.md) and [plugin guide](plugins/argus-device/README.md).

## Desktop application

**Under development and testing / 开发测试中。**

Argus Desktop centers on a conversation, with task history in the sidebar and a message composer at the bottom. Users can configure a model, connect named sessions, run a task, pause for human input, reply to continue, recover a task and export evidence.

- **Windows:** a native x64 portable ZIP has been built. Extract the entire folder and run `ArgusDesktop.exe`; keep `ArgusNativeHost.exe` and `_internal` alongside it. No WSL, Python or pyenv installation is needed. The main application opens without a console; the separate native host handles browser messaging.
- **macOS:** `.app` and DMG packaging scripts and a macOS CI job are prepared. No macOS build/runtime validation has been completed yet.
- **Linux:** the standalone GUI has passed local startup checks. Browser/mobile operation is available through the corresponding backends; local Linux desktop-window automation is not implemented.

The application accepts an OpenAI-compatible vision endpoint. Keys can stay in memory for the session or use a supported OS credential store; they are not written to `desktop.json`. Screenshots and task text are sent to the configured provider.

Developers can launch from source:

```sh
python3 -m pip install -e '.[desktop]'
argus-desktop
```

Add `windows` or `mac` extras for native desktop input. Native packaging runs **on the target OS**:

```sh
python3 -m pip install pyinstaller
python3 scripts/build_desktop.py
python3 scripts/package_desktop.py
```

Output: Windows ZIP, macOS DMG or Linux tar.gz under `dist/installers`, each with a SHA256 checksum. The manual [desktop workflow](.github/workflows/desktop.yml) builds CI artifacts. These are development builds without release signing or notarization. See [desktop usage and validation](docs/desktop.md).

## Observe, act, verify

CLI and MCP share named session state under `ARGUS_HOME_DIR` (default `~/.argus`). Clients must use the same state directory and a backend available to both entrances to operate the same target.

```sh
# Existing browser: install and connect the extension first.
argus device connect --platform browser --backend extension --session mail

# Android device/emulator; obtain its ID with device list.
argus device list --platform android
argus device connect --platform android --device DEVICE_ID --session phone

# Windows window-title substring; use --platform mac for a macOS app.
argus device connect --platform windows --app 'Example App' --session admin

argus device capabilities --session mail
argus doctor --session mail
argus device screenshot --session mail

# Replace OBSERVATION_ID with the returned ID, and choose coordinates from that image.
argus device act '{"type":"tap","x":50,"y":40,"coordinate_space":"percent"}' \
  --session mail --observation-id OBSERVATION_ID --observe-after
argus device wait --session mail --mode stable --timeout 5
```

Observations include identity, target, dimensions and coordinate mappings. Crop observations retain the mapping to the original screen. MCP `device_observe` and action results with observations return image content; CLI returns image paths and metadata. Query capabilities before using platform-dependent actions.

**Dispatched input or a stable frame does not establish business success.** Inspect the resulting screen. Prefer visible buttons and menus over keyboard shortcuts. Unsupported actions fail explicitly; Windows background mode must not silently fall back to global keyboard/mouse input. See [control details](docs/control.md).

## Tasks, handoff and recovery

Interactive tasks reuse Runtime while an Agent decides one step at a time. Explicit JSON workflows use `argus workflow`; they do not require a model planner.

```sh
argus task create '{"phone":"phone","mail":"mail","admin":"admin"}'
argus task observe TASK_ID --resource phone
argus task submit TASK_ID --resource phone --request-id focus-registration \
  --observation-id OBSERVATION_ID \
  --action '{"type":"tap","x_pct":50,"y_pct":40}' --note 'Focus the registration form'
argus task handoff TASK_ID --instructions 'Please sign into the test mailbox'
argus task resume TASK_ID --note 'User returned control after login'
argus task recover TASK_ID
argus task timeline TASK_ID
argus task finish TASK_ID --note 'Verified the final screen'
argus task export TASK_ID --out evidence.zip
```

Use the IDs returned by create/observe. MCP exposes the same task commands through `agent_task`. Project aliases can be saved with `argus resources bind NAME SESSION`.

Runtime records dispatch intent before input, deduplicates request IDs, and marks interrupted actions with unknown results as `needs_review`; it never blindly replays them. Handoff blocks automatic input, and resume obtains fresh observations. Tasks retain resource ownership until finished or cancelled; use the owning task interface while a session is bound. Local desktop windows share an input lock.

Execution facts, errors, before/after captures and Agent notes are recorded separately. Evidence exports can contain application content and entered text. See [interactive task semantics](docs/agent-control.md), [workflow format](docs/runtime.md) and [workflow examples](examples/workflows).

## Platforms and validation limits

| Target | Implementation / current boundary |
|---|---|
| Android | Appium + UiAutomator2; discovery/provisioning uses adb. Windows-host emulator installation and basic visual actions from WSL have been exercised. |
| iOS | Appium + XCUITest; local setup requires macOS and full Xcode. New provisioning paths remain mock-tested. |
| Chrome / Edge | Extension + native messaging for MCP and the desktop GUI; store installation and fresh-machine setup still require acceptance tests. |
| Managed test browser | Playwright/CDP through CLI/Runtime; Selenium remains available for QA. Playwright is rejected by MCP. |
| Windows desktop | Native driver or PowerShell/Win32 runner; WSL is optional. Experimental background control depends on application controls and rejects unsupported shortcuts. |
| macOS desktop | Window capture and foreground input; requires Screen Recording and Accessibility permissions. Native packaging and full task execution remain unverified. |
| Remote desktop | Experimental RDP prototype; not a stable supported deployment path. |

Windows native desktop tests, frozen EXE startup and native-host handshake passed. Linux Qt startup and packaging passed. These checks do **not** establish clean-machine installation, a live model-driven task or the full phone registration → email activation → desktop confirmation acceptance scenario. Detailed evidence boundaries: [desktop](docs/desktop.md), [Agent control](docs/agent-control.md), [mobile](docs/mobile.md), [distribution](distribution/README.md).

`doctor` checks connection, capture and image readability. Input remains untested unless an explicit `--probe-action` is supplied on a harmless target; its result still needs visual inspection.

## Configuration

External Agent control needs no Argus LLM API key. The standalone GUI has its own model settings. Optional CLI QA settings load in this order:

1. Built-in defaults.
2. `$ARGUS_HOME_DIR/config.env` (default `~/.argus/config.env`).
3. Working-project `.env`, or the explicit file selected by `ARGUS_CONFIG_FILE`.
4. Process environment variables.

`argus init` creates a project template; `argus init --user` creates a user template. Existing files are preserved. Installed package directories are not searched for `.env`; private configuration is never bundled in releases. The Agent installer supports `--config-file /absolute/path/to/private.env` to retain a file reference without copying its contents.

## BDD QA and other tools

The existing QA engine remains available for `.feature` (Gherkin) regression cases, visual reports, model tiering and optional element location:

```sh
python3 -m pip install -e '.[qa,mobile]'
argus init
# Configure a vision model and target in the private .env file before running.
argus run tests/my-app/login.feature --report
```

```gherkin
# argus-platform: android
# argus-package: com.example.app
Feature: Login
  @auto @android
  Scenario: Show the login form
    Given the app is launched
    When the user taps "Log in"
    Then the email and password fields are visible
```

QA verdicts are model judgements with recorded evidence, not independent proof. Use [probe plugins](docs/probes.md) for non-visual facts such as backend writes. Figma test generation and visual review remain available through `argus figma`. The existing [`/argus-drive` Skill](.claude/skills/argus-drive/SKILL.md) provides a separate QA-oriented Agent workflow; general cross-device tasks should use the shared operation Skill and Runtime records.

For QA execution guards, model configuration, case conventions and module responsibilities, see [agent.md](agent.md), [`.env.example`](.env.example) and [architecture](docs/architecture.md).

## 中文说明

Argus 为外部编程 Agent 提供手机、浏览器和桌面窗口的视觉操作能力：**观察 → 操作 → 新观察**，并保存跨端任务进度、执行事实和截图。

- **命令行用户**：安装 Claude Code / Codex / Qoder / QoderCN 的集成，由现有 Agent 决策，无需给 Argus 配模型 Key。从源码执行 `python3 scripts/install_agent_plugin.py`；版本化安装器可配合源码 ZIP 使用，无需 Git。安装后重启客户端。
- **窗口桌面用户**：使用 Qt 桌面版，在界面配置自己的视觉模型 API、模型名和 Key。Windows 原生便携包无需 WSL、Python 或 pyenv。macOS DMG 的脚本和 CI 已就绪，但尚未完成 macOS 构建与实测。
- **浏览器**：MCP 和桌面版使用 Chrome/Edge 扩展，保留现有页面和登录状态；MCP 不支持 Playwright。扩展暂通过开发者模式加载，未声称已上架商店。
- **恢复与接管**：CLI/MCP 共用命名会话；`argus task` 保存操作记录、支持人工接管和中断恢复。结果不确定的动作需核对，不自动重放。点击派发成功或画面稳定都不等于业务完成。
- **测试边界**：Windows EXE 已通过启动和桥接握手验证，完整跨端业务验收与干净机器安装仍需实测；不要将模拟测试当作真机兼容性证明。

安装与更新见[分发说明](distribution/README.md)，窗口版见[桌面指南](docs/desktop.md)，会话、操作和恢复见[Agent 操作指南](docs/agent-control.md)，开发约束见[通用 agent.md](agent.md)。

## License

Argus is licensed under [MIT](LICENSE). Third-party dependencies retain their respective licenses.

Argus 采用 MIT 许可证；第三方依赖遵循各自许可证。
