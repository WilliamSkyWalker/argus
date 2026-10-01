# Agent development guide


Shared repository guidance for programming agents and human contributors.
Use this document with the client’s own instruction-loading mechanism; the filename
`agent.md` does not imply automatic discovery by every client.
End-user visual operation instructions live in
[`plugins/saygo-device/skills/device/SKILL.md`](plugins/saygo-device/skills/device/SKILL.md).

## What

Saygo 为外部编程 Agent 提供跨手机、浏览器和桌面窗口的视觉控制服务。CLI/MCP 共享命名会话、观察、动作和错误语义；`saygo task` 支持逐步提交、人工接管、持久化恢复和证据导出。
桌面版 `saygo/desktop/` 使用 Qt 6 / PySide6，用户配置视觉模型 API，决策循环复用 InteractiveRuntime。BDD QA 引擎仍提供 `.feature` / `.md` 用例运行、报告和 Figma 集成。
视觉决策使用截图，不向模型提供 UI 树或 DOM。MCP 和桌面版浏览器使用扩展后端；Playwright 保留在 CLI/Runtime，Selenium 保留用于 QA。

## 🔒 铁律（违反过，务必守）

- **saygo 主仓是 Public(GitHub)**。写进代码/注释/docstring/README/agent.md/示例的一切都**必须脱敏**：禁止真实包名/bundle id、产品名、Apple team-id、公司名、真账号/邮箱、API key/token。一律用占位符：`com.example.app` / `你的 team id` / `${EMAIL}` / 通用描述。commit **message 本身**也脱敏。
- 真值只放 **gitignored** 文件：`.env` / `tests/<t>/_accounts.json` / `.saygo/mcp_clients.json` —— 从未入库，保持如此。
- 私有 `tests/<target>/` 默认被 .gitignore 排除；公开 `*_demo` 和 `_template` 是例外。**客户测试用例是独立私有仓**(嵌套在 `tests/<target>/`，自己的 .git，push 用普通 `git push`，别 force)——它是客户内容不适用上面的公开脱敏，但**别和 saygo 主仓搞混**。
- saygo 主仓默认分支为 `main`；按用户授权执行提交和推送，message 简洁。不得添加虚假的模型身份或固定某个供应商的协作者署名。

## 统一操作服务与 Runtime


- `saygo/devices/service.py` 是 CLI/MCP 的共享操作入口；会话绑定由 `devices/control.py`、`platforms/device_session.py` 管理。不要在新入口复制平台分派和错误处理。
- `saygo/runtime/interactive.py` 支持动态任务；`saygo task` 和 MCP `agent_task` 使用同一实现。显式 JSON 工作流走 `saygo workflow`，独立于 QA 引擎。桌面自然语言任务由 `desktop/runner.py` 决策，不在 Runtime 内另建模型循环。
- 动作先保存 durable intent 再派发；中断且结果未知标记 `needs_review`，禁止自动重放。request ID 用于防止重复派发。核对后 `resolve`，新动作须重新观察。
- observations 带 ID、会话、目标身份、尺寸和坐标映射；优先用百分比坐标。图像/裁剪坐标必须绑定原观察，调用方无需自行算缩放。旧观察、目标区域或窗口身份变化必须拒绝或重新观察定位。
- `observe_after`、有超时的变化/稳定等待只返回观察事实；`business_success: null` 不能改写成业务成功。能力缺失、输入异常必须明确报错。
- 优先可见按钮和菜单，尽量不用快捷键。Windows 后台不支持的快捷键不得静默切换前台；前台操作必须显式选择。
- 手机、浏览器和桌面均支持 handoff/resume。暂停阻止自动操作，交还控制权后截图，业务完成仍须另行验证。显式 workflow 的 human 步还要执行声明的验证步骤。
- 任务/工作流在 idle、handoff、needs_review 期间保留资源归属；普通 device 命令遵守这套锁。所有本地桌面窗口共享前台输入资源。锁不能阻止人或其他软件改变焦点。QA 路径的锁覆盖不要未经核对就声称等同于共享 device 服务。
- 实际操作、异常、前后截图和接管事件由程序记录；Agent 解释单独存为 annotation。导出包可能包含应用数据、输入文本和截图。
- 默认状态在 `$SAYGO_HOME_DIR`（`~/.saygo`），任务在其 `runtime` 子目录；`SAYGO_RUNTIME_DIR` 可覆盖任务库。CLI/MCP 要共享相同配置。项目资源别名在 `.saygo/resources.json`。

详见 [Agent 操作](docs/agent-control.md)、[Runtime](docs/runtime.md)。离线测试使用模拟驱动和真实 SQLite，不代表完整跨端任务通过真机验收。

## 桌面版、安装与分发


- `desktop/app.py`：Qt UI、后台工作线程、连接与任务管理；`desktop/model.py`：OpenAI-compatible 视觉 API 和凭据；`desktop/runner.py`：每次一个动作的模型循环，复用 InteractiveRuntime。
- 不使用 pyenv。打包用户无需安装 Python；Windows 原生直连，不依赖 WSL。开发者按需安装 `desktop`、`windows`、`mac` 等 extras。
- Key 只在内存或支持的 OS keyring；不得退回明文保存。`desktop.json` 不含 Key。截图和任务内容会发送到用户配置的模型提供方。
- 配置优先级：defaults → `$SAYGO_HOME_DIR/config.env` → 当前项目 `.env` 或 `SAYGO_CONFIG_FILE` → 环境变量。显式配置文件缺失时报错，不扫描 site-packages 下的 `.env`。
- `scripts/install_agent_plugin.py` 安装托管 runtime、客户端接入和浏览器桥；支持 Claude/Codex/Qoder/QoderCN CLI。新增客户端共用操作 Skill，只保留接入差异。
- `scripts/build_release.py` 按白名单生成源码、带 SHA256 的版本化安装器和扩展 ZIP；不得混入 `.env`、本机会话、截图或客户用例。
- `scripts/build_desktop.py` 生成原生目录包；`scripts/package_desktop.py` 在目标 OS 生成 Windows ZIP / macOS DMG / Linux tar.gz 与校验和。`.github/workflows/desktop.yml` 手动构建，不自动发布。
- Windows x64 已通过 7 项 desktop 测试、打包 EXE 的 Qt 启动和 native-host 握手；Linux GUI 启动/打包已检查。macOS 脚本未实机验证；签名、公证、干净机器和完整模型业务任务仍待验收。不得将这些检查混称为全平台实测。

使用和验证边界见 [desktop](docs/desktop.md)、[distribution](distribution/README.md)。

## QA 架构（数据流）

用例 → `gherkin.py` 解析 → `render_case()`(step + metadata) → `planner.py`(1 LLM call/case，拆 intent/expected/hint) → `agent.py` step 主循环 → fail/timeout/error 时 `healer.py`(根因五分类) → `report.py`(HTML+base64截图)。

`agent.py` 按 Scenario step 推进。当前 `PER_STEP_SUB_ACTION_LIMIT=-1`（禁用动作次数上限），`AGENT_MAX_STEPS=0`（默认禁用整个 scenario 的循环轮数上限）；主要保护是 `MAX_TURNS_WITHOUT_PROGRESS=15`。预算内主动 wait 与 probe 轮询不计入该无进展计数。普通路径只有当前 step 判 pass 才由框架推进；`current_step_index` 必须严格等于待执行步骤，LLM 不得自行 +1。pass/fail 必带 evidence（≥15 字符且引用屏幕元素），fail 还需 fail_reason（≥10 字符），in_progress 必带 action。连续 3 次校验拒绝判 fail；拒绝不执行动作、不耗 sub-action 配额，但计入循环轮数和无进展计数。

连续断言合并使用独立的 `validate_assertion_batch()`，逐条检查 verdict/evidence/where、重复证据和负向断言；整块通过才一次推进多步，probe 步不参与合并。证据校验是文本启发式检查，不会独立验证截图内容，不能保证模型没有误判。不可视断言禁 PASS 是模型提示约束；要做代码层验证需使用 probe。

`healer.py` 在 fail/timeout/error 后提供根因分类与建议，附加到报告，不自动修复用例或应用，也不改写原测试结果。

`brain.py` LLM 决策：发**原始截图** + 最近 1-3 张历史截图 + planner hint + step 列表 + 已过 step 的 evidence 锚点 + 上次 reject 理由 → 返回带 `step_progress` 的 JSON。**不可视觉验证的断言禁 PASS**(埋点/后端/系统时间/通知抽屉/跨App deeplink → 必须 fail，不许「假设通过/推断成立」蒙混)。

**分级模型 / 元素定位 / 多帧断言 / 参考图**（借鉴 midscene）：

- **分级模型**：`LLM_MODEL_BRAIN/PLANNER` 留空时回落 `LLM_MODEL`；`LLM_MODEL_LOCATOR` 留空时关闭定位。Locator 端点/密钥留空则复用主 LLM 配置。
- **元素定位**(`locator.py`，默认关)：配置 `LLM_MODEL_LOCATOR` 后，普通路径中带 `target` 的 tap/long_press 在执行前定位，成功则替换 Brain 坐标，失败则沿用原坐标。也供分层执行定位目标。旧的像素差 no_effect 检测已停用，对应 `AGENT_LOCATE_RETRY` 与网格升级路径目前不会由该检测触发。「grounding」保留指更大的定位策略，不作为定位小模型的别名。
- **稳定帧与多帧断言**：默认 `AGENT_SETTLE_ENABLED=true`，`settle.py` 用像素差、状态栏 mask 和超时采样；操作决策用末帧，断言按窗口变化量选择静态 1 帧或动态最多 3 帧。`AGENT_ASSERT_BURST_FRAMES=3` 用于关闭 settle 或缺少窗口帧时的回退路径；采样不保证捕获所有短暂提示。
- **参考图断言**(默认关)：case 声明 `@ref:<path>` / `# saygo-ref:` → 渲染成绝对路径 → brain 拿设计稿做视觉走查对比。供 Figma 走查。

## 执行优化（当前实现）


- `AGENT_WAIT_MAX_S=45`：每步累计主动等待预算；预算内 wait 轮不计无进展，耗尽后恢复计数，不是整个步骤的 45 秒超时。
- `AGENT_MERGE_ASSERTS=true`：连续断言同步合并；失败时可在现场识别、关闭拦截弹窗并重新判定。
- `AGENT_SPLIT_ACT_CHECK=false`：可选操作步批量执行。开启后 Brain 看截图拆原子动作序列，Locator 按需定位执行，下一轮 Brain 验证步骤；连续两次序列执行失败回退普通路径。不是零大模型调用，也不以 visual-diff 作为动作成功证明。

## 视觉操作约束

- macOS 显式后台模式见 `platforms/desktop_mac_background.py`。模型仍只看截图；驱动内部可对已选坐标做应用内 AX 命中、窗口身份校验和有限祖先查询，以执行原生控件动作，不向模型提供树、控件文本或语义定位。该路径禁止激活/启动 App、全局鼠标键盘、剪贴板和前台回退。原生按键定向到已绑定进程且须验证其已有焦点控件归属；不支持的动作明确失败。锁屏、非当前桌面或身份不明的窗口拒绝操作。实机验证范围和命令见 `docs/control.md` 的 macOS background mode；不能将独立测试面板的通过宣称为全应用兼容。

- **不喂 UI 树给 LLM**。QA 感知与定位不使用树(无 snap-to-clickable / element_marker / dialog_dismisser / _compact_xml)。决策只靠截图；macOS 后台驱动内部的原生动作适配范围见上条。
- **优先百分比坐标**：QA 模型使用 `x_pct/y_pct`（0–100），`brain._pct_to_px` 换算；统一服务还支持像素、图像与裁剪空间。元数据中的图像尺寸和映射不能省略，不能假设截图像素等于设备逻辑坐标。
- 移动端视觉控制经 Appium（截图可用 mjpeg 帧流回退到 `get_screenshot_as_png`）。设备需可交互，锁屏或受保护页面可能无法截图。adb 用于发现和生命周期管理，不作为视觉控制的旁路。

## 平台（`saygo/platforms/`）

- `base.py` 抽象接口(screenshot/tap/swipe/input_text/press_key/is_ime_visible…)。
- `appium.py` **iOS+Android 统一驱动**：`AppiumServerManager` 自动起 server(带 ANDROID_HOME、锁定装了 appium 的 node)；os 由 `config["appium"]["os"]` 选 xcuitest/uiautomator2。`create_platform("ios"/"android"/"appium")` 全 → AppiumPlatform。
- **mjpeg 帧流截图**(`platforms/mjpeg.py`，默认开)：起 session 时开 driver `mjpegServerPort`，`screenshot_raw` 从常驻流取最新帧(JPEG→PNG)省 HTTP 往返；取不到无条件 fallback 到 `get_screenshot_as_png`(故只快不错，云 appium 不暴露端口时自动降级)。`APPIUM_MJPEG_*` 控。
- `browser.py` 保留 Selenium QA/local/Grid。CLI/显式 Runtime 的 managed browser 使用 `browser_playwright.py`，CDP 接入常驻 Chromium，按 target ID 绑定页面；关闭控制器只断开，不关 Chrome。CLI `device start --backend playwright`、`pages/select-page/close-page`；详见 `docs/browser.md`。测试 `tests/browser_demo`（真实浏览器测试需 `SAYGO_TEST_CHROME`）。
- 桌面：`desktop_mac.py` / `desktop_win.py` 是前台窗口级驱动；Windows `--background` 使用 PowerShell/Win32 runner，支持范围取决于控件，不支持操作必须报错。WSL 显式选 `PLATFORM=windows`，由 `windows_runner.py` + PowerShell/Win32 runner 操作宿主。`PLATFORM=desktop` 仅在原生 Windows 选 Windows 驱动，其余系统选 macOS。桌面通过环境变量配置；`run --platform` 当前只接受 ios/android/browser/rdp。
- `rdp.py` 为实验性远程 Windows 驱动，尚不应视为稳定接口。
- **文字输入**：Android 走 `mobile: type`(经 UnicodeIME，cap `unicodeKeyboard:true`+`resetKeyboard:true`，`io.appium.settings` 提供)——原生 EditText 与 Flutter 自绘都通吃(ACTION_SET_TEXT 对 Flutter 无效)。iOS 聚焦元素 send_keys。
- **iOS 签名**：Appium 走 xcodebuild/CoreDevice(非 go-ios 隧道)自动签 WDA，需 `IOS_TEAM_ID` + Xcode 登录该 team + 设备在其开发列表 + login 钥匙串解锁(codesign)。`android.py`/`ios.py`/`hands.py` 旧驱动已删。

## Skills（`saygo/skills/`，截图→LLM 间预处理，默认开的都不碰树）

`loading_detector`/`keyboard_detector`(靠 platform.is_ime_visible)/`scroll_map`/`visual_diff`（图像差异，不作业务成功证明）/`toast_detector`；按需：`ocr`/`color_validator`/`layout_checker`/`smart_crop`。

## Probes（`saygo/probes/`，非视觉断言插件 —— 埋点/后端落库/上报日志）

纯视觉判不了的断言开的**代码层**通道，铁律「不可视断言禁 PASS」由此有了正解（不是放宽）：

- 用例在某 Then 下面挂 `# saygo-probe: <name> k=v` → **该 step 的 verdict 由插件决定**，agent 不调用 Brain 对它做视觉裁决。完整用例和步骤列表仍可能传给 Planner/Brain，因此这不是数据隔离机制。
- **用例只写意图**（`check=首页曝光`），真实事件名/表名/期望属性在插件 config 里映射 —— 改埋点方案时用例零改动。
- **三态 verdict**：`pass`/`fail`/`inconclusive(+retry_after_s)`。埋点批量上报有分钟级延迟，**查太早的 0 行不算证据**（我据此造过假 bug），saygo 按 probe 节奏轮询到 `PROBE_TIMEOUT_S`(默认 300s) 才判 fail；重试轮不计 no-progress（同 wait 语义）。插件抛异常/子进程崩 = inconclusive 继续重试，预算耗尽 fail 并把 error 写报告，**绝不因「查不了」放过断言**。
- 两种形态：**Python 类**（继承 `probes.base.Probe`，实现 `check(ctx,args)`）/ **子进程**（stdin JSON → stdout JSON，任何语言、依赖不污染 saygo 环境）。
- 注册表 `.saygo/probes.json`（gitignored，同 mcp_clients.json 一档；`.example` 入库，`${VAR}` 展开）。**插件代码放 `tests/<target>/probes/`**（客户私有仓；事件名/表名/查询逻辑不进主仓）。示例见 `tests/_template/probes/*.example`。
- ctx 给插件的锚点：`case_started_at/step_started_at/now`(时间窗) + `account`(多设备按 worker 绑) + `device/app_package/platform` + `attempt/elapsed_s/timeout_s` + `session`(同 case 内缓存) + `artifacts_dir`。
- **偏置跑法**：`saygo run --skip-probes`(probe step 标 **skip 不是 pass**，reason 点名哪几步没验证 + 结果带 `probes_skipped_steps`，防静默全绿) / `--only-probes`(**case 级筛选**：只跑声明了 probe 的 case，其 UI 步照跑——埋点得靠操作触发；纯查库用 `saygo probes check`)。走 env `PROBES_MODE=skip|only`，故 `--bg`/多设备 worker/MCP `run_target(probes=…)` 全通。
- 边界：一 step 一条 probe（多断言拆多 Then）；probe 步自动被排除出「连续断言合并」块；probe 名没注册 = fail（要跳过用 `@manual`）。CLI 调试 `saygo probes list` / `saygo probes check <name> k=v --target t --wait`。全文 `docs/probes.md`。

## CLI / MCP / 客户端接入


所有能运行 shell 的 Agent 都可调用 `saygo`；安装后无需从仓库根运行。`saygo device` 提供连接、观察和动作，`saygo task` 提供交互任务，`saygo run` 提供 QA。不要混用它们的状态或结果语义。

`saygo-mcp --profile device`（或 `python3 -m saygo.mcp.server --profile device`）启动设备模式，不需模型 Key 或 tests 目录。以 `server.py` 中 `_DEVICE_PROFILE_KEEP` / `_RUN_PROFILE_ONLY` 为准，文档不硬编码工具数量。新增工具必须登记正确 profile，并验证 stdio 握手和工具列表。

- `device_connect`、`device_sessions`、`device_command` 调用统一会话服务。
- `device_observe` 与带观察的 `device_act` 返回 MCP 图像内容；旧 `device_screenshot` 返回元数据/文件路径。
- `agent_task` 暴露逐步提交、接管、恢复和导出。
- MCP 浏览器默认 extension；拒绝 Playwright 连接、观察、动作以及使用它的任务，已保存绑定列入 `unavailable_sessions`，不能静默创建替代页面。
- `mcp/client.py` 是另一条能力：QA brain 可调用外部 MCP，私有配置为 `.saygo/mcp_clients.json`。
- 保持仓库声明的 MCP SDK 兼容范围；改动注册/握手时检查兼容分支与对应版本，不能只凭本机版本通过就宣称整个范围已验证。

`plugins/saygo-device/` 提供共享 device Skill、doctor 命令和 MCP 接入。托管安装器负责客户端差异；Claude marketplace 入口仍存在。插件缓存只包含插件自身目录，launcher 必须能找到已安装 runtime 或显式 checkout，不依赖缓存外相对路径。

公开插件文案用英文；skill/command/server 名不重复插件前缀。修改插件时检查 manifest、SKILL frontmatter 和客户端加载；Claude 插件改动运行 `claude plugin validate .` 和 `claude plugin validate plugins/saygo-device`。客户端权限配置只限 Saygo，保留无关设置和用户自定义项。客户端需要重启才能加载新工具。

通用操作协议在 [device Skill](plugins/saygo-device/skills/device/SKILL.md)；此 `agent.md` 是开发说明，不代替客户端的操作 Skill。

## 用例格式

**`.feature`(Gherkin，推荐)**：Feature/Background/Scenario(Outline)+Examples 全解析。Tag：

- `@P0/@P1/@P2` 优先级；`@auto/@partial/@manual`(后两个自动 skip)；`@skip/@wip` 跳过。
- **平台标签是集合，可扩展，不用 both**：`@android`/`@ios`/`@browser`/`@mac`/`@windows`/`@desktop`/`@rdp` 等；`@android @ios` 表示允许这两个平台。标签只筛选用例，不切换驱动、不自动发起双端运行。跑测平台不在集合里则 skip。
- `@TC-XXX` case ID；`@reset:pm_clear|relaunch|none` Android 重置(覆盖 feature 级 `# saygo-reset-default`)。
- 文件头信息元数据：`# saygo-target/platform/package/reset-default`(值行别写行内 `#` 注释)。

**`.md`(TDD)**：`### TC-XXX` 块 + `- **Priority/Reset before/Platform/Mode/Steps**`。

## Per-target 文件（私有 `tests/<target>/` gitignored；公开 `*_demo` 和模板例外）

- `cases/*.feature|*.md` 用例；`_preconditions.md`(auto-prepend，教 LLM 从异常态恢复到 Background 前置态——降 false-fail 最关键)；`_accounts.json`(账号/密钥池，`${EMAIL}`/`${PASSWORD}` 占位，**别 commit 真账号**，多设备按 worker i 绑 accounts[i])；`reports/<ts>/*.html`。
- 用例约定：**自包含**(前置态进 Background，进子页操作明示，不依赖前置 case)；Hints 写方位不写坐标；Then 拆成逐条可验证 bullet；不可视断言改写或 skip。

## Setup / CLI


```sh
# 开发：按需增加 desktop/mobile/windows/mac/qa 等 extras。
python3 -m pip install -e '.[mcp]'
saygo --help
# 终端 Agent：自动检测客户端，安装托管集成和浏览器桥。
python3 scripts/install_agent_plugin.py

saygo device sessions
saygo device list --platform android
saygo device connect --platform android --device DEVICE_ID --session phone
# 先安装浏览器桥并在扩展内 Connect。
saygo device connect --platform browser --backend extension --session web
saygo device capabilities --session web
saygo doctor --session web
saygo device screenshot --session web
saygo device act '{"type":"tap","x":50,"y":40,"coordinate_space":"percent"}' --session web --observation-id OBSERVATION_ID --observe-after
saygo task create '{"phone":"phone","web":"web"}'
saygo task recover TASK_ID
saygo task timeline TASK_ID

# QA 独立配置模型和目标；密钥只留本地。
saygo init
saygo run tests/my-app/login.feature --report
saygo probes list --json
saygo probes check NAME check=home_impression --target my-app --wait
```

按改动选择检查：`tests/control_demo`、`tests/runtime_demo`、`tests/desktop_demo`、`tests/extension_demo`、`tests/mobile_demo`。优先离线单元/契约测试；带 live 标记的测试需对应主机、授权和测试目标。未运行的真机路径必须明确说明。

## .env 关键项（真值在此，勿入库）
```
PLATFORM=android|ios|browser|mac|windows|desktop|rdp
LLM_PROVIDER=openrouter  LLM_API_KEY=…  LLM_BASE_URL=https://openrouter.ai/api/v1  LLM_MODEL=<vision-model-id>
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
PROBES_MODE=all|skip|only    # = saygo run --skip-probes / --only-probes
BROWSER_HEADLESS / VIEWPORT_* / SELENIUM_GRID_URL ; FIGMA_TOKEN ; SKILLS_ENABLED
```

## 已知限制 / 行为

- 坐标不准的头号真因是**分辨率标定**(截图px↔设备px 换算)；标定对了 + 用百分比协议基本落中。
- 不可视断言(埋点/后端/系统时间/launcher badge/通知抽屉/跨App)**视觉层**一律 fail，不许蒙混；要真验证挂 probe 插件(见上)，那条 step 由插件裁决。
- iOS 真机若非专用设备(如私人手机)会反复 `unavailable`(锁屏/休眠/拔线)——按需插+解锁，规模化用专用设备或云真机(云上 iOS 只有 Appium 一条路，且免签名代管)。
- 依赖按 `pyproject.toml` extras 选择，不要求浏览器用户安装移动端工具链。桌面模型客户端与 QA 模型客户端独立，修改时注意各自协议。

## Existing browser extension backend


`extensions/saygo-browser/` + `saygo/integrations/browser_bridge.py` + `platforms/browser_extension.py`：Native Messaging + 私有共享目录 IPC，Windows host 可与 WSL Saygo 通信，不监听网络端口。默认当前浏览器配置文件内所有 HTTP(S) 标签页（含新弹窗），操作目标仍显式选择；browser session UUID + tab ID 防重启误选；不提取 DOM。Runtime browser resource 必须写 `backend: extension`；先安装桥，再用 `device connect` 连接，见 `docs/browser-extension.md`。仅能声称已执行的平台测试，Windows/WSL 桥接、导航和截图已实机验证。测试：`tests/extension_demo`，可选 `SAYGO_TEST_CHROME`。

## Mobile discovery and simulator provisioning


`saygo/devices/mobile.py` implements mobile adapters behind `device list/connect/install/boot`. Discovery covers
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


`saygo/devices/control.py` extends `saygo device` with list/connect/sessions/disconnect,
install/boot and common open/scroll commands. `--session` aliases `--serial`.
Desktop bindings persist app+OS and reconnect short-lived controllers; Runtime
accepts desktop `session` resources as well as inline `app`. Disconnect preserves
endpoint configuration and blocks subsequent actions until explicit reconnect;
it is not cross-process cancellation or global extension revocation. Tests:
`tests/control_demo`. User guide: `docs/control.md`.

## Package layout


See `docs/architecture.md`. QA engine modules live under `saygo/qa/`, image
processing under `saygo/vision/`, device lifecycle/toolchain/Windows workers under
`saygo/devices/`, and Figma/browser bridge integrations under `saygo/integrations/`.
CLI command families live under `saygo/commands/`; `cli.py` only registers and
dispatches. Shared case discovery, device preparation and scheduling live in
`qa/cases.py`, `qa/device_setup.py` and `qa/execution.py`. MCP imports these
owning modules directly, without old CLI helper aliases.
Use these paths in imports. Root CLI/config/logger and browser bridge command
compatibility entry points remain. Windows worker bundles preserve the nested
package structure; `tests/mobile_demo/test_bundle.py` verifies isolated imports.
