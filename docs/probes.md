# Probe —— 非视觉断言插件（埋点 / 后端落库 / 上报日志）

argus 是**纯视觉**的：只看截图做决策和判断。屏幕上看不见的东西（埋点上报、后端
落库、系统时间、通知抽屉…）视觉层永远判不了，所以有条铁律：**不可视断言一律
fail，不许 LLM「推断成立」蒙混**。

Probe 是给这类断言开的一条**代码层**通道：

- 用例在某个 Then 下面挂一行 `# argus-probe: <name> k=v`
- 那个 step **完全不进 LLM**，argus 直接调插件，用返回的 verdict 定 pass/fail
- 「具体怎么查」100% 由插件决定（查数据仓库 / 查上报日志 / 抓包 / 调后端 API）

因为 verdict 来自代码层，brain 侧那套反谎报硬墙**一个字都不用改** —— 这些 step
LLM 根本看不到，没有蒙混的余地。

## 30 秒上手

```bash
cp .argus/probes.json.example .argus/probes.json        # 注册表（gitignored）
mkdir -p tests/<target>/probes
cp tests/_template/probes/analytics.py.example tests/<target>/probes/analytics.py
# 改 analytics.py 里的 _query()，填 .argus/probes.json 里的 dsn / 表名 / 事件映射
argus probes list                                       # 确认插件能加载
argus probes check analytics check=首页曝光 --target <target>   # 不起设备单发调试
```

用例里：

```gherkin
@P0 @android
Scenario: 首页曝光埋点
  Given 已登录进入首页
  When 停留 3 秒
  Then 上报首页曝光埋点
  # argus-probe: analytics check=首页曝光
```

## 用例侧语法

```
# argus-probe: <probe名> [k=v ...]
```

- 放在 step 的**下一行**，绑定它**上面最近的那个 step**（`.feature` 和 `.md`
  用例都一样）。
- 一个 step 只挂一条；多个事件请拆成多个 Then 各挂一条（符合「Then 拆成逐条
  可验证 bullet」的用例约定），或用数组参数一次传多个。
- 值支持裸 token（自动转 int/float/true/false/null）、`"引号串"`、
  `{...}` / `[...]`（按 JSON 解析）。裸 key 无 `=` 视为 `true`。
- Scenario Outline 的 `<占位符>` 在 directive 里同样会被 Examples 替换。

**用例里别写事件名 / 表名。** 写意图（`check=首页曝光`），让插件维护
「意图 → 事件名 + 期望属性」的映射。这样改事件名、或从查库改成查日志时，用例
一个字都不用动 —— 这也是当初选这个语法的原因。

Background 里的 probe 声明**不会执行**（Background 不进 step 主循环），解析时会
告警提醒你挪到 Scenario 里。

## 三态 verdict：为什么不是 pass/fail

埋点链路普遍是**批量上报**，落库有分钟级延迟。查太早得到 0 行**不等于漏报**，
据此判 fail 就是造假 bug。所以除了 `pass` / `fail` 还有第三态：

| verdict | 含义 | argus 的动作 |
|---|---|---|
| `pass` | 查到了，符合预期 | step pass，evidence 打 `[probe:<name>]` 前缀进报告和后续 step 的锚点 |
| `fail` | 确定性否定（事件到了但属性不对 / 意图没配映射） | 整 case fail，不再重试 |
| `inconclusive` | 还查不到，但也不能说没有 | 按 `retry_after_s` 重查，直到预算耗尽才判 fail |

重试轮**不计入** no-progress 计数（同 `wait` 动作语义），所以慢 flush 不会被
「连续 N 轮无推进」误判成假失败。预算/间隔的优先级：

```
.env 的 PROBE_TIMEOUT_S / PROBE_POLL_INTERVAL_S
  > 单个 probe 的 timeout_s / poll_interval_s
  > 注册表 defaults 块
  > 内置默认（300s / 20s，重查间隔下限 2s）
```

插件自己抛异常 / 子进程崩了 / stdout 不是 JSON → 记 error 并**当作
inconclusive 继续重试**，预算耗尽才 fail 并把 error 写进报告。绝不因为「查不了」
就静默放过断言。

## 跳过埋点检查 / 只跑埋点检查

```bash
argus run <target> --skip-probes    # probe step 全部标 skip，不调插件
argus run <target> --only-probes    # 只跑「声明了 probe 断言」的 case
```

两者互斥。也可以用 env `PROBES_MODE=skip|only`（`--bg` 子进程、多设备 worker、
MCP `run_target(probes="skip")` 都走这条通道）。

**`--skip-probes`** —— 数据通道挂了、或这一轮只关心 UI 回归时用：
- probe step 标 **`skip`（不是 pass）**，直接推进到下一步
- case 的 `reason` 里点名哪几步没验证，结果 JSON 有 `probes_skipped_steps`，
  HTML 报告里该 step 显示 SKIP + 灰色 probe 块 —— **一份全绿报告必须能看出缺口**
- 插件一次都不调（连 setup 都不调）

**`--only-probes`** —— 只关心埋点这一轮时用。注意它是**case 级筛选**，不是
「只跑 probe step」：埋点得靠前面的 UI 操作触发出来，只查库不操作 App 永远查不到。
所以命中的 case 里 **UI 步照常跑**，只是没有 probe 声明的 case 整个 skip（跟
`@manual` 一样进报告的 skip 计数）。开跑时会打印 `N/M 个 case 声明了 probe 断言`，
一个都没命中会告警。

真的想「完全不跑 App、只查一次数据」→ 用 `argus probes check`（见下）。

## 注册表 `.argus/probes.json`

跟 `.argus/mcp_clients.json` 同一档：**gitignored**，里面放连接串和密钥；
`.example` 入库。`${VAR}` 会用环境变量展开，所以真值可以只放 `.env`。

```json
{
  "defaults": {"timeout_s": 300, "poll_interval_s": 20},
  "probes": {
    "analytics": {
      "type": "python",
      "module": "tests/<target>/probes/analytics.py",
      "class": "AnalyticsProbe",
      "config": {"dsn": "${ANALYTICS_DSN}", "events": {"首页曝光": {"event": "…"}}}
    },
    "applog": {
      "type": "subprocess",
      "command": ["node", "tests/<target>/probes/applog.js"],
      "call_timeout_s": 60,
      "config": {}
    }
  }
}
```

`module` 支持文件路径（相对 cwd）或点分模块名。`class` 省略时自动找模块里唯一的
`Probe` 子类。也可以用 `PROBES_CONFIG` / `ARGUS_PROBES_CONFIG` 指到别处。

**插件代码放哪**：`tests/<target>/probes/` —— 那个目录整体是客户私有仓，事件名、
表名、查询逻辑都属于客户内容，不该进 argus 主仓。argus 主仓只有接口 + 加载器 +
占位符示例。

## 两种插件形态

### A. Python 类（最短路径）

```python
from argus.probes.base import Probe, ProbeResult

class MyProbe(Probe):
    name = "analytics"

    def setup(self, ctx): ...        # 可选，每个 case 首次用到时调一次
    def teardown(self, ctx): ...     # 可选，best-effort

    def check(self, ctx, args) -> ProbeResult:
        rows = my_query(since=ctx.case_started_at, user=ctx.account.get("email"))
        if rows:
            return ProbeResult.ok(f"查到 {len(rows)} 条，首条 {rows[0]['ts']}",
                                  data={"rows": rows[:20]})
        return ProbeResult.pending("暂未查到，可能还没上报", retry_after_s=30)
```

`self.config` = 注册表里该 probe 的 `config` 块（`${VAR}` 已展开）。
完整示例见 `tests/_template/probes/analytics.py.example`。

### B. 子进程（任何语言）

stdin 收一个 JSON 请求，stdout 出一个 JSON verdict，调试信息写 stderr。
协议全文见 `argus/probes/subprocess_probe.py` 的 docstring，
可跑的示例见 `tests/_template/probes/applog.sh.example`。

进程隔离，所以插件的依赖（DB driver、抓包库）不会污染 argus 自己的环境 ——
这是它相对 Python 型的主要优势。

## 插件能拿到的上下文（`ProbeContext`）

核心用途：把「**本次跑测**产生的数据」从历史数据里框出来。

| 字段 | 用途 |
|---|---|
| `case_started_at` / `step_started_at` / `now` | 查询时间窗（epoch 秒）。flush 延迟的等待预算从 `step_started_at` 起算 |
| `account` | 本 case 绑的账号（多设备并行时 worker i 绑 accounts[i]）—— 身份锚点 |
| `device` / `platform` / `app_package` / `app_version` | 区分同一时间窗里的多台设备 / 双端 |
| `attempt` / `elapsed_s` / `timeout_s` | 第几次重查、已等多久、总预算（插件可自己决定要不要提前放弃） |
| `run_id` / `case_id` / `target` | 跑测标识，便于插件自己留痕 |
| `step_index` / `step_text` / `scenario_steps` | 断言上下文 |
| `artifacts_dir` | 落原始数据（SQL 结果 / dump）的目录，路径进报告 |
| `session` | 插件自留地，同一 case 内跨多次 check 保留（如缓存 distinct_id） |

## 调试

```bash
argus probes list [--json]                     # 有哪些插件 + 逐个试加载
argus probes check <name> [k=v ...] \
    --target <target>      # 绑 tests/<target>/_accounts.json 的 accounts[0]
    --since-s 600          # 假装 case 是 600 秒前开跑的（查询时间窗）
    --wait                 # inconclusive 时按节奏轮询到预算耗尽
    --json
```

`check` 不起设备、不跑用例，把「查询写没写对」和跑测流程解耦。退出码：
pass=0，其余=1。

## 报告

probe step 在 HTML 报告里单独一块：查了什么（spec）、verdict、evidence、
第几次查/等了多久/预算多少，以及折叠的原始数据（超 20000 字符截断）。
首次和终局各留一张截图做现场对照。

## 已知边界

- 一个 step 一条 probe（多断言拆多个 Then）。
- 挂了 probe 的断言步不会进「连续断言合并」（合并会一次推进多步，等于让 brain
  视觉判掉本该走插件的断言）—— 合并块会自动截到 probe 步之前。
- 用例声明了 probe 但注册表里没这个名字 / 压根没注册表 → **判 fail**，报错里
  写清楚怎么修。不静默跳过：一条没被验证的断言当 pass 就是前面那条铁律禁的事。
  真要跳过用 `--skip-probes`（标 skip 并在 reason 里留痕）或 `@manual` / `@partial`。
