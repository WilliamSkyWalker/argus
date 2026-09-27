# 多资源操作运行时（首版）

Saygo 的通用操作引擎正在从 QA 引擎中独立出来。新增 `saygo workflow` 支持显式 JSON
工作流：手机、浏览器、桌面之间顺序切换，传递结构化结果，查询数据库，暂停交给用户，
并在用户交还控制权后继续。原有 `saygo run` 和 QA Agent 保持原入口。

当前版本提供执行基础，不会根据自然语言自动规划步骤、识别支付页面或生成点击坐标。
人工步骤需显式声明；视觉操作使用截图和百分比坐标，适合外部 Agent 或显式流程调用。
目前数据库连接器只有 SQLite 的已注册只读查询，尚无写操作、通用 API/MCP 连接器或远程接管 UI。

## 当前架构

```text
JSON workflow → 校验 → Runtime → 资源适配器
                         │       ├─ Appium / 持久 Chrome 会话
                         │       ├─ macOS / Windows 窗口驱动
                         │       └─ SQLite 只读查询
                         ├─ SQLite checkpoint + 事件历史
                         └─ waiting_for_human → 用户 resume → 重新观察 → 后续验证
```

- `saygo/runtime/schema.py`：版本化格式与保留 JSON 类型的变量引用。
- `store.py`：任务快照、事件、资源归属；任务执行使用 OS 文件锁，进程退出后自动释放执行锁。
- `resources.py`：复用现有视觉驱动；查询只读数据库。
- `engine.py`：顺序路由、动作意图记录、暂停恢复和不确定动作核对。
- `cli.py`：命令入口。浏览器默认使用 Playwright（已加入 requirements.txt），显式工作流不要求 LLM key。

## 启动

所有命令都从仓库根目录运行。默认状态保存在 `.saygo_runs/runtime/`（已被 Git 忽略）。
也可以设置 `SAYGO_RUNTIME_DIR` 或使用 `workflow --state-dir /absolute/path ...`。
**同一组资源必须使用同一个状态目录**，资源锁只在这个目录内协调。

移动端与浏览器需要先创建命名会话：

```bash
python3 -m saygo.cli device start --platform android --serial emulator-5554
python3 -m saygo.cli device start --platform browser --backend playwright --serial admin-web
python3 -m saygo.cli workflow start examples/workflows/phone_browser_payment.json
```

跨端示例需要替换设备序列号、业务页面，并提供实际 SQLite 数据库。创建订单后通过 `resume --data` 返回订单 ID。
其中创建订单暂由用户操作，浏览器仅打开示例页面；它展示编排协议，**不是自动下单或后台核对的完整业务实现**。
数据库相对路径按工作流文件所在目录解析，在创建任务时转成绝对路径。

Windows/WSL 可运行 `examples/workflows/desktop_observe.json`，将 `app` 改成已打开窗口的标题子串。
macOS 使用 `kind: "mac"` 与应用名；当前 macOS 驱动 setup 会打开/前置该应用。
桌面操作仍使用真实前台键鼠。Windows 适配器不配置自动启动命令。

纯数据库示例：先准备含 `orders(id,status)` 表的 `examples/workflows/orders.sqlite`，再运行：

```bash
python3 -m saygo.cli workflow start examples/workflows/query.json
```

不要把真实数据库或工作流中的账户信息加入版本库。

## 浏览器后端

browser 资源默认使用 Playwright/CDP，可显式配置 `"backend": "selenium"`。
支持稳定 page ID、列出/选择/关闭标签页，任务恢复会回到任务绑定的页面。
安装、命令与限制见 [浏览器后端文档](browser.md)。旧 QA 入口保持 Selenium。

## 步骤与数据传递

`version: 1`，`resources` 是资源名到规格的映射，`steps` 按列表顺序执行；每个 step 的 `id` 唯一。

| kind | 行为 | 必需字段 |
|---|---|---|
| `pages` | 列出浏览器页面（Playwright） | `resource` |
| `observe` | 保存资源截图及尺寸、时间、图像摘要 | `resource` |
| `action` | 执行一次视觉操作，记录执行意图和结果 | `resource`, `observation`, `action` |
| `tool` | 调用资源中注册的参数化查询 | `resource`, `operation` |
| `human` | 等待用户在指定资源完成操作 | `resource`, `instructions`, `verify_step` |
| `check` | 比较结构化结果，失败则停止 | `actual`, `equals` |

引用整个 JSON 值，不做字符串拼接：

```json
{"$ref": "steps.lookup.rows.0.status"}
```

可引用 `inputs.*` 和已完成步骤的 `steps.<id>.*`；列表支持数字下标。
引用缺失会使执行失败，不会替换为空值或猜测数据。

视觉动作例子：

```json
{
  "id": "tap_continue",
  "kind": "action",
  "resource": "phone",
  "observation": {"$ref": "steps.phone_screen"},
  "action": {"type": "tap", "x_pct": 50, "y_pct": 75}
}
```

支持 `tap`、`swipe`、`input`、`press_key`、`scroll_up/down`、`open_url`、`open_app`；浏览器另支持 `go_back/forward`、`select_page/close_page`。
点击坐标是 0–100 百分比，swipe 使用 `x1_pct/y1_pct/x2_pct/y2_pct`。
具体平台仍可能不支持某类动作；派发后异常进入 `needs_review`，不会自动重试。

每个 action 必须引用同资源最近 30 秒内、尚未消费的 observation。派发前再次截图，
尺寸与图像摘要须一致。当前是保守的精确图像比对：时钟、动画、光标闪烁也可能导致拒绝；
后续将加入目标重定位与稳定区域校验。拒绝时需要重新观察、重新规划，不能盲目沿用旧坐标。
动作结果 `dispatched` 只表示驱动调用返回，不代表业务目标达成；应安排独立 `check`。

SQLite 每个 operation 对应资源规格里的已注册 SQL，参数使用 `arguments` 绑定。
连接以 `mode=ro` 打开，authorizer 禁止写入、ATTACH、PRAGMA 等非查询操作；查询最多返回
1000 行，并设置执行时间限制。首版不轮询最终一致性结果：检查时尚未查到数据会失败。

## 人工接管

`human` 必须指定后续 `check` 的 ID 为 `verify_step`。例如完成支付后，后续查询订单状态，
再检查其等于 `paid`。该 check 的业务正确性由工作流作者负责。

```bash
python3 -m saygo.cli workflow status RUN_ID
python3 -m saygo.cli workflow resume RUN_ID --note "已创建订单" --data '{"order_id":"order-1"}'
# 遇到后续支付接管时：
python3 -m saygo.cli workflow resume RUN_ID --note "已在手机完成支付"
python3 -m saygo.cli workflow events RUN_ID
```

暂停返回 `waiting_for_human`，命令正常退出，不持续调用模型或控制设备。
任务保留资源归属，其他 workflow 任务不能占用同一资源；用户直接在设备上操作。
恢复需要用户显式发出 `resume`，系统重新连接并观察视觉资源，丢弃过时截图，再继续执行。
用户的说明会记录为 acknowledgement，后续 check 才负责判定结果。`--data` 可返回 JSON 对象，
后续通过 `steps.<human_step>.data.*` 引用；这些数据的来源是用户，不视为已经验证的事实。

还可以主动请求暂停或取消：

```bash
python3 -m saygo.cli workflow pause RUN_ID
python3 -m saygo.cli workflow cancel RUN_ID
```

执行中的任务在动作边界处理请求，不强行中断已派发的点击或查询。
首版接管暂停整个工作流；不支持暂停手机后让另一分支并行执行。
单独使用旧的 `saygo device`、QA 引擎或外部应用不会遵守新运行时的锁，使用期间不要混用自动化入口。

## 崩溃恢复与不确定动作

状态：`queued`、`running`、`waiting_for_human`、`needs_review`、`succeeded`、`failed`、`cancelled`。

- 每个有副作用的 action 先持久化 `action_dispatching`，再调用驱动，最后原子保存结果和步骤进度。
- 驱动抛异常或派发期间崩溃，无法证明动作是否生效时进入 `needs_review`。
- `recover` 只有在执行进程已退出、OS 锁可获得时才允许执行，不会抢占活跃执行器。
- 已完成的步骤不重放。没有在途动作的中断任务可恢复为 queued；有在途动作必须先核对。

```bash
python3 -m saygo.cli workflow recover RUN_ID
python3 -m saygo.cli workflow resolve RUN_ID --outcome completed --note "已核对订单确实创建"
python3 -m saygo.cli workflow run RUN_ID
```

如果确定动作未执行，可使用 `--outcome not_executed`。再次执行前会重新截图，只有画面仍与
原动作依据一致才允许重试；画面变化则失败，需重新规划。这不提供跨 UI 与数据库的 exactly-once 保证。
`failed`/`cancelled`/`succeeded` 是终态，不支持原地重新运行。

任务持久化不等于设备会话永久存活。Appium 超时、手机断连或 Chrome 退出后，需要显式重建/重连
命名会话；恢复时不会自动重置应用状态。截图、工作流输入和查询结果会落到本地状态目录，按业务数据管理。

`start` 先输出 `created_run_id`，再输出最终快照；`status` 输出快照，`events` 输出事件列表。
退出码：正常执行/人工等待为 0，failed/needs_review 为 1，命令或状态错误为 2。

## 验证与后续阶段

```bash
python3 -m unittest discover -s tests/runtime_demo -v
```

离线测试使用模拟视觉驱动和真实 SQLite，覆盖跨资源数据流、接管恢复、失败验证、资源互斥、
崩溃与不确定动作、过时截图及查询只读限制；不代替真机、浏览器和桌面集成验证。

下一阶段按顺序推进：

1. 通用视觉决策策略：按目标看屏、定位、执行、验证；通过 Runtime 统一派发，不沿用 QA 专属 pass/fail prompt。
2. 更可靠的视觉观察版本与重定位、登录/支付接管触发、结构化数据提取。
3. 参数化 API/MCP Connector、带幂等策略的写操作、可持久化的等待与重试。
4. QA 工作流适配、任务时间线和远程接管界面；再考虑多资源并行与自然语言自动规划。
