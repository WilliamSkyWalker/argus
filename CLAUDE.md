# CLAUDE.md

Guidance for Claude Code in this repo. Terse on purpose — read it all.

## What
Argus = 视觉驱动 AI QA agent，替代人工测试。喂 `.feature`(Gherkin) 或 `.md`(TDD) 用例 → 看屏(iOS/Android/Browser) → 决策 → 执行 → 自判 pass/fail。**纯视觉**：只看截图，不喂 UI 树。移动端统一 Appium，浏览器 Selenium。可选 Figma 集成(生成用例/视觉走查)。

## 🔒 铁律（违反过，务必守）
- **argus 主仓是 Public(GitHub)**。写进代码/注释/docstring/README/CLAUDE.md/示例的一切都**必须脱敏**：禁止真实包名/bundle id、产品名、Apple team-id、公司名、真账号/邮箱、API key/token。一律用占位符：`com.example.app` / `你的 team id` / `${EMAIL}` / 通用描述。commit **message 本身**也脱敏。
- 真值只放 **gitignored** 文件：`.env` / `tests/<t>/_accounts.json` / `.argus/mcp_clients.json` —— 从未入库，保持如此。
- `tests/` 目录被 argus 的 .gitignore 排除。**测试用例是独立私有仓**(嵌套在 `tests/<target>/`，自己的 .git，push 用普通 `git push`，别 force)——它是客户内容不适用上面的公开脱敏，但**别和 argus 主仓搞混**。
- argus 主仓：直接 commit 到 `main`，message 简洁；commit 尾 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## 架构（数据流）
用例 → `gherkin.py` 解析 → `render_case()`(step + metadata) → `planner.py`(1 LLM call/case，拆 intent/expected/hint) → `agent.py` step 主循环 → fail/timeout 时 `healer.py`(根因五分类) → `report.py`(HTML+base64截图)。

`agent.py` step-driven：外层迭代 Scenario step，内层每 step ≤ `PER_STEP_SUB_ACTION_LIMIT=10` 次 LLM sub-action，必须 LLM 出 `current_step_status=pass` 才推进。`step_validator.py` 硬墙：`current_step_index` 只能 +0/+1(禁跳/退)、pass/fail 必带 `evidence`(≥15 字+引用具体屏幕元素)、`in_progress` 必带 action；reject 不耗配额(连续 `MAX_REJECTS_PER_STEP=3` 判 fail)、reject 理由喂回 LLM 自纠。

`brain.py` LLM 决策：发**原始截图** + 最近 1-3 张历史截图 + planner hint + step 列表 + 已过 step 的 evidence 锚点 + 上次 reject 理由 → 返回带 `step_progress` 的 JSON。**不可视觉验证的断言禁 PASS**(埋点/后端/系统时间/通知抽屉/跨App deeplink → 必须 fail，不许「假设通过/推断成立」蒙混)。

## 纯视觉（本次大改，记牢）
- **不喂 UI 树给 LLM**。树逻辑全删(无 snap-to-clickable / element_marker / dialog_dismisser / _compact_xml)。决策只靠截图。
- **坐标用百分比**：LLM 出 `x_pct/y_pct`(0-100)，`brain._pct_to_px` 换算成像素。实测 20+ VLM：问%定位准(σ~2%)且分辨率无关，问绝对像素会偏~7%(小按钮点空)。prompt **不喂分辨率**。
- 截图纯 driver(`get_screenshot_as_png`)。**设备必须解锁**(锁屏=FLAG_SECURE 截不了)。不用 adb 截图(云真机没 adb)。

## 平台（`argus/platforms/`）
- `base.py` 抽象接口(screenshot/tap/swipe/input_text/press_key/is_ime_visible…)。
- `appium.py` **iOS+Android 统一驱动**：`AppiumServerManager` 自动起 server(带 ANDROID_HOME、锁定装了 appium 的 node)；os 由 `config["appium"]["os"]` 选 xcuitest/uiautomator2。`create_platform("ios"/"android"/"appium")` 全 → AppiumPlatform。
- `browser.py` Selenium(local/Grid，回写实际 viewport 校正坐标)。
- **文字输入**：Android 走 `mobile: type`(经 UnicodeIME，cap `unicodeKeyboard:true`+`resetKeyboard:true`，`io.appium.settings` 提供)——原生 EditText 与 Flutter 自绘都通吃(ACTION_SET_TEXT 对 Flutter 无效)。iOS 聚焦元素 send_keys。
- **iOS 签名**：Appium 走 xcodebuild/CoreDevice(非 go-ios 隧道)自动签 WDA，需 `IOS_TEAM_ID` + Xcode 登录该 team + 设备在其开发列表 + login 钥匙串解锁(codesign)。`android.py`/`ios.py`/`hands.py` 旧驱动已删。

## Skills（`argus/skills/`，截图→LLM 间预处理，默认开的都不碰树）
`loading_detector`/`keyboard_detector`(靠 platform.is_ime_visible)/`scroll_map`/`visual_diff`(也供 agent 判 no_effect)/`toast_detector`；按需：`ocr`/`color_validator`/`layout_checker`/`smart_crop`。

## MCP（`argus/mcp/`）
`server.py` FastMCP stdio，暴露 argus 能力为 tool(list/run/device 原语；device_* 走 AppiumPlatform 供 argus-drive)。根目录 `.mcp.json` 让 Claude Code 自动挂载。`client.py` 让 brain 调外部 server(如 Figma MCP)，配置 `.argus/mcp_clients.json`(gitignored)。`/argus-drive` skill = Claude 当 brain、Appium 当 platform 跑用例出 HTML 报告。

## 用例格式
**`.feature`(Gherkin，推荐)**：Feature/Background/Scenario(Outline)+Examples 全解析。Tag：
- `@P0/@P1/@P2` 优先级；`@auto/@partial/@manual`(后两个自动 skip)；`@skip/@wip` 跳过。
- **平台标签是集合，可扩展，不用 both**：`@android`/`@ios`/`@browser`(以后 `@web`/`@desktop`)。挂哪个就在哪个平台跑，`@android @ios` = 两端都跑。跑测平台不在集合里则 skip。
- `@TC-XXX` case ID；`@reset:pm_clear|relaunch|none` Android 重置(覆盖 feature 级 `# argus-reset-default`)。
- 文件头信息元数据：`# argus-target/platform/package/reset-default`(值行别写行内 `#` 注释)。

**`.md`(TDD)**：`### TC-XXX` 块 + `- **Priority/Reset before/Platform/Mode/Steps**`。

## Per-target 文件（`tests/<target>/`，此目录整体 gitignored/独立仓）
- `cases/*.feature|*.md` 用例；`_preconditions.md`(auto-prepend，教 LLM 从异常态恢复到 Background 前置态——降 false-fail 最关键)；`_accounts.json`(账号/密钥池，`${EMAIL}`/`${PASSWORD}` 占位，**别 commit 真账号**，多设备按 worker i 绑 accounts[i])；`reports/<ts>/*.html`。
- 用例约定：**自包含**(前置态进 Background，进子页操作明示，不依赖前置 case)；Hints 写方位不写坐标；Then 拆成逐条可验证 bullet；不可视断言改写或 skip。

## Setup / CLI
```bash
pip3 install openai Pillow uiautomator2 selenium        # 依赖(不装 argus 本身)
# Appium(移动端): npm i -g appium@3 && appium driver install uiautomator2 xcuitest@latest  (用 Node LTS)
python3 -m argus.cli init        # 生成 .env，填 LLM_API_KEY
alias argus="python3 -m argus.cli"

argus run <target>                       # 整目录递归
argus run <target>/mod/foo.feature       # 单文件
argus run <target> --device s1 s2 --apk app.apk --report   # 多设备调度(共享队列+账号池绑定+并行装APK)
argus run <target> --shard 0/3           # 手动分片
argus run <target> --bg ; argus status [run_id]            # 后台+查
argus run "打开X验证Y" --platform ios    # inline
argus new <t> --platform android --package com.x.y         # 脚手架
argus list / devices / setup
argus figma frames|gen-tests|review <url>
```

## .env 关键项（真值在此，勿入库）
```
PLATFORM=android|ios|browser
LLM_PROVIDER=openrouter  LLM_API_KEY=…  LLM_BASE_URL=https://openrouter.ai/api/v1  LLM_MODEL=google/gemini-2.5-flash
ANDROID_PACKAGE=com.example.app   # 被测包名(必填)；填真值别提交
APPIUM_DEVICE=  APPIUM_SERVER_URL=  # 设备 udid/serial；空则默认
IOS_TEAM_ID=  IOS_WDA_BUNDLE_ID=com.example.wda  IOS_BUNDLE_ID=   # iOS 真机签名
BROWSER_HEADLESS / VIEWPORT_* / SELENIUM_GRID_URL ; FIGMA_TOKEN ; SKILLS_ENABLED
```

## 已知限制 / 行为
- 坐标不准的头号真因是**分辨率标定**(截图px↔设备px 换算)；标定对了 + 用百分比协议基本落中。
- 不可视断言(埋点/后端/系统时间/launcher badge/通知抽屉/跨App)一律 fail，不许蒙混。
- iOS 真机若非专用设备(如私人手机)会反复 `unavailable`(锁屏/休眠/拔线)——按需插+解锁，规模化用专用设备或云真机(云上 iOS 只有 Appium 一条路，且免签名代管)。
- 依赖：`openai`(OpenAI 兼容 LLM) / `Pillow` / `uiautomator2` / `selenium` / Appium(server+drivers)。
