# Saygo

**你说，它做。**

让 AI 操作你的电脑、浏览器和手机。Saygo connects programming agents to real devices through visual observation, actions, and verified results.

**官网：[saygo.work](https://saygo.work/)** · [Source repository](https://github.com/WilliamSkyWalker/saygo)

[MIT License](LICENSE) · [中文说明](#中文说明) · [Agent development guide](agent.md)

**Visual control of phones, browser pages and desktop windows, with persistent sessions and task records.**

Saygo gives external programming agents a shared CLI/MCP operation layer: observe a screen, choose an action, execute it, and inspect the result. It also provides a standalone Qt desktop application where users configure their own vision-model API, plus a BDD QA runner for regression tests.

## Choose how to use it

| User | Entry point | Model configuration |
|---|---|---|
| Claude Code, Codex, Qoder or QoderCN CLI user | Managed Agent integration: MCP + shared operation Skill | No Saygo model API key; the external Agent makes decisions |
| Desktop application user | Saygo Desktop (Qt 6 / PySide6) | Configure an OpenAI-compatible vision API URL, model and API key in the app |
| Script or CI user | `saygo device`, `saygo task`, `saygo workflow`, `saygo run` | Direct control needs no key; the built-in QA loop needs model credentials |

Saygo is currently a **development/beta distribution**. Installable source bundles and native packaging scripts exist; this does not mean the packages are published on PyPI or the Chrome Web Store. See the [distribution guide](distribution/README.md) for release artifacts, updates and uninstall.

## Programming Agent setup

### Python package (publication pending)

The wheel and source distribution are prepared for PyPI under `saygo-agent-control`.
Until publication, install the local wheel with `pipx install '/path/to/saygo_agent_control-0.4.3-py3-none-any.whl[mcp]'`.
After the package is published, the standard installation is:

```sh
pipx install 'saygo-agent-control[mcp]'
saygo setup --client codex
# Or: saygo setup --client claude / both / qoder / qodercn / all
```

Python 3.10+, pipx and the selected Agent CLI must already be installed. `saygo setup`
uses the version bundled with the installed package, prepares an isolated runtime,
and registers the plugin and browser bridge. No repository clone is needed.
Use `saygo setup --help` for mobile, browser, update and uninstall options.
Dependencies still require network access. Update with `pipx upgrade saygo-agent-control`
then rerun `saygo setup`; remove client registration with `saygo setup --uninstall`
before removing the pipx application. Managed runtime updates remain a separate,
opt-in GitHub release channel.

Build and first-publication instructions: [Python package release](distribution/PYPI.md).


**Install from source for the Saygo rename.** The repository has been renamed, but the existing release assets predate this change. Use the source installation below until a Saygo-branded release is published:

```sh
git clone https://github.com/WilliamSkyWalker/saygo.git
cd saygo
python3 scripts/install_agent_plugin.py
```

On Windows, use `py -3 scripts/install_agent_plugin.py`. The new identifiers are `saygo` (CLI), `saygo-device` (plugin), and `~/.saygo` (configuration). There are no old-name compatibility aliases.

### Release installer (after Saygo artifacts are published)

With Python 3.10+ and an installed Agent client, copy one command below. It finds the newest release, verifies and runs its installer; no Git or manual GitHub download is needed. Run it in a directory where you can save the installer.

macOS / Linux / WSL:

```sh
curl -fL https://raw.githubusercontent.com/WilliamSkyWalker/saygo/main/scripts/install_saygo.py -o install-saygo.py && python3 install-saygo.py
```

Windows PowerShell:

```powershell
Invoke-WebRequest https://raw.githubusercontent.com/WilliamSkyWalker/saygo/main/scripts/install_saygo.py -OutFile install-saygo.py -ErrorAction Stop; py -3 install-saygo.py
```

The installer automatically downloads and verifies the source archive, prepares an isolated runtime, detects supported clients, installs their integration and shared Skill, and sets up the Chrome/Edge native messaging host. It does not require pyenv.

The command always selects the newest release, including betas. Add `--channel stable` for stable releases only. Run the same command again to upgrade.

After downloading, rerun `install-saygo.py` with `--client claude`, `codex`, `qoder`, `qodercn`, `both` (Claude + Codex), or `all` to select clients. Add `--mobile` for mobile dependencies. On native Windows use `py -3` in place of `python3`.

From a checkout, developers can use `python3 scripts/install_agent_plugin.py`. Check [GitHub Releases](https://github.com/WilliamSkyWalker/saygo/releases) for published assets. The `saygo-0.4.3.zip` name below is the new build output, not a claim that this asset is already published.

Managed installations check for updates in the background and report new releases. Automatic runtime updates are opt-in: add `--auto-update --update-channel beta` when running the installer. Prepared updates activate on a later idle Agent startup; browser/Skill changes prompt a full upgrade. See [update controls and limits](distribution/README.md#update-and-uninstall).

For browser control, load the installer-provided extension directory through **Load unpacked** at `chrome://extensions` (or Edge's extension page), then click **Connect local bridge** in its popup. Restart the Agent client and describe a task, for example:

> List the connected sessions, connect the test browser page as `mail`, and show me its current screen before making changes.

**MCP browser control uses the extension backend. Playwright is excluded from MCP**, including existing Playwright sessions and tasks using them. Playwright remains available through CLI/Runtime for managed test browsers. The extension works with existing website tabs and their login state; it does not extract DOM content for visual decisions.

The installer configures Saygo-scoped permissions for Claude and Qoder/QoderCN. Codex approval setup and client-policy limits are documented in the [distribution guide](distribution/README.md). Qoder integration covers the CLI; IDE integration is not claimed verified.

For direct package installation during development:

```sh
python3 -m pip install -e '.[mcp]'
saygo --help
saygo-mcp --profile device
```

Install only the needed extras: `mobile`, `windows`, `mac`, `desktop`, `browser` (CLI Playwright), `selenium`, or `qa`. A browser-extension-only MCP setup does not need the mobile toolchain. See the [operation guide](docs/agent-control.md) and [plugin guide](plugins/saygo-device/README.md).

## Desktop application

**Under development and testing / 开发测试中。**

Saygo Desktop centers on a conversation, with task history in the sidebar and a message composer at the bottom. Users can configure a model, connect named sessions, run a task, pause for human input, reply to continue, recover a task and export evidence.

- **Windows:** a native x64 portable ZIP has been built. Extract the entire folder and run `SaygoDesktop.exe`; keep `SaygoNativeHost.exe` and `_internal` alongside it. No WSL, Python or pyenv installation is needed. The main application opens without a console; the separate native host handles browser messaging.
- **macOS:** `.app` and DMG packaging scripts and a macOS CI job are prepared. No macOS build/runtime validation has been completed yet.
- **Linux:** the standalone GUI has passed local startup checks. Browser/mobile operation is available through the corresponding backends; local Linux desktop-window automation is not implemented.

The application accepts an OpenAI-compatible vision endpoint. Keys can stay in memory for the session or use a supported OS credential store; they are not written to `desktop.json`. Screenshots and task text are sent to the configured provider.

Developers can launch from source:

```sh
python3 -m pip install -e '.[desktop]'
saygo-desktop
```

Add `windows` or `mac` extras for native desktop input. Native packaging runs **on the target OS**:

```sh
python3 -m pip install pyinstaller
python3 scripts/build_desktop.py
python3 scripts/package_desktop.py
```

Output: Windows ZIP, macOS DMG or Linux tar.gz under `dist/installers`, each with a SHA256 checksum. The manual [desktop workflow](.github/workflows/desktop.yml) builds CI artifacts. These are development builds without release signing or notarization. See [desktop usage and validation](docs/desktop.md).

## Observe, act, verify

CLI and MCP share named session state under `SAYGO_HOME_DIR` (default `~/.saygo`). Clients must use the same state directory and a backend available to both entrances to operate the same target.

```sh
# Existing browser: install and connect the extension first.
saygo device connect --platform browser --backend extension --session mail

# Android device/emulator; obtain its ID with device list.
saygo device list --platform android
saygo device connect --platform android --device DEVICE_ID --session phone

# Windows window-title substring; use --platform mac for a macOS app.
saygo device connect --platform windows --app 'Example App' --session admin

saygo device capabilities --session mail
saygo doctor --session mail
saygo device screenshot --session mail

# Replace OBSERVATION_ID with the returned ID, and choose coordinates from that image.
saygo device act '{"type":"tap","x":50,"y":40,"coordinate_space":"percent"}' \
  --session mail --observation-id OBSERVATION_ID --observe-after
saygo device wait --session mail --mode stable --timeout 5
```

Observations include identity, target, dimensions and coordinate mappings. Crop observations retain the mapping to the original screen. MCP `device_observe` and action results with observations return image content; CLI returns image paths and metadata. Query capabilities before using platform-dependent actions.

**Dispatched input or a stable frame does not establish business success.** Inspect the resulting screen. Prefer visible buttons and menus over keyboard shortcuts. Unsupported actions fail explicitly; Windows background mode must not silently fall back to global keyboard/mouse input. See [control details](docs/control.md).

## Tasks, handoff and recovery

Interactive tasks reuse Runtime while an Agent decides one step at a time. Explicit JSON workflows use `saygo workflow`; they do not require a model planner.

```sh
saygo task create '{"phone":"phone","mail":"mail","admin":"admin"}'
saygo task observe TASK_ID --resource phone
saygo task submit TASK_ID --resource phone --request-id focus-registration \
  --observation-id OBSERVATION_ID \
  --action '{"type":"tap","x_pct":50,"y_pct":40}' --note 'Focus the registration form'
saygo task handoff TASK_ID --instructions 'Please sign into the test mailbox'
saygo task resume TASK_ID --note 'User returned control after login'
saygo task recover TASK_ID
saygo task timeline TASK_ID
saygo task finish TASK_ID --note 'Verified the final screen'
saygo task export TASK_ID --out evidence.zip
```

Use the IDs returned by create/observe. MCP exposes the same task commands through `agent_task`. Project aliases can be saved with `saygo resources bind NAME SESSION`.

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

External Agent control needs no Saygo LLM API key. The standalone GUI has its own model settings. Optional CLI QA settings load in this order:

1. Built-in defaults.
2. `$SAYGO_HOME_DIR/config.env` (default `~/.saygo/config.env`).
3. Working-project `.env`, or the explicit file selected by `SAYGO_CONFIG_FILE`.
4. Process environment variables.

`saygo init` creates a project template; `saygo init --user` creates a user template. Existing files are preserved. Installed package directories are not searched for `.env`; private configuration is never bundled in releases. The Agent installer supports `--config-file /absolute/path/to/private.env` to retain a file reference without copying its contents.

## BDD QA and other tools

The existing QA engine remains available for `.feature` (Gherkin) regression cases, visual reports, model tiering and optional element location:

```sh
python3 -m pip install -e '.[qa,mobile]'
saygo init
# Configure a vision model and target in the private .env file before running.
saygo run tests/my-app/login.feature --report
```

```gherkin
# saygo-platform: android
# saygo-package: com.example.app
Feature: Login
  @auto @android
  Scenario: Show the login form
    Given the app is launched
    When the user taps "Log in"
    Then the email and password fields are visible
```

QA verdicts are model judgements with recorded evidence, not independent proof. Use [probe plugins](docs/probes.md) for non-visual facts such as backend writes. Figma test generation and visual review remain available through `saygo figma`. The existing [`/saygo-drive` Skill](.claude/skills/saygo-drive/SKILL.md) provides a separate QA-oriented Agent workflow; general cross-device tasks should use the shared operation Skill and Runtime records.

For QA execution guards, model configuration, case conventions and module responsibilities, see [agent.md](agent.md), [`.env.example`](.env.example) and [architecture](docs/architecture.md).

## 中文说明

Saygo 为外部编程 Agent 提供手机、浏览器和桌面窗口的视觉操作能力：**观察 → 操作 → 新观察**，并保存跨端任务进度、执行事实和截图。

- **命令行用户**：安装 Claude Code / Codex / Qoder / QoderCN 的集成，由现有 Agent 决策，无需给 Saygo 配模型 Key。当前改名版本请使用上方源码安装步骤；Saygo 品牌安装包发布后再使用单命令下载入口。安装后重启客户端。
- **窗口桌面用户**：使用 Qt 桌面版，在界面配置自己的视觉模型 API、模型名和 Key。Windows 原生便携包无需 WSL、Python 或 pyenv。macOS DMG 的脚本和 CI 已就绪，但尚未完成 macOS 构建与实测。
- **浏览器**：MCP 和桌面版使用 Chrome/Edge 扩展，保留现有页面和登录状态；MCP 不支持 Playwright。扩展暂通过开发者模式加载，未声称已上架商店。
- **恢复与接管**：CLI/MCP 共用命名会话；`saygo task` 保存操作记录、支持人工接管和中断恢复。结果不确定的动作需核对，不自动重放。点击派发成功或画面稳定都不等于业务完成。
- **测试边界**：Windows EXE 已通过启动和桥接握手验证，完整跨端业务验收与干净机器安装仍需实测；不要将模拟测试当作真机兼容性证明。

安装与更新见[分发说明](distribution/README.md)，窗口版见[桌面指南](docs/desktop.md)，会话、操作和恢复见[Agent 操作指南](docs/agent-control.md)，开发约束见[通用 agent.md](agent.md)。

## License

Saygo is licensed under [MIT](LICENSE). Third-party dependencies retain their respective licenses.

Saygo 采用 MIT 许可证；第三方依赖遵循各自许可证。
