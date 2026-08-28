"""子进程 probe —— 用任何语言写「怎么查」。

argus 起一个子进程，把请求 JSON 写进 stdin，从 stdout 读一个 JSON verdict。
进程隔离，所以 probe 需要的依赖（DB driver / 抓包库）不会污染 argus 自己的环境。

## 请求（argus → probe，stdin，单个 JSON 对象）

```json
{
  "probe": "analytics",
  "action": "check",              // setup | check | teardown
  "args": {"check": "首页曝光"},   // 用例 directive 里的 k=v
  "config": {"dsn": "…"},         // 注册表里该 probe 的 config 块（${VAR} 已展开）
  "context": {
    "step_index": 4, "step_text": "Then 上报首页曝光埋点",
    "case_started_at": 1755400000.0, "step_started_at": 1755400042.1,
    "now": 1755400050.3, "attempt": 2, "elapsed_s": 30.0, "timeout_s": 300.0,
    "platform": "android", "device": "…", "app_package": "…", "app_version": "…",
    "run_id": "…", "case_id": "TC-XXX-001", "target": "…",
    "account": {"email": "…"}, "artifacts_dir": "…", "session": {}
  }
}
```

## 响应（probe → argus，stdout，单个 JSON 对象）

```json
{
  "verdict": "pass" | "fail" | "inconclusive",
  "evidence": "查到 3 条 …，首条 10:02:11",
  "data": {"rows": [...]},        // 可选，折叠进 HTML 报告
  "retry_after_s": 30,            // 可选，仅 inconclusive 有意义
  "session": {"distinct_id": "…"} // 可选，argus 会在同一 case 内回传给后续调用
}
```

约定：
- stdout **只准**输出这一个 JSON 对象；调试信息写 stderr（argus 会记进日志）。
- 查不到数据时返回 ``inconclusive`` 而不是 ``fail`` —— 埋点上报有分钟级延迟，
  argus 会按 ``retry_after_s`` 重试到预算耗尽才判 fail。
- 退出码非 0 / stdout 不是合法 JSON → argus 记 error 并继续重试，预算耗尽判 fail。
- ``setup`` / ``teardown`` 是可选的：不认这两个 action 就返回
  ``{"verdict":"pass"}`` 或直接退出 0，argus 只记 debug。
"""

from __future__ import annotations

import json
import subprocess

from .base import PASS, Probe, ProbeContext, ProbeResult
from ..logger import get_logger

log = get_logger("probes.subprocess")

DEFAULT_TIMEOUT_S = 60.0


class SubprocessProbe(Probe):
    """把外部可执行文件包装成 Probe。

    注册表配置::

        {"type": "subprocess",
         "command": ["node", "tests/<target>/probes/applog.js"],
         "cwd": ".",                  // 可选
         "env": {"FOO": "${FOO}"},    // 可选（在上层已展开 ${VAR}）
         "call_timeout_s": 60,        // 单次调用的进程超时（不是断言总预算）
         "config": {...}}
    """

    def __init__(self, name: str, command: list[str], config: dict | None = None,
                 cwd: str | None = None, env: dict | None = None,
                 call_timeout_s: float = DEFAULT_TIMEOUT_S):
        super().__init__(config)
        self.name = name
        self.command = list(command)
        self.cwd = cwd or None
        self.env_extra = dict(env or {})
        self.call_timeout_s = float(call_timeout_s or DEFAULT_TIMEOUT_S)

    # ── Probe 接口 ──

    def setup(self, ctx: ProbeContext) -> None:
        res = self._call("setup", ctx, {})
        if res.error:
            log.debug("probe %s setup 未处理（可忽略）: %s", self.name, res.error)

    def check(self, ctx: ProbeContext, args: dict) -> ProbeResult:
        return self._call("check", ctx, args)

    def teardown(self, ctx: ProbeContext) -> None:
        res = self._call("teardown", ctx, {})
        if res.error:
            log.debug("probe %s teardown 未处理（可忽略）: %s", self.name, res.error)

    # ── 内部 ──

    def _call(self, action: str, ctx: ProbeContext, args: dict) -> ProbeResult:
        import os

        payload = {
            "probe": self.name,
            "action": action,
            "args": args,
            "config": self.config,
            "context": ctx.to_dict(),
        }
        env = os.environ.copy()
        env.update({k: str(v) for k, v in self.env_extra.items()})

        try:
            proc = subprocess.run(
                self.command,
                input=json.dumps(payload, ensure_ascii=False, default=str),
                capture_output=True, text=True, timeout=self.call_timeout_s,
                cwd=self.cwd, env=env,
            )
        except FileNotFoundError:
            return ProbeResult.failed(f"probe 命令不存在: {self.command[0]}")
        except subprocess.TimeoutExpired:
            return ProbeResult.failed(
                f"probe 进程超过 {self.call_timeout_s:.0f}s 未返回（call_timeout_s）")
        except Exception as e:
            return ProbeResult.failed(f"probe 进程启动失败: {e}")

        if proc.stderr:
            log.debug("probe %s stderr: %s", self.name, proc.stderr.strip()[:2000])

        out = (proc.stdout or "").strip()
        if proc.returncode != 0:
            tail = (proc.stderr or out or "").strip()[-500:]
            return ProbeResult.failed(
                f"probe 退出码 {proc.returncode}"
                + (f"，stderr 末尾: {tail}" if tail else ""))
        if not out:
            # setup / teardown 不返回内容是允许的
            if action != "check":
                return ProbeResult(verdict=PASS)
            return ProbeResult.failed("probe stdout 为空（check 必须输出一个 JSON 对象）")

        try:
            raw = json.loads(_last_json_object(out))
        except ValueError as e:
            return ProbeResult.failed(
                f"probe stdout 不是合法 JSON ({e}): {out[:300]}")

        result = ProbeResult.from_dict(raw)
        # session 回写：让 probe 能在同一 case 内缓存东西（如 distinct_id）
        sess = raw.get("session")
        if isinstance(sess, dict):
            ctx.session.update(sess)
        return result


def _last_json_object(out: str) -> str:
    """容错：probe 不小心往 stdout 混了日志行时，取最后一个顶层 JSON 对象。

    协议要求 stdout 只有一个 JSON 对象，但外部脚本很容易顺手 print 一行调试。
    从末尾找到 ``}`` 再回溯配平出对应 ``{``，比整段 json.loads 更宽容。
    """
    s = out.strip()
    if s.startswith("{") and s.endswith("}"):
        return s
    end = s.rfind("}")
    if end < 0:
        return s
    depth, in_str = 0, None
    for i in range(end, -1, -1):
        c = s[i]
        if in_str:
            # 反向扫描字符串：遇到未被转义的同种引号即出串
            if c == in_str and not _escaped(s, i):
                in_str = None
            continue
        if c in "\"'":
            if not _escaped(s, i):
                in_str = c
            continue
        if c == "}":
            depth += 1
        elif c == "{":
            depth -= 1
            if depth == 0:
                return s[i:end + 1]
    return s


def _escaped(s: str, i: int) -> bool:
    """s[i] 前面有奇数个反斜杠 → 它是被转义的字符。"""
    n = 0
    i -= 1
    while i >= 0 and s[i] == "\\":
        n += 1
        i -= 1
    return n % 2 == 1
