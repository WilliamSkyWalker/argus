"""用例侧的 probe 声明解析 —— ``# argus-probe: <name> k=v ...``

放在某个 step 下面一行，绑定到**它上面最近的那个 step**：

    Then 上报首页曝光埋点
    # argus-probe: analytics check=首页曝光

写法约定（刻意让用例只表达**意图**）：
- 第一个 token = probe 名（注册表 ``.argus/probes.json`` 里的 key）
- 其余 ``k=v`` 原样透传给 probe 的 ``args``
- 用例里**别写 event 名 / 表名**这类实现细节 —— 用 ``check=<意图名>``，由
  probe 自己维护「意图 → 事件名 + 期望属性」的映射。换埋点方案（改事件名 /
  从查库改成查日志）时用例一个字都不用动。
- 值支持：裸 token（自动 coerce int/float/true/false/null）、``"引号串"``、
  ``{...}`` / ``[...]``（按 JSON 解析）。裸 key 无 ``=`` 视为 ``True``。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ..logger import get_logger

log = get_logger("probes.spec")

# 用例里的 probe 声明行。gherkin.py 解析 .feature 时把这行原样保留进 steps
# （渲染进 case body），agent 再从 case body 里按 step 顺序反查绑定关系 ——
# 与 `- **Ref**:` 同一套「元数据搭 case 文本便车」的做法。
PROBE_DIRECTIVE_RE = re.compile(r'^\s*#\s*argus-probe:\s*(.+?)\s*$')

# 同上但按多行搜索整段文本（`--only-probes` 筛 case 用；注意不能拿上面那条
# 无 re.M 的正则去 .search 多行文本 —— ^ 只会匹配串首，一律不命中）
PROBE_DIRECTIVE_SEARCH_RE = re.compile(
    r'^[ \t]*#[ \t]*argus-probe:[ \t]*\S.*$', re.MULTILINE)


def has_probe_directive(text: str) -> bool:
    """整段文本里有没有 probe 声明（case 级筛选用）。"""
    return bool(PROBE_DIRECTIVE_SEARCH_RE.search(text or ""))


@dataclass
class ProbeSpec:
    """一条 probe 声明。"""

    name: str
    args: dict = field(default_factory=dict)
    raw: str = ""

    def summary(self) -> str:
        """给日志 / 报告用的一行摘要。"""
        if not self.args:
            return self.name
        kv = " ".join(f"{k}={_fmt(v)}" for k, v in self.args.items())
        return f"{self.name} {kv}"


def _fmt(v) -> str:
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


def parse_directive(text: str) -> ProbeSpec | None:
    """解析 directive 正文（``# argus-probe:`` 之后的部分）。

    正文为空 / 只有空白 → 返回 None（调用方记 warning）。
    """
    body = text.strip()
    if not body:
        return None
    # probe 名 = 第一个空白前的 token
    parts = body.split(None, 1)
    name = parts[0].strip()
    if not name or "=" in name:
        log.warning("argus-probe 声明缺少 probe 名: %r", text)
        return None
    args = _parse_args(parts[1]) if len(parts) > 1 else {}
    return ProbeSpec(name=name, args=args, raw=body)


def parse_directive_line(line: str) -> ProbeSpec | None:
    """整行解析（含 ``# argus-probe:`` 前缀）。不是 directive 行则返回 None。"""
    m = PROBE_DIRECTIVE_RE.match(line)
    if not m:
        return None
    return parse_directive(m.group(1))


# ─────────────────────────────────────────────────────────
# k=v 解析
# ─────────────────────────────────────────────────────────

def _parse_args(s: str) -> dict:
    args: dict = {}
    i, n = 0, len(s)
    while i < n:
        while i < n and s[i].isspace():
            i += 1
        if i >= n:
            break
        j = i
        while j < n and not s[j].isspace() and s[j] != "=":
            j += 1
        key = s[i:j]
        if not key:
            break
        if j >= n or s[j] != "=":
            args[key] = True          # 裸 flag
            i = j
            continue
        val, i = _read_value(s, j + 1)
        args[key] = val
    return args


def _read_value(s: str, i: int) -> tuple[object, int]:
    n = len(s)
    if i >= n or s[i].isspace():
        return "", i
    ch = s[i]

    # "quoted" / 'quoted'
    if ch in "\"'":
        buf: list[str] = []
        j = i + 1
        while j < n and s[j] != ch:
            if s[j] == "\\" and j + 1 < n:
                buf.append(s[j + 1])
                j += 2
                continue
            buf.append(s[j])
            j += 1
        return "".join(buf), min(j + 1, n)

    # JSON object / array（按嵌套配平扫到闭合符，字符串内的括号不计数）
    if ch in "{[":
        close = "}" if ch == "{" else "]"
        depth, j, in_str = 0, i, None
        while j < n:
            c = s[j]
            if in_str:
                if c == "\\":
                    j += 2
                    continue
                if c == in_str:
                    in_str = None
            elif c in "\"'":
                in_str = c
            elif c == ch:
                depth += 1
            elif c == close:
                depth -= 1
                if depth == 0:
                    j += 1
                    break
            j += 1
        raw = s[i:j]
        try:
            return json.loads(raw), j
        except ValueError:
            log.warning("argus-probe 参数不是合法 JSON，按字符串透传: %s", raw)
            return raw, j

    # 裸 token
    j = i
    while j < n and not s[j].isspace():
        j += 1
    return _coerce(s[i:j]), j


_INT_RE = re.compile(r'^[+-]?(0|[1-9]\d*)$')      # 前导 0 的串（"007"）按字符串留着
_FLOAT_RE = re.compile(r'^[+-]?\d+\.\d+$')


def _coerce(tok: str):
    low = tok.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low in ("null", "none"):
        return None
    if _INT_RE.match(tok):
        return int(tok)
    if _FLOAT_RE.match(tok):
        return float(tok)
    return tok
