"""Probe（探针）接口 —— argus 的非视觉断言通道。

argus 主循环是**纯视觉**的：只看截图。埋点上报 / 后端落库 / 上报日志这类
「屏幕上看不见」的断言，视觉层永远判不了（铁律：不可视断言一律 fail，不许
LLM 靠推断蒙混过关）。Probe 就是给这类断言开的一条**代码层**通道：

- 用例在某个 Then 后面挂一行 ``# argus-probe: <name> k=v ...``
- 该 step **完全不进 LLM**：agent 直接调对应 probe，用 verdict 定 pass/fail
- 「具体怎么查」由 probe 实现自己决定（查数据库 / 查上报日志 / 抓包 /
  调后端 API），argus 只管「什么时候查、查什么、结果怎么进报告」

所以 brain 侧的反谎报硬墙一个字都不用改 —— 这些 step LLM 根本看不到。

## 三态 verdict

埋点链路普遍有**批量上报延迟**（分钟级）。查太早得到 0 行**不等于**漏报，
据此判 fail 就是造假 bug。所以 probe 除了 ``pass`` / ``fail`` 还有第三态：

    ``inconclusive`` —— 「现在还查不到，但也不能说没有」

argus 收到 inconclusive 会按 ``retry_after_s``（probe 自己给的节奏，没给则用
配置里的轮询间隔）重查，直到 ``timeout_s`` 耗尽才判 fail。**这样"先等 flush
再下结论"是框架保证的，不依赖每个插件作者自觉。**

## 实现一个 probe

方式 A —— Python 类（最短路径）::

    from argus.probes.base import Probe, ProbeResult

    class MyProbe(Probe):
        name = "analytics"

        def check(self, ctx, args):
            rows = my_query(event=self.config["events"][args["check"]],
                            since=ctx.case_started_at, user=ctx.account.get("email"))
            if rows:
                return ProbeResult.ok(f"查到 {len(rows)} 条，首条 {rows[0]['ts']}",
                                      data={"rows": rows[:20]})
            return ProbeResult.pending("暂未查到，可能还没上报", retry_after_s=30)

方式 B —— 任何语言写的可执行文件：stdin 收一个 JSON 请求、stdout 出一个 JSON
verdict，见 ``argus/probes/subprocess_probe.py`` 的协议说明。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# verdict 取值
PASS = "pass"
FAIL = "fail"
INCONCLUSIVE = "inconclusive"

VALID_VERDICTS = (PASS, FAIL, INCONCLUSIVE)


@dataclass
class ProbeContext:
    """argus 喂给 probe 的上下文 —— 用来把「本次跑测产生的数据」从历史数据里框出来。

    时间戳都是 epoch 秒（``time.time()``，UTC 基准）。probe 自己决定用哪些字段
    拼查询：典型做法是 ``case_started_at <= ts <= now`` 加上 account/device 作为
    身份锚点。
    """

    # 本次断言
    step_index: int = 0                  # 1-based，对应 Scenario 的第几个 step
    step_text: str = ""                  # 该 step 原文（含 Then/And 前缀）
    scenario_steps: list[str] = field(default_factory=list)

    # 时间窗
    case_started_at: float = 0.0         # 本 case 开跑时刻
    step_started_at: float = 0.0         # 本 step 开始时刻（前一步刚做完动作的时刻）
    now: float = 0.0                     # 本次 check 发起时刻

    # 轮询状态（同一个断言可能被查多次，见模块 docstring 的三态说明）
    attempt: int = 1                     # 第几次尝试，1-based
    elapsed_s: float = 0.0               # 本 step 已经在等/查上花掉多少秒
    timeout_s: float = 0.0               # 总预算（超了 argus 就判 fail）

    # 被测对象
    platform: str = ""                   # android / ios / browser / ...
    device: str = ""                     # udid / adb serial（多设备并行时区分数据来源）
    app_package: str = ""                # 包名 / bundle id
    app_version: str = ""                # 若平台能报出来

    # 跑测标识
    run_id: str = ""
    case_id: str = ""                    # @TC-XXX
    target: str = ""                     # tests/<target>

    # 本 case 绑定的账号（多设备并行时按 worker 绑 accounts[i]）。含密钥，
    # **别往日志/报告里整体打印**。
    account: dict = field(default_factory=dict)

    # probe 可以往这里落原始数据（SQL 结果 / 抓包文件），路径会进报告
    artifacts_dir: str = ""

    # probe 自留地：同一 case 内跨多次 check / 跨 setup→check 保留（比如缓存
    # 查出来的 distinct_id）。argus 不解释内容。
    session: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """给 subprocess 协议序列化用。"""
        return {
            "step_index": self.step_index,
            "step_text": self.step_text,
            "scenario_steps": list(self.scenario_steps),
            "case_started_at": self.case_started_at,
            "step_started_at": self.step_started_at,
            "now": self.now,
            "attempt": self.attempt,
            "elapsed_s": self.elapsed_s,
            "timeout_s": self.timeout_s,
            "platform": self.platform,
            "device": self.device,
            "app_package": self.app_package,
            "app_version": self.app_version,
            "run_id": self.run_id,
            "case_id": self.case_id,
            "target": self.target,
            "account": dict(self.account),
            "artifacts_dir": self.artifacts_dir,
            "session": dict(self.session),
        }


@dataclass
class ProbeResult:
    """probe 的判定结果。

    ``evidence`` 会原样进 HTML 报告、并作为该 step 的 evidence 锚点喂给后续
    step 的 LLM —— 所以要写**具体查到了什么**（条数 / 时间戳 / 关键属性值），
    别写「验证通过」这种没信息量的话。
    """

    verdict: str = INCONCLUSIVE
    evidence: str = ""
    data: dict = field(default_factory=dict)   # 结构化明细，折叠进报告
    retry_after_s: float | None = None         # 仅 inconclusive 有意义
    error: str = ""                            # probe 自身出错（连不上库等）

    @property
    def is_pass(self) -> bool:
        return self.verdict == PASS

    @property
    def is_fail(self) -> bool:
        return self.verdict == FAIL

    # ── 便捷构造 ──
    @classmethod
    def ok(cls, evidence: str, data: dict | None = None) -> "ProbeResult":
        return cls(verdict=PASS, evidence=evidence, data=data or {})

    @classmethod
    def no(cls, evidence: str, data: dict | None = None) -> "ProbeResult":
        return cls(verdict=FAIL, evidence=evidence, data=data or {})

    @classmethod
    def pending(cls, evidence: str, retry_after_s: float | None = None,
                data: dict | None = None) -> "ProbeResult":
        """还查不到 —— argus 会等一会儿重查，直到预算耗尽才判 fail。"""
        return cls(verdict=INCONCLUSIVE, evidence=evidence,
                   retry_after_s=retry_after_s, data=data or {})

    @classmethod
    def failed(cls, error: str, data: dict | None = None) -> "ProbeResult":
        """probe 自己炸了（连不上库 / 查询语法错）。

        当作 inconclusive 处理：argus 会重试，预算耗尽仍不行才判 step fail
        并把 error 写进报告 —— 绝不因为「查不了」就静默放过断言。
        """
        return cls(verdict=INCONCLUSIVE, evidence=f"probe 执行失败: {error}",
                   error=error, data=data or {})

    @classmethod
    def from_dict(cls, raw: Any) -> "ProbeResult":
        """解析 subprocess probe 的 JSON 输出（容错：字段缺失/类型不对不抛）。"""
        if not isinstance(raw, dict):
            return cls.failed(f"probe 输出不是 JSON 对象: {type(raw).__name__}")
        verdict = str(raw.get("verdict", "")).strip().lower()
        if verdict not in VALID_VERDICTS:
            return cls.failed(
                f"probe 返回了无效 verdict {raw.get('verdict')!r}"
                f"（合法值: {'/'.join(VALID_VERDICTS)}）")
        data = raw.get("data")
        retry = raw.get("retry_after_s")
        try:
            retry = float(retry) if retry is not None else None
        except (TypeError, ValueError):
            retry = None
        return cls(
            verdict=verdict,
            evidence=str(raw.get("evidence") or ""),
            data=data if isinstance(data, dict) else ({"value": data} if data else {}),
            retry_after_s=retry,
            error=str(raw.get("error") or ""),
        )


class Probe(ABC):
    """一个 probe = 一种「怎么查」的实现。

    子类只需实现 ``check``。``setup`` / ``teardown`` 可选（每个 case 各一次）。
    ``self.config`` 是注册表里该 probe 的 ``config`` 块（``${VAR}`` 已展开），
    真值（连接串 / 表名 / 事件名映射）都放那儿，别写死在代码里。
    """

    name: str = "base"

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    def setup(self, ctx: ProbeContext) -> None:
        """每个 case 第一次用到本 probe 之前调一次。失败只记 warning，不阻塞。"""

    @abstractmethod
    def check(self, ctx: ProbeContext, args: dict) -> ProbeResult:
        """执行一次断言查询。

        ``args`` 是用例 directive 里的 k=v（如 ``check=首页曝光 tab=recommend``，
        值会被 coerce 成 int/float/bool，``{...}`` / ``[...]`` 按 JSON 解析）。
        用例只表达**意图**，事件名/表名这类实现细节由 probe 自己映射。

        返回 pass / fail / inconclusive；抛异常等价于 ``ProbeResult.failed``。
        """
        ...

    def teardown(self, ctx: ProbeContext) -> None:
        """case 结束后调（best-effort，异常只记 warning）。"""
