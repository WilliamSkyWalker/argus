# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What is this

Argus — a vision-based AI Agent that replaces human QA testers. Given a BDD `.feature` (Cucumber-Gherkin) or TDD-style markdown case, the agent observes the screen (iOS / Android / Browser), decides what to do, executes actions, and judges pass/fail autonomously. Supports Figma design integration for auto-generating test cases and visual review.

主战场是 Android — `tests/nb_cases/`（移动端 App，BDD `.feature` 格式），独立 git 仓库挂在子目录里。

## Architecture

```
                         ┌─────────────────────┐
                         │   Figma Integration  │
                         │  figma.py / figma_ops│
                         │  gen-tests / review  │
                         └────────┬────────────┘
                                  ↓
测试用例 (.feature / TDD .md / 自然语言 / Figma 生成)
                  ↓
        gherkin.py 解析 → render_case() → step 列表 + metadata
                  ↓
       ┌──────────────────────────────────────┐
       │   planner.py (LLM, 1 call/case)      │
       │   产出每个 step 的 intent/expected/  │
       │   action_hint，注入 brain 上下文     │
       └────────────────┬─────────────────────┘
                        ↓
       ┌──────────────────────────────────────┐
       │   agent.py — Step-driven 主循环      │
       │   for each Gherkin step:             │
       │     for sub-action in 1..10:         │
       │       dialog_dismisser.dismiss()     │
       │       screenshot + skills enhance    │
       │       brain.decide() → JSON          │
       │       step_validator.validate() ──┐  │
       │       ├─ reject → retry_feedback ─┘  │
       │       ├─ in_progress → exec action   │
       │       ├─ pass → advance step         │
       │       └─ fail → abort Scenario       │
       └────────────────┬─────────────────────┘
                        ↓
       ┌──────────────────────────────────────┐
       │   healer.py (LLM, 仅 fail/timeout)   │
       │   verdict: case_outdated / app_bug / │
       │   llm_misjudge / fixture / flaky     │
       └────────────────┬─────────────────────┘
                        ↓
                Platform Abstraction (platforms/base.py)
        ┌────────────┬────────────┬────────────┐
        │   iOS      │  Android   │  Browser   │
        │ idb+simctl │ adb+u2     │ Selenium   │
        └────────────┴────────────┴────────────┘
```

### Core modules (under `argus/`)

- `agent.py` — **step-driven** 主循环。外层迭代 Scenario step，内层每 step 最多 `PER_STEP_SUB_ACTION_LIMIT=10` 次 LLM sub-action，必须由 LLM 输出 `current_step_status=pass` 才能推进。Validator reject 不消耗 sub-action 配额（最多 `MAX_REJECTS_PER_STEP=3` 连续 reject）
- `brain.py` — LLM 决策。发**原始截图**（无 grid 覆盖）+ 最近 1–3 张历史截图 + UI tree + planner hint + step 列表 + 已通过 step 的 evidence 锚点 + validator 上次的 reject 理由。返回带 `step_progress` 块的 JSON。prompt 里硬规定「不可视觉验证的断言禁止 PASS」（埋点 / 后端 API / 系统时间 / launcher icon 等都必须 fail，杜绝 LLM 自欺）
- `planner.py` — 在 executor 启动前跑一次 LLM，把整个 Scenario 拆成 `intent / expected_state / action_hint` 结构化剧本，作为 hint 注入 brain。失败时 graceful degrade（plan=空，不阻塞 executor）
- `healer.py` — case fail/timeout 后跑一次 LLM 分析根因，verdict 五分类：`case_outdated / app_bug / llm_misjudge / fixture_failure / flaky`。写到报告里给人复审
- `step_validator.py` — 「看全局，禁跳跃」的硬墙。验 `current_step_index` 单调（+0/+1，禁跳跃 / 倒退）、`evidence` 至少 15 字符 + 引用具体屏幕元素、`fail_reason` 长度，`in_progress` 必须带 action。Reject 理由喂回 LLM 让它自己改
- `dialog_dismisser.py` — 每个 turn 截图前 + 截图后各跑一次，自动 dismiss 已知系统弹窗（Android runtime permission / "Dark Mode Is Here" overlay / iOS "Don't Allow"）。Case 里加 `**Disable dialog auto-dismiss**: yes` 可关闭（适用于专门测弹窗本身的 case）
- `gherkin.py` — 自家手写的 .feature 解析器。支持 Feature / Background / Scenario / Scenario Outline + Examples 全套语法，`@tag` → metadata（`@P0/@auto/@partial/@manual/@TC-XXX/@reset:pm_clear/@ios/@android/@skip/@wip`）。`render_case()` 输出兼容 cli.py 的 markdown 正则（`**Reset before**` / `**Platform**` / `**Automation**` 字段）
- `config.py` — defaults → .env → environment variables。**默认 LLM 是 OpenRouter + `google/gemini-2.5-flash`**（不再是 Qwen DashScope）
- `cli.py` — CLI 入口。支持 `.feature` / `.md` / 目录 / inline 文本；多 Android 设备调度（`--device s1 s2 ...` 共享队列 + 账号池自动绑定）；`--apk` 安装重试 3 次；`--shard N/M` 手动切片；`--bg` 后台跑；report auto 落到 `tests/<first-level>/reports/<ts>/` 子目录
- `report.py` — 报告导出（JSON / 内嵌 base64 截图的 HTML），渲染 evidence + healer verdict + LLM thinking
- `logger.py` — 结构化日志（不用 print）
- `grid.py` — 留存的坐标 grid 工具，**不再画在主图上**（纯视觉路线后保留给可能的 opt-in 或测试）

### `argus.mcp` — MCP server & client

把 argus 能力暴露成 MCP tools（供 Claude Code / Desktop / Cursor 调用），同时
给 argus 自己装上 MCP client（供 brain 调外部 server，如 Figma MCP）。

**Server**：`argus/mcp/server.py` — FastMCP 实例，stdio transport，12 个 tool：
- 只读：`list_targets / list_cases / list_runs / get_run_status / get_report`
- 跑测：`run_target / run_case / cancel_run`（异步后台跑，返回 run_id，配合 get_run_status 轮询）
- 设备：`list_devices / install_apk / adb_reconnect / setup_simulator`

启动：`python3 -m argus.mcp.server`。stdio 注意 — server tool 内部走 `_silenced_stdout`
把 cli helper 的 `print()` 重定向到 stderr，避免污染 JSON-RPC channel；
`argus.logger` 本身走 stderr，安全。

**Claude Code 集成**：项目根目录已有 `.mcp.json`，clone 后 Claude Code 自动挂载
`argus` server，直接用 tools 说话即可（"列一下 nb_cases 的所有 case"、"跑
04-feed-foryou.feature"）。

**Client**：`argus/mcp/client.py` — `MCPClient` (async) / `MCPClientSync` (sync
facade) / `MCPRegistry` (多 server 聚合)，配 `to_openai_tools()` adapter 转
OpenAI Chat Completions tool schema。配置文件 `.argus/mcp_clients.json`
（.example 入库；实际配置含 token 被 .gitignore）。

**Brain 集成**：`agent.py` 启动时若 `.argus/mcp_clients.json` 存在则自动加载
registry 喂给 Brain。Brain 收到非空 registry 时：
- 启动时一次性拉所有 server 的 tool catalog，转 OpenAI tool schema 缓存
- `decide()` 主调用附 `tools=[...]`，跑最多 `MCP_TOOL_LOOP_LIMIT=3` 轮 tool_call 交互
- 每个 tool 调用都 log 出参数（审计 LLM 行为用）
- 触顶仍未出 action 时强制最后一轮 + `response_format=json_object` 兜底
- registry 为空时代码路径完全等同接入前（`response_format=json_object` 单 round）

**Figma 集成**：`figma_via_mcp.py` 提供 `MCPFigmaClient`（与 `FigmaClient` 同形
签名）+ `get_figma_client(token, prefer_mcp=True)` dispatcher。`figma_ops.py` /
CLI 都改用 dispatcher，不再硬绑 REST。逻辑：注册了 `figma` MCP server 走 MCP
（无需 `FIGMA_TOKEN`），没注册走 REST（要 token）。Tool 名映射默认对齐
`@figma/mcp-server`（`get_metadata` / `get_screenshot`），如自家 MCP server 用
别名可在 server 配置加 `argus_mapping` 字段覆盖。

### `argus.drive` — alternate driver（Claude Code as brain）

`argus/drive/` 把 Claude Code 会话本身当作 argus 的另一种 driver：Claude 是 brain，`adb` 是 platform，对话 turn 是主循环，跑 `.feature` 用例并输出 argus 风格 HTML 报告。

- `render.py` — 把 Claude 跑测过程中维护的 `journal.json` 渲染成 HTML（复用 `argus.report.save_html`）。运行：`python3 -m argus.drive.render --journal foo.json --output foo.html`
- 启动方式：用户在 Claude Code 里调 `/argus-drive` skill（见下文 Skills）

### Platform drivers (under `argus/platforms/`)

- `base.py` — abstract Platform 接口（screenshot, tap, swipe, input, is_ime_visible, …）
- `ios.py` — iOS via idb；自动检测 simulator vs real device
- `android.py` — Android via adb + uiautomator2；`input_text` 走 ACTION_SET_TEXT 不经过 IME（避免 Gboard 拼音拦截）。`get_ui_tree()` 返回**原始** uiautomator 树（不在此简化；缓存到 `_last_raw_tree` 供 tap 吸附用）。`tap()` 前做 snap-to-clickable 坐标修正（见 VLM 限制 §5）
- `browser.py` — Selenium WebDriver；local & Selenium Grid；headless 可选。启动时查实际 `innerWidth/innerHeight` 写回 viewport，校正"配置 1440×900 vs 实际 1432×757"的坐标偏移

iOS-specific helpers：`simulator.py`（`xcrun simctl` 生命周期）、`hands.py`（idb touch/input）。

### Vision skills (under `argus/skills/`)

Pluggable preprocessing pipeline that runs between screenshot capture and LLM decision：

- `base.py` — Skill abstract base class, SkillContext, SkillResult
- `__init__.py` — skill registry, `create_pipeline()`, `run_pipeline()`

**Phase 1 — 状态感知（默认开启）:**
- `loading_detector.py` — 检测加载状态（spinner/骨架屏/空白页），建议 LLM wait
- `keyboard_detector.py` — 检测软键盘弹出，报告遮挡区域，避免盲点操作
- `scroll_map.py` — 分析滚动位置，告知 LLM 视口在页面什么位置、上下是否有更多内容

**Phase 2 — 元素标注（默认开启）:**
- `element_marker.py` — 从 UI tree 提取可交互元素，在截图上画编号标记

**Phase 3 — 变化检测（默认开启）:**
- `visual_diff.py` — 对比前后截图，高亮变化区域。也被 `agent.py` 用于检测"上一动作无可见效果"
- `toast_detector.py` — 检测短暂出现的 toast/snackbar 通知，裁剪发送给 LLM

**Phase 4 — 增强（默认关闭，按需开启）:**
- `smart_crop.py` — 自动裁剪感兴趣区域，发送放大图。当前**默认关**，纯视觉路线后多张放大图反而干扰

**Phase 5 — 验证（按需开启）:**
- `ocr.py` — OCR 文字提取（需 easyocr）
- `color_validator.py` — 色彩分析，UI 风格验证
- `layout_checker.py` — 布局检查（重叠/裁切/对齐/触控目标大小），配合 Figma review

Pipeline flow: `screenshot → [状态感知 → 元素标注 → 变化检测 → 验证] → enhanced text + extra images → LLM`

### Figma integration

- `figma.py` — Figma REST API 客户端（auth, file structure, PNG export, node extraction）
- `figma_ops.py` — LLM-powered ops：design → test cases、visual review（design vs screenshot）

## Test cases — 两种格式都支持

### 1. `.feature` (Gherkin / Cucumber) — 推荐用法

`tests/nb_cases/nb_mobile/02-feed/04-feed-foryou.feature` 这种 — `gherkin.py` 全套解析。

```gherkin
# argus-reset-default: relaunch

Feature: Feed 流 - For You Tab 核心交互

  Background:
    Given 测试账号 ${EMAIL} 已登录
    And 已落地 Home Feed For You Tab

  @P0 @auto @android @TC-FEED-002 @reset:pm_clear
  Scenario: For You Tab 下拉刷新替换推荐内容
    Given For You 列表首屏已加载完成
    When 用户从屏幕顶部向下拖拽约屏幕高度的 1/4
    Then 顶部 Tab Bar 下方出现一个圆形旋转 spinner

  @P1 @auto @android @TC-FEED-003-outline
  Scenario Outline: 不同账户类型登录
    When 输入 <email>
    Then 跳转到 <next_page>

    Examples:
      | email             | next_page  |
      | a@example.com     | Home Feed  |
      | b@gmail.com       | Onboarding |
```

Tag 约定（gherkin.py 识别）：
- `@P0 / @P1 / @P2` — 优先级
- `@auto / @partial / @manual` — 自动化程度（`partial/manual` 会被 cli 自动 skip）
- `@ios / @android / @both` — 平台限制（mismatch 会自动 skip）
- `@TC-XXX-NNN` — case ID（写报告用）
- `@reset:pm_clear / @reset:relaunch / @reset:none` — Android 状态重置策略（覆盖 feature 级 `# argus-reset-default`）
- `@skip / @wip` — 整 scenario 跳过
- Scenario Outline 展开时 TC tag 自动加 `-1/-2/...` 后缀保持 ID 唯一

### 2. TDD-style markdown — eve-kit / settings-app 老格式

```markdown
### TC-EVE-XXX-001  标题
- **Priority**: P0
- **Reset before**: pm_clear   ← Android 重置策略（pm_clear | relaunch | none）
- **Platform**: android         ← 平台限制（ios | android | both）
- **Mode**: exploratory         ← 可选；命中 brain 注入探索性测试提示
- **Steps**:
  Given 前置条件
  When  执行步骤
  Then  断言
```

Parser 在 `cli.py:_resolve_test_target` 切目录 / 文件 / inline；`.feature` 走 `gherkin.parse_feature_file`，`.md` 沿用按 `### TC-` 切块的旧路径。

### Per-target 文件约定

每个 `tests/<target>/` 目录可以放：
- `cases/*.md` 或子目录 `*.feature` — 测试用例
- `_preconditions.md` — argus auto-prepend 到每个 case 文本前，告诉 LLM "如何从异常状态恢复到 Background Given 描述的前置状态"。极大降低 fixture 未就位时的 false fail
- `_accounts.json` — **账号/密钥池**（数组），只装两类东西：① 不能进 git 的凭据（账号/密码），case 里用 `${EMAIL}` / `${PASSWORD}` 占位符引用；② 并发互斥的可互换资源。多 `--device` 调度时 dispatcher 按 worker `i` 直接绑 `accounts[i]`，单设备用 `accounts[0]`（无 env 透传）。**普通测试数据（输入变体）用 Gherkin `Examples` 表 / Data Table，别塞进账号池**
- `_accounts.json.example` — 模板（不要把真账号 commit 进 git）
- `reports/<timestamp>/*.html` — 报告（每次 run 一个时间戳子目录），`reports/latest.html` 软链指向最新

### Test targets (under `tests/`)

```
tests/
  ├── nb_cases/        # 🔥 主战场：移动端 App（独立 git 仓库，单独管理）
  │   ├── nb_mobile/   # 10 个模块 .feature （01-account ~ 10-cross）
  │   ├── _preconditions.md / _accounts.json
  │   └── *.apk        # 被测包
  ├── eve-kit/         # 旧 target：EVE Kit 工业工具（browser，.md 格式）
  ├── settings-app/    # 旧 target：iOS 设置 App
  ├── browser-demo/    # 旧 target：浏览器示例
  └── _template/       # 新项目模板
```

### 新建测试 target 流程

新建一个被测项目（target）= 在 `tests/` 下建一个目录 + 填几个约定文件。两种方式：

**方式一：脚手架命令（推荐）**

```bash
argus new <target> --platform android --package com.x.y   # android
argus new <target> --platform browser --url https://x.com # browser
argus new <target> --platform ios                         # ios
```

自动从 `tests/_template/` 生成 `tests/<target>/`，并把 `cases/example.feature` 的元数据头
（`argus-target/platform/package/reset-default`）按参数填好，附 `_preconditions.md` 样例 +
`_accounts.json.example`。目录已存在时拒绝覆盖。生成后按提示改用例即可。

**方式二：手动**

1. `cp -r tests/_template tests/<target>`（或照着 `_template/` 建）
2. 改 `cases/example.feature` 的 4 行元数据头（target / platform / package / reset-default），写真实用例；文件名/拆分按业务模块
3. 改 `_preconditions.md`：把样例占位换成你产品真实的「状态机判断 + 登录恢复 + 首页→子页导航」，这一步对降 false-fail 最关键
4. 多账号并发：把 `_accounts.json.example` 复制成 `_accounts.json` 填真账号（**别 commit**），用例里用 `${EMAIL}` 等占位符
5. 改 `README.md` 描述（`argus list` 取首段非标题行）
6. 跑：`argus run <target>`（单文件 `argus run <target>/cases/foo.feature`）

**关键约定（务必遵守）**：
- 用例**自包含**：前置态写进 Background，进入子页的操作明示，不依赖前置 case 状态
- `.feature` 元数据头**值行不要写行内 `#` 注释**（会被读进值里）；说明放整行注释
- 不可视觉验证的断言（埋点 / 后端 / 系统时间）打 `@skip-vision` 或改写，不要让它强制 fail
- target 名 = `tests/` 下第一级目录名；`argus run <target>` 和报告落盘 `tests/<target>/reports/` 都按它解析

## Setup

```bash
# 安装依赖（不装 argus 本身——直接以 module 形式跑 cli）
pip3 install openai Pillow uiautomator2 selenium fb-idb

# iOS
brew install idb-companion
python3 -m argus.cli setup     # create & boot simulator

# Android
brew install android-platform-tools
# 首次连接设备时 uiautomator2 会自动 push uiautomator-server.apk

# 配置
python3 -m argus.cli init       # 生成默认 .env
# 然后编辑 .env 填 LLM_API_KEY（OpenRouter sk-or-v1-...）
```

**调用方式**：项目根目录下 `python3 -m argus.cli <command>`。嫌长可加 alias：
`alias argus="python3 -m argus.cli"`，下文示例里 `python3 -m argus.cli` 都可简写。

## CLI Commands

```bash
# ── Test execution ──
argus run nb_cases                                          # 整目录递归（.feature + .md 混合）
argus run nb_cases/nb_mobile/02-feed                        # 子目录
argus run tests/nb_cases/nb_mobile/02-feed/04-feed-foryou.feature   # 单 feature 文件
argus run eve-kit -j 4                                      # 4-way 并发（需 Selenium Grid）
argus run eve-kit --report                                  # 自动 report 落到 tests/eve-kit/reports/<ts>/

# ── 多 Android 设备调度（核心生产用法）──
argus run nb_cases --device serial1 serial2 serial3 --apk app.apk --report
# ↑ 共享 case 队列 + 动态调度（慢设备碰到难 case 不阻塞其它设备）
#   每台设备绑定 _accounts.json[i]（避免同账号同时登录冲突）
#   --apk 先 parallel install 到 3 台设备（3 次重试，失败设备剔除继续）
#   adb connect 上线检查 20s 超时（mDNS serial 不重连，等设备自播报）

# ── 手动分片（不走多设备调度时用）──
argus run nb_cases --shard 0/3                              # 跑前 1/3
argus run nb_cases --shard 1/3                              # 中间 1/3

# ── 后台跑 ──
argus run nb_cases --bg --report --device serial1 serial2   # 后台 + 多设备
argus status                                                 # 列所有后台任务
argus status <run_id>                                        # 看具体 run 日志/状态

# ── inline ──
argus run "打开设置 App，验证能看到通用选项" --platform ios

# ── 项目管理 ──
argus new <target> --platform android --package com.x.y      # 脚手架新建 target（见「新建测试 target 流程」）
argus new <target> --platform browser --url https://x.com
argus list                                                   # 列所有 target
argus devices                                                # 列 iOS simulator
argus setup                                                  # 创建并启动 simulator

# ── Figma ──
argus figma frames <figma-url>
argus figma gen-tests <figma-url> -o tests.md
argus figma review <figma-url> --platform ios --screenshot app.png -o review.html
```

## Key configuration (.env)

```
PLATFORM=android                 # ios | android | browser

# LLM — 默认 OpenRouter + Gemini 2.5 Flash（聚合 OpenAI 兼容协议）
LLM_PROVIDER=openrouter
LLM_API_KEY=sk-or-v1-xxx
LLM_BASE_URL=https://openrouter.ai/api/v1
LLM_MODEL=google/gemini-2.5-flash   # 也可 anthropic/claude-sonnet-4.5 / openai/gpt-4o / ...
LLM_HTTP_REFERER=                # OpenRouter 归因头（可选）
LLM_X_TITLE=                     # OpenRouter 归因头（可选）

# Android
ANDROID_SERIAL=                  # 单设备时指定 serial，多设备走 --device 参数不用配
ANDROID_PACKAGE=com.example.app  # 被测包名（必填，覆盖 _reset 用；填你自己 App 的真实包名，别提交真包名进仓库）

# iOS
SIMULATOR_DEVICE_MODE=auto       # auto | simulator | device
SIMULATOR_DEVICE_TYPE=iPhone 16 Pro

# Browser
BROWSER_HEADLESS=false
BROWSER_VIEWPORT_WIDTH=1280
BROWSER_VIEWPORT_HEIGHT=720
SELENIUM_GRID_URL=               # 配 grid + -j N 才能并发

# Figma
FIGMA_TOKEN=

# Agent
AGENT_MAX_STEPS=20               # 兜底；正常路径走 step-driven 的 PER_STEP_SUB_ACTION_LIMIT=10
AGENT_STEP_DELAY=1.0

# Skills
SKILLS_ENABLED=loading_detector,keyboard_detector,scroll_map,element_marker,visual_diff,toast_detector
SKILLS_OCR_LANGS=ch_sim,en
SKILLS_OCR_GPU=false

# Android tap 吸附（默认开）：tap 前把落点修正到包含它的最小 clickable 节点中心；
# 对 Flutter（无 clickable 节点）天然 no-op。设 0/false 关闭。
TAP_SNAP_TO_CLICKABLE=1
```

## Behavior notes & known limits

### Step-driven loop 的硬约束（vs 旧的 generic 20-turn loop）

- `step_validator` 是**硬墙**，不是建议：
  - `current_step_index` 必须 +0 或 +1（不能跳跃 / 倒退）
  - `current_step_status=pass/fail` 必须带 `evidence`（≥15 字符 + 引用具体屏幕元素）
  - `fail_reason` 至少 10 字符
  - reject 理由直接喂回 LLM 的 next-decide call 让它自纠
- **不可视觉验证的断言禁止 PASS**：埋点 / 后端 API / 系统时间 / launcher icon badge / SharedPreferences / 系统通知抽屉 / 跨 App deeplink 都必须 fail，不许 LLM 用"假设通过 / 视觉无法验证但符合预期"蒙混
- 默认 `PER_STEP_SUB_ACTION_LIMIT=10`（每个 step 最多 10 次 LLM sub-action），`MAX_REJECTS_PER_STEP=3`（连续 3 次 validator reject 标 fail）
- `AGENT_MAX_STEPS` 是兜底，正常路径不会触发

### 已实现的稳定性机制

- **Dialog auto-dismiss**：每 turn 自动 dismiss Android runtime permission 弹窗 / "Dark Mode Is Here" overlay / iOS "Don't Allow"，case 不用写"如果弹了 X 就点 Y"的样板。专门测弹窗的 case 加 `**Disable dialog auto-dismiss**: yes` 关闭
- **Fixture 恢复指南**：`tests/<target>/_preconditions.md` auto-prepend 到 case，告诉 LLM 怎么从异常状态恢复到 Background Given 描述的前置态。极大降低 false fail
- **Planner + Healer**：每个 case 跑前 planner 拆剧本注入 hint；fail/timeout 后 healer 给 verdict 分类（case_outdated / app_bug / llm_misjudge / fixture / flaky）
- **多图历史上下文**：brain 每次决策带最近 1–3 张历史截图，按时间顺序，帮 LLM 直观感受变化
- **no_effect 检测**：每步执行后 visual_diff 算像素变化率 < 0.5% 标 `history[i].no_effect=True`，下一步 prompt 提示 LLM 调整策略
- **多设备动态调度**：`--device s1 s2 ...` 共享 case 队列 + 账号池自动绑定 + APK 并行安装 + adb connect 自愈
- **报告 evidence + logger case 标识**：报告渲染 evidence；logger 每行带 case ID，便于审查 LLM 是否谎报

### 坐标定位 & tap 准确性

- **tap 点不中的真因是分辨率/缩放没标定对，不是 VLM 的 y 轴系统偏差**。截图像素 → 设备像素的映射算错（尤其 `screencap` 出图尺寸 ≠ `wm size` 物理分辨率的设备，如三星 Override 分辨率）会让落点整体偏移。标定对了，视觉坐标就落中。~~"小元素 ~50px patch tokenization 硬伤"是错误说法，已废弃~~。
- **应对**：
  1. **先标定分辨率**：每台设备读一次 `wm size` + `screencap` 实际尺寸，算 `设备px/截图px` 比例；相等就 1:1，不等按比例换算。这是 tap 准确性的根。
  2. **Flutter App 纯视觉**：`uiautomator dump` 报 `ERROR: could not get idle state`、拿不到可用节点树 → 照截图看到的点，别去拿树（拿了也是空/白费）。
  3. **原生控件 App 才用 UI tree bounds 辅助**：dump 得到真实带 bounds 的树时，用 `clickable` 节点 bounds 中心比视觉更精确。下面 3 项都是**针对原生树**的既有机制，对 Flutter 天然 no-op：
     - `element_marker` skill 给元素画编号 + bounds 映射表
     - **tap 吸附（snap-to-clickable）**：`android.py` 执行 tap 前，若坐标落在某 `clickable=true` 节点 bounds 内，自动把落点修正到该节点（面积最小者）中心。可用 `TAP_SNAP_TO_CLICKABLE=0` 关闭，>55% 屏幕的大容器不吸附
     - **UI tree 简化**：`get_ui_tree()` 只返回原始树，LLM-facing 简化统一在 `brain.py:_compact_android_xml`（合并 clickable 父+文字子成一行、剥 resource-id 包名前缀、砍 class 噪音）
  4. Hints 段写方位（"右上角"、"X 按钮左边"）**不写精确像素坐标**

### 测试用例书写约定

- **每 Scenario 自包含**：依赖的前置态写进 Background；进入页面的操作明示（不要假设"已在 X 页面"）
- **Hints / 位置参考写方位不写坐标**："标题正下方"、"X 按钮右边"
- **Then 写成可独立验证的列表**：每条 bullet 一个断言点，便于 LLM 逐项核对
- **不可视断言必须打 `@skip-vision` tag 或改写**：埋点 / 后端调用类断言写进 case 会强制 fail（参考上面「硬约束」第 2 条）
- **模糊场景用 exploratory 模式**：UX 走查、找未明示 bug 时加 `Mode: exploratory`（.md 格式）或写在 Background 里（.feature）

## Key dependencies

- `openai` — LLM API client（OpenAI 兼容，含 OpenRouter / DashScope / Gemini / vLLM 等）
- `Pillow` — image processing
- `uiautomator2` — Android UI 控制（避开 IME 输入法）
- `idb` (fb-idb) — iOS Development Bridge
- `adb` (platform-tools) — Android Debug Bridge
- `selenium` — browser automation
- `xcrun simctl` — Apple simulator CLI
