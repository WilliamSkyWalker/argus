# Argus

> A vision-based AI agent that replaces the human QA tester.

Argus reads a **BDD `.feature` test case** (Gherkin / Cucumber), **looks at the screen** (iOS / Android / Browser), decides what to do, performs the action, and judges pass/fail on its own — the way a human tester would, but driven by a vision LLM.

- 👁️ **Pure-vision first** — sends the raw screenshot to the LLM; uses the UI tree for precise tap coordinates when one is available, falls back to visual estimation otherwise.
- 🧭 **Step-driven** — iterates Gherkin steps one at a time; a hard validator forbids step-skipping and bans "PASS" on assertions that can't be visually verified (no self-deception).
- 🤖 **Self-healing reports** — after a failure it runs a root-cause classifier (`case_outdated / app_bug / llm_misjudge / fixture_failure / flaky`) for human review.
- 📱 **Multi-platform, multi-device** — Android (adb + uiautomator2), iOS (idb + simctl), Browser (Selenium). Schedule many Android devices against a shared case queue.
- 🎨 **Figma integration** — generate test cases from a design, or run a visual design-vs-screenshot review.
- 🔌 **MCP** — exposes its capabilities as MCP tools (drive it from Claude Code / Cursor) and can itself call external MCP servers (e.g. Figma).

---

## How it works

```
BDD .feature test case (hand-written, or Figma-generated)
        │
        ▼
  gherkin.py  ──►  parse into steps + metadata
        │
        ▼
  planner.py  (1 LLM call/case)  ──►  per-step intent / expected_state / action_hint
        │
        ▼
  agent.py  — step-driven main loop
     for each Gherkin step:
       for sub-action in 1..10:
         dialog_dismisser.dismiss()        # auto-close known system popups
         screenshot + skills enhance       # loading / keyboard / scroll / element-marker / diff
         brain.decide() → JSON action      # vision LLM
         step_validator.validate()         # monotonic step index, evidence, no blind PASS
           ├─ reject → feed reason back to the LLM
           ├─ pass   → advance to next step
           └─ fail   → abort scenario
        │
        ▼
  healer.py  (only on fail/timeout)  ──►  verdict for human review
        │
        ▼
  Platform abstraction:  iOS │ Android │ Browser
```

## Supported platforms

| Platform | Driver |
|----------|--------|
| Android  | `adb` + `uiautomator2` (text input via `ACTION_SET_TEXT`, bypassing the IME) |
| iOS      | `idb` + `xcrun simctl` (auto-detects simulator vs real device) |
| Browser  | Selenium WebDriver (local or Selenium Grid, optional headless) |

---

## Quick start (newcomer guide)

### 1. Prerequisites

```bash
# Python deps (Argus runs as a module — you do NOT install argus itself)
pip3 install openai Pillow uiautomator2 selenium fb-idb

# Android
brew install android-platform-tools        # adb
#   uiautomator2 auto-pushes its server apk on first device connect

# iOS (only if testing iOS)
brew install idb-companion

# Browser (only if testing web) — install a WebDriver / use Selenium Grid
```

> Note: invoke everything as `python3 -m argus.cli <command>` from the repo root.
> Handy alias: `alias argus="python3 -m argus.cli"`.

### 2. Configure

```bash
python3 -m argus.cli init      # writes a default .env
```

Then edit `.env` and set your LLM key. The default provider is **OpenRouter** (OpenAI-compatible):

```env
PLATFORM=android
LLM_PROVIDER=openai
LLM_API_BASE=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-v1-...
LLM_MODEL=gemini-2.5-flash      # any vision model: google/gemini-2.5-flash, anthropic/claude-sonnet-4.5, ...
```

### 3. Run your first test

```bash
# A single .feature file
python3 -m argus.cli run tests/my-app/login.feature --report

# A whole target directory (recursively runs every .feature), with an HTML report
python3 -m argus.cli run my-app --report
```

Open the generated `tests/<target>/reports/latest.html` to see screenshots, the LLM's reasoning, per-step evidence, and the healer verdict.

---

## Writing test cases

Argus uses **BDD `.feature` files (Gherkin / Cucumber)**.

```gherkin
# argus-platform: android
# argus-package: com.example.app

Feature: Login

  Background:
    Given the app is installed and launched

  @P0 @auto @android @reset:pm_clear
  Scenario: Log in with email and password
    When the user taps "Continue with E-mail"
    And  enters "${EMAIL}" and taps "Continue"
    And  enters "${PASSWORD}" and taps "Continue"
    Then the home screen is shown with a bottom navigation bar
```

Tags Argus understands: `@P0/@P1/@P2` (priority) · `@auto/@partial/@manual` (automation; partial/manual are auto-skipped) · `@ios/@android/@both` (platform gate) · `@TC-XXX` (case id) · `@reset:pm_clear|relaunch|none` (Android state reset) · `@skip/@wip`.

### Per-target conventions (`tests/<target>/`)

- `_preconditions.md` — auto-prepended to every case; tells the LLM how to recover to the Background state when the screen doesn't match.
- `_accounts.json` — account pool. With multiple `--device`s, worker `i` binds `accounts[i]`; `${EMAIL}` / `${PASSWORD}` placeholders in cases are substituted.
- `reports/<timestamp>/*.html` — reports (`reports/latest.html` symlinks the newest).

---

## CLI cheatsheet

```bash
# Execution
argus run <target | dir | .feature file>   # run BDD cases
argus run my-app --report                   # auto HTML report
argus run my-app -j 4                        # 4-way browser concurrency (needs Selenium Grid)

# Multi-device Android (shared queue + dynamic scheduling)
argus run my-app --device serial1 serial2 serial3 --apk app.apk --report
#   --apk installs to all devices first (3 retries); each device binds _accounts.json[i]

argus run my-app --shard 0/3            # manual sharding (without multi-device)
argus run my-app --bg                   # run in background
argus status                              # list background runs
argus status <run_id>                     # inspect one run

# Project management
argus list                                # list targets
argus devices                             # list iOS simulators
argus setup                               # create & boot a simulator

# Figma
argus figma gen-tests <figma-url> -o tests.feature
argus figma review <figma-url> --platform ios --screenshot app.png -o review.html
```

## MCP

Argus speaks MCP both ways.

**As a server** — `argus/mcp/server.py` (FastMCP, stdio transport) exposes 12 tools so you can drive Argus from Claude Code / Cursor / Claude Desktop in plain language (*"list the my-app scenarios"*, *"run login.feature"*):

- read-only: `list_targets` / `list_cases` / `list_runs` / `get_run_status` / `get_report`
- execution: `run_target` / `run_case` / `cancel_run` (async — returns a `run_id` to poll)
- devices: `list_devices` / `install_apk` / `adb_reconnect` / `setup_simulator`

Start it with `python3 -m argus.mcp.server`. The repo ships a `.mcp.json`, so after cloning, **Claude Code auto-mounts the `argus` server** — just talk to it.

**As a client** — during a run, Argus's brain can call external MCP servers (e.g. the Figma MCP). Configure them in `.argus/mcp_clients.json` (a `.example` is checked in; real tokens are gitignored). When a server is registered, the brain pulls its tool catalog and may invoke those tools mid-decision (every call is logged for audit).

## Key configuration (`.env`)

| Var | Meaning |
|-----|---------|
| `PLATFORM` | `ios` \| `android` \| `browser` |
| `LLM_PROVIDER` / `LLM_API_BASE` / `LLM_API_KEY` / `LLM_MODEL` | LLM (OpenAI-compatible; OpenRouter by default) |
| `ANDROID_SERIAL` / `ANDROID_PACKAGE` | single-device serial / **app package under test (required for Android — no default; configure in `.env`)** |
| `LLM_MAX_TOKENS` | output token cap shared by brain + planner (default 8192) |
| `AGENT_MAX_STEPS` | backstop step cap (normal path uses per-step sub-action limit of 10) |
| `SKILLS_ENABLED` | preprocessing pipeline (loading/keyboard/scroll/element-marker/diff/toast) |
| `TAP_SNAP_TO_CLICKABLE` | Android: snap a tap onto the enclosing clickable node's center (default on; no-op for treeless apps like Flutter) |

> ⚠️ **Upgrade note (breaking, Android only):** `ANDROID_PACKAGE` no longer has a hard-coded default — **set it in `.env`** (`ANDROID_PACKAGE=com.your.app`) or override per-run via the env var (`ANDROID_PACKAGE=com.your.app python3 -m argus.cli run …`). If unset, Android state-reset raises a clear error instead of silently testing the wrong app. `_accounts.json` is unchanged — `git pull` upgrade needs no data migration; browser/iOS unaffected.

## Project layout

```
argus/            core engine (agent, brain, planner, healer, validators, gherkin, skills, platforms, mcp, drive)
tests/            test targets (each a folder of .feature/.md cases + _preconditions.md + _accounts.json + reports)
.mcp.json         lets Claude Code auto-mount the argus MCP server
CLAUDE.md         deep architecture & behavior notes (read this to contribute)
```

## Known limitations

- **Vision coordinate offset on tiny edge elements.** Mitigations: prefer UI-tree `bounds`, `element_marker`, **tap snap-to-clickable**, and writing hints as directions ("top-right") rather than pixel coordinates.
- **Treeless apps (e.g. Flutter Canvas)** expose little/no UI tree → Argus relies on vision; tap-snap and tree-simplification are automatically no-ops there.
- Assertions that can't be visually verified (analytics events, backend calls, system time, notification drawer, cross-app deep links) are intentionally **forced to fail** — write them out or tag them out.

For the full architecture, module-by-module notes, and contribution guidance, see **[CLAUDE.md](./CLAUDE.md)**.

---
---

# Argus（中文）

> 用视觉的 AI agent，替代人工 QA 测试员。

Argus 读取 **BDD `.feature` 测试用例**（Gherkin / Cucumber），**直接看屏幕**（iOS / Android / 浏览器），自己决定怎么操作、执行动作，并自主判定通过/失败 —— 像人类测试员一样，但由视觉大模型驱动。

- 👁️ **纯视觉优先** —— 把原始截图发给 LLM；有 UI 树时用 bounds 精确点击，没有时退回视觉估算。
- 🧭 **Step-driven** —— 逐个推进 Gherkin step；硬校验器禁止跳步，并禁止对"无法视觉验证的断言"判 PASS（杜绝自欺）。
- 🤖 **自愈报告** —— 失败后跑根因分类（`case_outdated / app_bug / llm_misjudge / fixture_failure / flaky`）供人工复审。
- 📱 **多平台多设备** —— Android（adb + uiautomator2）、iOS（idb + simctl）、浏览器（Selenium）。多台 Android 可共享用例队列动态调度。
- 🎨 **Figma 集成** —— 从设计稿生成用例，或做"设计 vs 截图"视觉走查。
- 🔌 **MCP** —— 把自身能力暴露为 MCP 工具（可被 Claude Code / Cursor 调用），也能调用外部 MCP server（如 Figma）。

## 工作原理

```
BDD .feature 测试用例（手写，或 Figma 生成）
        │
        ▼
  gherkin.py  ──►  解析成 step 列表 + metadata
        │
        ▼
  planner.py  (每 case 1 次 LLM)  ──►  每步 intent / expected_state / action_hint
        │
        ▼
  agent.py  — step-driven 主循环
     对每个 Gherkin step：
       sub-action 循环 1..10：
         dialog_dismisser.dismiss()        # 自动关已知系统弹窗
         截图 + skills 增强                 # 加载/键盘/滚动/元素标注/变化检测
         brain.decide() → JSON 动作         # 视觉 LLM
         step_validator.validate()         # step 单调、evidence、不许盲目 PASS
           ├─ reject → 把理由喂回 LLM 自纠
           ├─ pass   → 推进下一步
           └─ fail   → 终止 Scenario
        │
        ▼
  healer.py  (仅 fail/timeout)  ──►  根因 verdict
        │
        ▼
  平台抽象：iOS │ Android │ 浏览器
```

## 新手指引

### 1. 装依赖

```bash
# Python 依赖（不安装 argus 本身，直接以 module 形式跑）
pip3 install openai Pillow uiautomator2 selenium fb-idb

# Android
brew install android-platform-tools        # adb（首次连设备 uiautomator2 自动推 server apk）

# iOS（只测 iOS 才装）
brew install idb-companion
```

> 在仓库根目录用 `python3 -m argus.cli <command>` 调用。建议 `alias argus="python3 -m argus.cli"`。

### 2. 配置

```bash
python3 -m argus.cli init      # 生成默认 .env
```

编辑 `.env` 填 LLM key，默认用 **OpenRouter**（OpenAI 兼容协议）：

```env
PLATFORM=android
LLM_PROVIDER=openai
LLM_API_BASE=https://openrouter.ai/api/v1
LLM_API_KEY=sk-or-v1-...
LLM_MODEL=gemini-2.5-flash      # 任意视觉模型：google/gemini-2.5-flash、anthropic/claude-sonnet-4.5 …
ANDROID_PACKAGE=com.your.app    # 被测包名：跑 Android 必填，在这里配好（无默认，缺则报错）
```

> ⚠️ **升级提示（破坏性，仅 Android）**：`ANDROID_PACKAGE` 不再有写死的默认值 —— **请在 `.env` 里配** `ANDROID_PACKAGE=com.your.app`，或跑测时用环境变量临时覆盖 `ANDROID_PACKAGE=com.your.app python3 -m argus.cli run …`。不配的话跑 Android 会**直接报错**（而不是静默测错 App）。`_accounts.json` 格式不变，`git pull` 升级无需迁移数据；Browser / iOS 不受影响。

### 3. 跑第一个测试

```bash
# 单个 .feature 文件
python3 -m argus.cli run tests/my-app/login.feature --report

# 整个 target 目录（递归跑所有 .feature）+ HTML 报告
python3 -m argus.cli run my-app --report
```

打开生成的 `tests/<target>/reports/latest.html`，可看截图、LLM 思考、逐步 evidence 和 healer verdict。

## 用例格式（BDD Gherkin）

**`.feature`（Gherkin / Cucumber）** —— 见上方英文示例。Argus 识别的 tag：`@P0/@P1/@P2`（优先级）·`@auto/@partial/@manual`（自动化程度，partial/manual 自动跳过）·`@ios/@android/@both`（平台限制）·`@TC-XXX`（用例 ID）·`@reset:pm_clear|relaunch|none`（Android 状态重置）·`@skip/@wip`。

**每个 target 目录约定**：`_preconditions.md`（自动 prepend，告诉 LLM 怎么从异常态恢复到 Background）、`_accounts.json`（账号池，多设备时 worker `i` 绑 `accounts[i]`，用例里 `${EMAIL}`/`${PASSWORD}` 占位符被替换）、`reports/`（报告，`latest.html` 软链最新）。

## 常用命令

```bash
argus run <target | 目录 | .feature 文件>      # 跑 BDD 用例
argus run my-app --report                     # 自动 HTML 报告
# 多 Android 设备：共享队列 + 动态调度 + APK 并行安装 + 账号池自动绑定
argus run my-app --device s1 s2 s3 --apk app.apk --report
argus run my-app --bg                         # 后台跑
argus status [<run_id>]                         # 看后台任务
argus list / argus devices / argus setup        # 列 target / 列模拟器 / 建并启动模拟器
argus figma gen-tests <url> -o tests.feature         # 从 Figma 生成用例
```

## MCP

Argus 双向支持 MCP。

**作为 server**：`argus/mcp/server.py`（FastMCP，stdio）暴露 12 个 tool，可在 Claude Code / Cursor / Claude Desktop 里用自然语言驱动 Argus（"列一下 my-app 的用例"、"跑 login.feature"）：

- 只读：`list_targets` / `list_cases` / `list_runs` / `get_run_status` / `get_report`
- 跑测：`run_target` / `run_case` / `cancel_run`（异步，返回 `run_id` 轮询）
- 设备：`list_devices` / `install_apk` / `adb_reconnect` / `setup_simulator`

启动：`python3 -m argus.mcp.server`。仓库自带 `.mcp.json`，clone 后 **Claude Code 自动挂载 `argus` server**，直接对话即可。

**作为 client**：跑测时 brain 可调用外部 MCP server（如 Figma MCP）。在 `.argus/mcp_clients.json` 配置（入库的是 `.example`，真 token 被 gitignore）。注册了 server 后，brain 会拉取其 tool catalog 并在决策中按需调用（每次调用都记日志便于审计）。

## 已知限制

- **小元素视觉坐标偏差**：应对 —— 优先用 UI 树 `bounds`、`element_marker`、**tap 吸附（snap-to-clickable）**、hints 写方位不写像素。
- **无 UI 树的 app（如 Flutter Canvas）** 暴露不出可用树 → 走纯视觉；tap 吸附与树简化在这类 app 上自动 no-op。
- **不可视觉验证的断言**（埋点 / 后端调用 / 系统时间 / 通知抽屉 / 跨 App deeplink）被**强制判 fail** —— 请改写或用 tag 排除。

完整架构、逐模块说明与贡献指引见 **[CLAUDE.md](./CLAUDE.md)**。
