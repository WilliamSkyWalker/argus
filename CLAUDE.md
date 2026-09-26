# CLAUDE.md

Guidance for Claude Code in this repo. Terse on purpose — read it all.

## What
Argus = 视觉驱动 AI QA agent，替代人工测试。喂 `.feature`(Gherkin) 或 `.md`(TDD) 用例 → 看屏(iOS/Android/Browser) → 决策 → 执行 → 自判 pass/fail。**纯视觉**：只看截图，不喂 UI 树。移动端统一 Appium，浏览器 Selenium。可选 Figma 集成(生成用例/视觉走查)。

## 🔒 铁律（违反过，务必守）
- **argus 主仓是 Public(GitHub)**。写进代码/注释/docstring/README/CLAUDE.md/示例的一切都**必须脱敏**：禁止真实包名/bundle id、产品名、Apple team-id、公司名、真账号/邮箱、API key/token。一律用占位符：`com.example.app` / `你的 team id` / `${EMAIL}` / 通用描述。commit **message 本身**也脱敏。
- 真值只放 **gitignored** 文件：`.env` / `tests/<t>/_accounts.json` / `.argus/mcp_clients.json` —— 从未入库，保持如此。
- `tests/` 目录被 argus 的 .gitignore 排除。**测试用例是独立私有仓**(嵌套在 `tests/<target>/`，自己的 .git，push 用普通 `git push`，别 force)——它是客户内容不适用上面的公开脱敏，但**别和 argus 主仓搞混**。
- argus 主仓：直接 commit 到 `main`，message 简洁；commit 尾 `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`。

## 通用多资源 Runtime（首版）

`argus/runtime/` 与 QA 引擎独立，CLI 为 `argus workflow`，格式和限制见 `docs/runtime.md`。
支持显式 JSON 步骤、多资源顺序操作、SQLite 只读查询、人工接管与持久化恢复；尚无自然语言规划。
- 所有写入设备的动作先记 durable intent，再派发，再原子保存结果和游标。异常/中断且结果不确定 → `needs_review`，禁止自动重放。
- `human` 要声明后续 `verify_step`；resume 只确认交还控制权，重新观察后仍要执行验证。
- OS 锁保护执行进程，SQLite 资源归属跨人工暂停保留；现有 `argus device`/QA 入口尚未纳入这套锁。
- 测试：`python3 -m unittest discover -s tests/runtime_demo -v`。用模拟视觉驱动和真实 SQLite 离线验证，不声称完成真机测试。

## 架构（数据流）
用例 → `gherkin.py` 解析 → `render_case()`(step + metadata) → `planner.py`(1 LLM call/case，拆 intent/expected/hint) → `agent.py` step 主循环 → fail/timeout/error 时 `healer.py`(根因五分类) → `report.py`(HTML+base64截图)。

`agent.py` 按 Scenario step 推进。当前 `PER_STEP_SUB_ACTION_LIMIT=-1`（禁用动作次数上限），`AGENT_MAX_STEPS=0`（默认禁用整个 scenario 的循环轮数上限）；主要保护是 `MAX_TURNS_WITHOUT_PROGRESS=15`。预算内主动 wait 与 probe 轮询不计入该无进展计数。普通路径只有当前 step 判 pass 才由框架推进；`current_step_index` 必须严格等于待执行步骤，LLM 不得自行 +1。pass/fail 必带 evidence（≥15 字符且引用屏幕元素），fail 还需 fail_reason（≥10 字符），in_progress 必带 action。连续 3 次校验拒绝判 fail；拒绝不执行动作、不耗 sub-action 配额，但计入循环轮数和无进展计数。

连续断言合并使用独立的 `validate_assertion_batch()`，逐条检查 verdict/evidence/where、重复证据和负向断言；整块通过才一次推进多步，probe 步不参与合并。证据校验是文本启发式检查，不会独立验证截图内容，不能保证模型没有误判。不可视断言禁 PASS 是模型提示约束；要做代码层验证需使用 probe。

`healer.py` 在 fail/timeout/error 后提供根因分类与建议，附加到报告，不自动修复用例或应用，也不改写原测试结果。

`brain.py` LLM 决策：发**原始截图** + 最近 1-3 张历史截图 + planner hint + step 列表 + 已过 step 的 evidence 锚点 + 上次 reject 理由 → 返回带 `step_progress` 的 JSON。**不可视觉验证的断言禁 PASS**(埋点/后端/系统时间/通知抽屉/跨App deeplink → 必须 fail，不许「假设通过/推断成立」蒙混)。

**分级模型 / 元素定位 / 多帧断言 / 参考图**（借鉴 midscene）：
- **分级模型**：`LLM_MODEL_BRAIN/PLANNER` 留空时回落 `LLM_MODEL`；`LLM_MODEL_LOCATOR` 留空时关闭定位。Locator 端点/密钥留空则复用主 LLM 配置。
- **元素定位**(`locator.py`，默认关)：配置 `LLM_MODEL_LOCATOR` 后，普通路径中带 `target` 的 tap/long_press 在执行前定位，成功则替换 Brain 坐标，失败则沿用原坐标。也供分层执行定位目标。旧的像素差 no_effect 检测已停用，对应 `AGENT_LOCATE_RETRY` 与网格升级路径目前不会由该检测触发。「grounding」保留指更大的定位策略，不作为定位小模型的别名。
- **稳定帧与多帧断言**：默认 `AGENT_SETTLE_ENABLED=true`，`settle.py` 用像素差、状态栏 mask 和超时采样；操作决策用末帧，断言按窗口变化量选择静态 1 帧或动态最多 3 帧。`AGENT_ASSERT_BURST_FRAMES=3` 用于关闭 settle 或缺少窗口帧时的回退路径；采样不保证捕获所有短暂提示。
- **参考图断言**(默认关)：case 声明 `@ref:<path>` / `# argus-ref:` → 渲染成绝对路径 → brain 拿设计稿做视觉走查对比。供 Figma 走查。

## 执行优化（当前实现）

- `AGENT_WAIT_MAX_S=45`：每步累计主动等待预算；预算内 wait 轮不计无进展，耗尽后恢复计数，不是整个步骤的 45 秒超时。
- `AGENT_MERGE_ASSERTS=true`：连续断言同步合并；失败时可在现场识别、关闭拦截弹窗并重新判定。
- `AGENT_SPLIT_ACT_CHECK=false`：可选操作步批量执行。开启后 Brain 看截图拆原子动作序列，Locator 按需定位执行，下一轮 Brain 验证步骤；连续两次序列执行失败回退普通路径。不是零大模型调用，也不以 visual-diff 作为动作成功证明。

## 纯视觉（本次大改，记牢）
- **不喂 UI 树给 LLM**。树逻辑全删(无 snap-to-clickable / element_marker / dialog_dismisser / _compact_xml)。决策只靠截图。
- **坐标用百分比**：LLM 出 `x_pct/y_pct`(0-100)，`brain._pct_to_px` 换算成像素。实测 20+ VLM：问%定位准(σ~2%)且分辨率无关，问绝对像素会偏~7%(小按钮点空)。prompt **不喂分辨率**。
- 截图纯 driver(`get_screenshot_as_png`)。**设备必须解锁**(锁屏=FLAG_SECURE 截不了)。不用 adb 截图(云真机没 adb)。

## 平台（`argus/platforms/`）
- `base.py` 抽象接口(screenshot/tap/swipe/input_text/press_key/is_ime_visible…)。
- `appium.py` **iOS+Android 统一驱动**：`AppiumServerManager` 自动起 server(带 ANDROID_HOME、锁定装了 appium 的 node)；os 由 `config["appium"]["os"]` 选 xcuitest/uiautomator2。`create_platform("ios"/"android"/"appium")` 全 → AppiumPlatform。
- **mjpeg 帧流截图**(`platforms/mjpeg.py`，默认开)：起 session 时开 driver `mjpegServerPort`，`screenshot_raw` 从常驻流取最新帧(JPEG→PNG)省 HTTP 往返；取不到无条件 fallback 到 `get_screenshot_as_png`(故只快不错，云 appium 不暴露端口时自动降级)。`APPIUM_MJPEG_*` 控。
- `browser.py` 保留 Selenium QA/local/Grid。新 Runtime 的 browser 默认 `browser_playwright.py`，CDP 接入常驻 Chromium，按 target ID 绑定页面；关闭控制器只断开，不关 Chrome。CLI `device start --backend playwright`、`pages/select-page/close-page`；详见 `docs/browser.md`。测试 `tests/browser_demo`（真实浏览器测试需 `ARGUS_TEST_CHROME`）。
- 桌面：`desktop_mac.py` / `desktop_win.py` 是前台窗口级驱动；WSL 显式选 `PLATFORM=windows`，由 `windows_runner.py` + PowerShell/Win32 runner 操作宿主。`PLATFORM=desktop` 仅在原生 Windows 选 Windows 驱动，其余系统选 macOS。桌面通过环境变量配置；`run --platform` 当前只接受 ios/android/browser/rdp。
- `rdp.py` 为实验性远程 Windows 驱动，尚不应视为稳定接口。
- **文字输入**：Android 走 `mobile: type`(经 UnicodeIME，cap `unicodeKeyboard:true`+`resetKeyboard:true`，`io.appium.settings` 提供)——原生 EditText 与 Flutter 自绘都通吃(ACTION_SET_TEXT 对 Flutter 无效)。iOS 聚焦元素 send_keys。
- **iOS 签名**：Appium 走 xcodebuild/CoreDevice(非 go-ios 隧道)自动签 WDA，需 `IOS_TEAM_ID` + Xcode 登录该 team + 设备在其开发列表 + login 钥匙串解锁(codesign)。`android.py`/`ios.py`/`hands.py` 旧驱动已删。

## Skills（`argus/skills/`，截图→LLM 间预处理，默认开的都不碰树）
`loading_detector`/`keyboard_detector`(靠 platform.is_ime_visible)/`scroll_map`/`visual_diff`(也供 agent 判 no_effect)/`toast_detector`；按需：`ocr`/`color_validator`/`layout_checker`/`smart_crop`。

## Probes（`argus/probes/`，非视觉断言插件 —— 埋点/后端落库/上报日志）
纯视觉判不了的断言开的**代码层**通道，铁律「不可视断言禁 PASS」由此有了正解（不是放宽）：
- 用例在某 Then 下面挂 `# argus-probe: <name> k=v` → **该 step 的 verdict 由插件决定**，agent 不调用 Brain 对它做视觉裁决。完整用例和步骤列表仍可能传给 Planner/Brain，因此这不是数据隔离机制。
- **用例只写意图**（`check=首页曝光`），真实事件名/表名/期望属性在插件 config 里映射 —— 改埋点方案时用例零改动。
- **三态 verdict**：`pass`/`fail`/`inconclusive(+retry_after_s)`。埋点批量上报有分钟级延迟，**查太早的 0 行不算证据**（我据此造过假 bug），argus 按 probe 节奏轮询到 `PROBE_TIMEOUT_S`(默认 300s) 才判 fail；重试轮不计 no-progress（同 wait 语义）。插件抛异常/子进程崩 = inconclusive 继续重试，预算耗尽 fail 并把 error 写报告，**绝不因「查不了」放过断言**。
- 两种形态：**Python 类**（继承 `probes.base.Probe`，实现 `check(ctx,args)`）/ **子进程**（stdin JSON → stdout JSON，任何语言、依赖不污染 argus 环境）。
- 注册表 `.argus/probes.json`（gitignored，同 mcp_clients.json 一档；`.example` 入库，`${VAR}` 展开）。**插件代码放 `tests/<target>/probes/`**（客户私有仓；事件名/表名/查询逻辑不进主仓）。示例见 `tests/_template/probes/*.example`。
- ctx 给插件的锚点：`case_started_at/step_started_at/now`(时间窗) + `account`(多设备按 worker 绑) + `device/app_package/platform` + `attempt/elapsed_s/timeout_s` + `session`(同 case 内缓存) + `artifacts_dir`。
- **偏置跑法**：`argus run --skip-probes`(probe step 标 **skip 不是 pass**，reason 点名哪几步没验证 + 结果带 `probes_skipped_steps`，防静默全绿) / `--only-probes`(**case 级筛选**：只跑声明了 probe 的 case，其 UI 步照跑——埋点得靠操作触发；纯查库用 `argus probes check`)。走 env `PROBES_MODE=skip|only`，故 `--bg`/多设备 worker/MCP `run_target(probes=…)` 全通。
- 边界：一 step 一条 probe（多断言拆多 Then）；probe 步自动被排除出「连续断言合并」块；probe 名没注册 = fail（要跳过用 `@manual`）。CLI 调试 `argus probes list` / `argus probes check <name> k=v --target t --wait`。全文 `docs/probes.md`。

## 对其他 agent 开放：CLI 是通用接口
**任何能跑 shell 的 agent 都用 `argus` CLI 驱动**（不限 Claude Code）。两类能力：
- **编排**：`argus run/list/status/report`；`list` 带 `--json`，`device list` 默认 JSON、`run --report` 出 JSON，供机读。
- **设备驱动**：`argus device <start|screenshot|tap|swipe|input|type-send|key|launch|stop>`，每条输出 JSON。跨进程复用同一常驻 Appium session（`platforms/device_session.py` 按 session_id 重连；server 由 AppiumServerManager 常驻），故 turn-by-turn 驱动不必每次重建 session。全程 Appium 原语，**不碰 adb**。

## MCP（`argus/mcp/`，给 MCP-native agent 的可选适配器）
`server.py` FastMCP stdio，把上面同一套能力也暴露为 MCP tool(list/run/device_*；device_* 与 `argus device` CLI **共享同一 session 状态文件**，可互相接管)。根目录 `.mcp.json` 让 Claude Code 自动挂载。`client.py` 让 brain 调外部 server(如 Figma MCP)，配置 `.argus/mcp_clients.json`(gitignored)。`/argus-drive` skill = Claude 当 brain、`argus device` CLI 当 platform 跑用例出 HTML 报告。
**tool profile**：`--profile device` / `ARGUS_MCP_PROFILE=device` 只留 device_*+设备管理(11 个，摘掉跑测/报告 8 个)，这一档不需要 LLM key/tests/。新增跑测类 tool 必须登记进 `_RUN_PROFILE_ONLY`，否则启动自检报错。
**mcp SDK 1.x/2.x 都要能跑**：2.0 把 `mcp.server.fastmcp.FastMCP` 改名成 `mcp.server.MCPServer`，server.py 用 try/except 两版兼容（只依赖 `tool()`/`remove_tool()`/`run()`，签名一致）。本机全局装的是 1.27，**但 `pip install` 默认拿 2.x** —— 只在本机测等于没测到 2.x，改 MCP 相关代码要用一次性 venv 双版本各跑一次握手。

## Claude Code 插件（`plugins/`，本仓同时是 marketplace）
根 `.claude-plugin/marketplace.json`(marketplace 名 **`argus-plugins`**，故 install id 是 `argus-device@argus-plugins`) + `plugins/argus-device/`。装法 `/plugin marketplace add WilliamSkyWalker/argus`。
- **只有 `argus-device` 一个插件**：MCP server key `argus`(profile=device) + skill `device` + command `doctor`。**命名铁律：skill/command/server 名字里不再重复插件名** —— Claude Code 会自动加 `<插件名>:` 前缀，重复会得到 `/argus-device:argus-doctor` 这种三连。用户看到的是 `/argus-device:doctor`、`/argus-device:device`。
- 跑用例/出报告**刻意不做成插件**（要 LLM key + `tests/`）——走 clone 后的 `argus run` 或 `/argus-drive` skill。曾有过 `argus-runner` 插件，已按"只留 Claude 当 brain 那条"删掉。
- **插件缓存只拷插件目录自身**(`../..` 引用不到 argus 包)，所以 `scripts/argus_mcp.py` 运行时按 `ARGUS_HOME` → cwd 往上找 → 已装包 三级解析。
- 对外文案(plugin.json/README/skill/命令)一律**英文**(插件目录面向全球用户)；`.claude-plugin/marketplace.json` 的 plugin `source` 必须写显式 `./plugins/<name>`(`metadata.pluginRoot`+裸 source 当前 CC 版本不支持，装不上)。
- 真机验证插件（当前会话装完也拿不到新 MCP tool，必须新会话）：`cd <非 argus 目录> && ARGUS_HOME=<repo> claude -p "<任务>" --plugin-dir <repo>/plugins/argus-device --allowedTools "mcp__plugin_argus-device_argus__device_screenshot,…"` —— 插件 MCP tool 名格式是 `mcp__plugin_<插件名>_<server名>__<tool>`。
- 改完必跑 `claude plugin validate .` + `plugins/argus-device`(它连 SKILL.md frontmatter 一起校验：**英文 description 里有 `: ` 必须加引号**，否则 YAML 解析失败、metadata 静默丢空)。
- 提交到 Anthropic 官方目录**不走 PR**(`claude-plugins-community` 的 PR 自动关)，走表单 `clau.de/plugin-directory-submission`。

## 用例格式
**`.feature`(Gherkin，推荐)**：Feature/Background/Scenario(Outline)+Examples 全解析。Tag：
- `@P0/@P1/@P2` 优先级；`@auto/@partial/@manual`(后两个自动 skip)；`@skip/@wip` 跳过。
- **平台标签是集合，可扩展，不用 both**：`@android`/`@ios`/`@browser`/`@mac`/`@windows`/`@desktop`/`@rdp` 等；`@android @ios` 表示允许这两个平台。标签只筛选用例，不切换驱动、不自动发起双端运行。跑测平台不在集合里则 skip。
- `@TC-XXX` case ID；`@reset:pm_clear|relaunch|none` Android 重置(覆盖 feature 级 `# argus-reset-default`)。
- 文件头信息元数据：`# argus-target/platform/package/reset-default`(值行别写行内 `#` 注释)。

**`.md`(TDD)**：`### TC-XXX` 块 + `- **Priority/Reset before/Platform/Mode/Steps**`。

## Per-target 文件（`tests/<target>/`，此目录整体 gitignored/独立仓）
- `cases/*.feature|*.md` 用例；`_preconditions.md`(auto-prepend，教 LLM 从异常态恢复到 Background 前置态——降 false-fail 最关键)；`_accounts.json`(账号/密钥池，`${EMAIL}`/`${PASSWORD}` 占位，**别 commit 真账号**，多设备按 worker i 绑 accounts[i])；`reports/<ts>/*.html`。
- 用例约定：**自包含**(前置态进 Background，进子页操作明示，不依赖前置 case)；Hints 写方位不写坐标；Then 拆成逐条可验证 bullet；不可视断言改写或 skip。

## Setup / CLI
```bash
pip3 install -r requirements.txt                        # Python 3.11+ 依赖
python3 -m argus.cli mcp init --skip-ios                # Android 沙盒工具链；iOS 去掉 --skip-ios
python3 -m argus.cli init        # 生成 .env，填 LLM_API_KEY
alias argus="python3 -m argus.cli"

argus run <target>                       # 整目录递归
argus run <target>/mod/foo.feature       # 单文件
argus run <target> --device s1 s2 --apk app.apk --report   # 多设备调度(共享队列+账号池绑定+并行装APK)
argus run <target> --shard 0/3           # 手动分片
argus run <target> --bg ; argus status [run_id]            # 后台+查
argus run "打开X验证Y" --platform ios    # inline
argus new <t> --platform android --package com.example.app # 脚手架
argus list --json / argus device list / argus device install --platform ios --boot                 # 发现(机读)
# 设备驱动原语(任何 agent 可调，输出 JSON，跨进程复用 session：Appium 移动端 / Selenium 浏览器)
argus device start --serial s1                              # 建/复用 session
argus device screenshot --serial s1 --out shot.png         # → {path,screen_size,scale}
argus device tap 540 1260 --serial s1                      # 设备像素坐标
argus device type-send "hi" --input-x 540 --input-y 2400 --send-x 1000 --send-y 2400
argus device launch com.example.app --force-stop --serial s1   # Appium activate(无 adb)
argus device stop --serial s1
# 同一套原语也驱动浏览器(Selenium)：start --platform browser 起常驻 Chrome(remote-debugging-port，
# 跨进程按 debuggerAddress 重连，同 Appium 模型)；navigate 打开 URL 并截图；tap/swipe/input/key 复用。
# 视口/无头由 env 控:ARGUS_BROWSER_HEADLESS/_W/_H；Chrome 路径 ARGUS_CHROME_BIN。
argus device start --platform browser --serial web1
argus device navigate "https://example.com" --serial web1 --out page.png
argus figma frames|gen-tests|review <url>
# 非视觉断言插件(埋点等)：list 逐个试加载；check 不起设备单发调试(退出码 0=pass)
argus probes list --json
argus probes check <name> check=<意图> --target <t> --since-s 600 --wait
argus run <t> --skip-probes    # 跳过埋点检查(标 skip 不标 pass)
argus run <t> --only-probes    # 只跑有埋点断言的 case(UI 步照跑)
```

## .env 关键项（真值在此，勿入库）
```
PLATFORM=android|ios|browser|mac|windows|desktop|rdp
LLM_PROVIDER=openrouter  LLM_API_KEY=…  LLM_BASE_URL=https://openrouter.ai/api/v1  LLM_MODEL=google/gemini-2.5-flash
LLM_MODEL_BRAIN=  LLM_MODEL_PLANNER=  LLM_MODEL_LOCATOR=   # 分级模型，空→回落 LLM_MODEL(locator 空=关)
LLM_LOCATOR_BASE_URL=  LLM_LOCATOR_API_KEY=   # 定位模型独立端点(空→复用主 LLM)
ANDROID_PACKAGE=com.example.app   # 被测包名(必填)；填真值别提交
APPIUM_DEVICE=  APPIUM_SERVER_URL=  # 设备 udid/serial；空则默认
APPIUM_MJPEG_ENABLED=true  APPIUM_MJPEG_PORT=  APPIUM_MJPEG_QUALITY=90   # mjpeg 帧流截图(多设备需各给端口)
AGENT_MAX_STEPS=0   # scenario 循环轮数上限，<=0 禁用
AGENT_SETTLE_ENABLED=true  AGENT_WAIT_MAX_S=45  AGENT_MERGE_ASSERTS=true
AGENT_SPLIT_ACT_CHECK=false
AGENT_ASSERT_BURST_FRAMES=3  AGENT_LOCATE_RETRY=2   # 回退路径多帧数 / 已停用 no_effect 检测的旧阈值
IOS_TEAM_ID=  IOS_WDA_BUNDLE_ID=com.example.wda  IOS_BUNDLE_ID=   # iOS 真机签名
PROBES_CONFIG=  PROBE_TIMEOUT_S=  PROBE_POLL_INTERVAL_S=   # 非视觉断言插件(空→注册表 defaults / 300s / 20s)
PROBES_MODE=all|skip|only    # = argus run --skip-probes / --only-probes
BROWSER_HEADLESS / VIEWPORT_* / SELENIUM_GRID_URL ; FIGMA_TOKEN ; SKILLS_ENABLED
```

## 已知限制 / 行为
- 坐标不准的头号真因是**分辨率标定**(截图px↔设备px 换算)；标定对了 + 用百分比协议基本落中。
- 不可视断言(埋点/后端/系统时间/launcher badge/通知抽屉/跨App)**视觉层**一律 fail，不许蒙混；要真验证挂 probe 插件(见上)，那条 step 由插件裁决。
- iOS 真机若非专用设备(如私人手机)会反复 `unavailable`(锁屏/休眠/拔线)——按需插+解锁，规模化用专用设备或云真机(云上 iOS 只有 Appium 一条路，且免签名代管)。
- 依赖：`openai`(OpenAI 兼容 LLM) / `Pillow` / `uiautomator2` / `selenium` / Appium(server+drivers)。

## Existing browser extension backend

`extensions/argus-browser/` + `argus/browser_bridge.py` + `platforms/browser_extension.py`：Native Messaging + 私有共享目录 IPC，Windows host 可与 WSL Argus 通信，不监听网络端口。默认当前浏览器配置文件内所有 HTTP(S) 标签页（含新弹窗），操作目标仍显式选择；browser session UUID + tab ID 防重启误选；不提取 DOM。Runtime browser resource 必须写 `backend: extension`；先安装桥，再用 `device connect` 连接，见 `docs/browser-extension.md`。仅能声称已执行的平台测试，Windows/WSL 桥接、导航和截图已实机验证。测试：`tests/extension_demo`，可选 `ARGUS_TEST_CHROME`。

## Mobile discovery and simulator provisioning

`argus/mobile.py` implements mobile adapters behind `device list/connect/install/boot`. Discovery covers
adb devices, devicectl physical iOS devices and simctl simulators. Explicit IDs
and persistent session aliases are separate. Local iOS requires full Xcode on
macOS; remote devices use an explicit Appium URL and ID. Installer never force
overwrites AVDs; driver installation failures must propagate. SDK licensing is
interactive unless `--accept-licenses` is explicitly supplied. adb is used only
for discovery/lifecycle here, not visual control. Tests: `tests/mobile_demo`;
see `docs/mobile.md` for platform requirements and actual validation coverage.

`mobile_host.py` selects install/boot hosts and performs a read-only preflight.
WSL without usable KVM defaults to Windows; `--host local/windows` overrides it.
`--dry-run` must not install anything. Android SDK consent precedes downloads;
noninteractive installs require `--accept-licenses`. Only stdlib worker files are
copied to Windows, never project configuration or credentials. Windows Appium is
loopback-only; `mobile_relay.py` routes WSL HTTP via the Windows worker's stdio,
with tokenized local URLs and no automatic replay on ambiguous transport failure.
Do not silently enable Windows features or reboot. Installer verification uses a
real screenshot after connecting; mocked host tests are not emulator validation.
Windows tools must use `toolchain.background_options()`; private Appium Node
processes preload `windows_no_console.cjs` so adb/logcat never open consoles.
Use a private Windows adb port, propagated via Appium's `adbPort`, to avoid WSL
forwarding port 5037. WSL-to-Windows API 35 installation, screenshot, Settings
launch, tap and text input have been exercised; iOS remains mock-tested only.

## Unified control

`argus/control.py` extends `argus device` with list/connect/sessions/disconnect,
install/boot and common open/scroll commands. `--session` aliases `--serial`.
Desktop bindings persist app+OS and reconnect short-lived controllers; Runtime
accepts desktop `session` resources as well as inline `app`. Disconnect preserves
endpoint configuration and blocks subsequent actions until explicit reconnect;
it is not cross-process cancellation or global extension revocation. Tests:
`tests/control_demo`. User guide: `docs/control.md`.
