"""Probe 注册表 + 执行器 —— 用例侧声明的 ``# argus-probe:`` 由这里落地。

职责分工（刻意让「怎么查」完全在用户手里）：

- **argus 管**：什么时候查（哪个 step）、查什么（directive 里的意图 + 上下文）、
  查不到时等多久重试、结果怎么进报告和 evidence 链。
- **插件管**：怎么查。查数据仓库落库表 / grep 上报日志 / 读抓包 dump / 调后端 API
  都行，argus 不关心。

注册表默认读 ``.argus/probes.json``（gitignored，跟 mcp_clients.json 同一档 ——
里面有连接串/密钥，绝不入库）；``ARGUS_PROBES_CONFIG`` 可指别处。格式::

    {
      "defaults": {"timeout_s": 300, "poll_interval_s": 20},
      "probes": {
        "analytics": {
          "type": "python",
          "module": "tests/<target>/probes/analytics.py",
          "class": "AnalyticsProbe",
          "config": {"dsn": "${ANALYTICS_DSN}", "table": "…"}
        },
        "applog": {
          "type": "subprocess",
          "command": ["node", "tests/<target>/probes/applog.js"],
          "call_timeout_s": 60,
          "config": {}
        }
      }
    }

``${VAR}`` 会用环境变量展开（同 mcp/client.py 的约定），所以真值可以只放 .env。
"""

from __future__ import annotations

import atexit
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path

from .base import (FAIL, INCONCLUSIVE, PASS, Probe, ProbeContext, ProbeResult,
                   VALID_VERDICTS)
from .spec import (PROBE_DIRECTIVE_RE, PROBE_DIRECTIVE_SEARCH_RE, ProbeSpec,
                   has_probe_directive, parse_directive, parse_directive_line)
from ..logger import get_logger

log = get_logger("probes")

DEFAULT_CONFIG_PATH = Path(".argus") / "probes.json"

# 兜底默认值（注册表 defaults 块 / 单个 probe 块都可覆盖）
DEFAULT_TIMEOUT_S = 300.0
DEFAULT_POLL_INTERVAL_S = 20.0
# probe 给的 retry_after_s 再小也不允许比这更密（防插件返回 0 把 CPU 打满）
MIN_POLL_INTERVAL_S = 2.0
# 单个 step 内最多查多少次（配合 timeout 双保险，防 retry_after 被设成极小值）
MAX_ATTEMPTS_PER_STEP = 100
# 进报告的 data 序列化上限（防一次查询把几万行塞进 HTML）
MAX_DATA_CHARS = 20000

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _expand_env(value):
    """递归展开 ``${VAR}``（同 mcp/client.py 的约定）。未定义的变量替换成空串。"""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


# ─────────────────────────────────────────────────────────
# 注册表
# ─────────────────────────────────────────────────────────


@dataclass
class ProbeEntry:
    name: str
    kind: str                      # "python" | "subprocess"
    raw: dict = field(default_factory=dict)
    timeout_s: float = DEFAULT_TIMEOUT_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S


@dataclass
class ProbeRegistry:
    """probes.json 的内存形态 —— 只存声明，实例按需懒加载。"""

    entries: dict[str, ProbeEntry] = field(default_factory=dict)
    config_path: str = ""

    @classmethod
    def from_config(cls, path: str | Path | None = None) -> "ProbeRegistry":
        p = Path(path or os.environ.get("ARGUS_PROBES_CONFIG") or DEFAULT_CONFIG_PATH)
        if not p.exists():
            return cls(config_path=str(p))
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception as e:
            log.error("probes 注册表解析失败 %s: %s", p, e)
            return cls(config_path=str(p))

        defaults = data.get("defaults") or {}
        d_timeout = _as_float(defaults.get("timeout_s"), DEFAULT_TIMEOUT_S)
        d_poll = _as_float(defaults.get("poll_interval_s"), DEFAULT_POLL_INTERVAL_S)

        entries: dict[str, ProbeEntry] = {}
        for name, raw in (data.get("probes") or {}).items():
            if not isinstance(raw, dict):
                log.warning("probe %s 配置不是对象，跳过", name)
                continue
            kind = str(raw.get("type") or ("subprocess" if raw.get("command") else "python"))
            if kind not in ("python", "subprocess"):
                log.warning("probe %s 的 type=%s 不支持（python|subprocess），跳过", name, kind)
                continue
            entries[name] = ProbeEntry(
                name=name, kind=kind, raw=raw,
                timeout_s=_as_float(raw.get("timeout_s"), d_timeout),
                poll_interval_s=_as_float(raw.get("poll_interval_s"), d_poll),
            )
        return cls(entries=entries, config_path=str(p))

    def __bool__(self) -> bool:
        return bool(self.entries)

    def names(self) -> list[str]:
        return sorted(self.entries)


def _as_float(v, default: float) -> float:
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


# ─────────────────────────────────────────────────────────
# 实例化
# ─────────────────────────────────────────────────────────


def build_probe(entry: ProbeEntry) -> Probe:
    """按注册表条目实例化一个 Probe（失败抛异常，调用方转成 step fail）。"""
    raw = _expand_env(entry.raw)
    cfg = raw.get("config") or {}
    if not isinstance(cfg, dict):
        raise ValueError(f"probe {entry.name} 的 config 必须是对象")

    if entry.kind == "subprocess":
        from .subprocess_probe import SubprocessProbe
        command = raw.get("command")
        if isinstance(command, str):
            command = [command]
        if not command or not isinstance(command, list):
            raise ValueError(f"probe {entry.name} 缺少 command（subprocess 型必填）")
        return SubprocessProbe(
            name=entry.name, command=[str(c) for c in command], config=cfg,
            cwd=raw.get("cwd"), env=raw.get("env") or {},
            call_timeout_s=_as_float(raw.get("call_timeout_s"), 60.0),
        )

    # python 型
    module_ref = raw.get("module") or raw.get("path")
    if not module_ref:
        raise ValueError(f"probe {entry.name} 缺少 module（python 型必填：文件路径或点分模块名）")
    mod = _import_module(str(module_ref))
    cls = _resolve_probe_class(mod, raw.get("class"), entry.name)
    inst = cls(cfg)
    if not inst.name or inst.name == "base":
        inst.name = entry.name
    return inst


def _import_module(ref: str):
    """支持两种写法：文件路径（含 ``/`` 或以 .py 结尾）/ 点分模块名。"""
    import importlib
    import importlib.util

    if ref.endswith(".py") or "/" in ref or os.sep in ref:
        p = Path(ref).expanduser()
        if not p.is_absolute():
            p = (Path.cwd() / p).resolve()
        if not p.is_file():
            raise FileNotFoundError(f"probe 模块文件不存在: {p}")
        spec = importlib.util.spec_from_file_location(f"argus_probe_{p.stem}", p)
        if spec is None or spec.loader is None:
            raise ImportError(f"无法加载 probe 模块: {p}")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    return importlib.import_module(ref)


def _resolve_probe_class(mod, class_name: str | None, probe_name: str):
    if class_name:
        cls = getattr(mod, str(class_name), None)
        if cls is None:
            raise AttributeError(f"probe 模块里没有类 {class_name}")
        return cls
    # 没指定 class：找模块里唯一的 Probe 子类（排除基类本身）
    candidates = [
        obj for obj in vars(mod).values()
        if isinstance(obj, type) and issubclass(obj, Probe) and obj is not Probe
        and obj.__module__ == mod.__name__
    ]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise AttributeError(
            f"probe {probe_name} 的模块里找不到 Probe 子类"
            f"（继承 argus.probes.base.Probe，或用 \"class\" 指定类名）")
    raise AttributeError(
        f"probe {probe_name} 的模块里有 {len(candidates)} 个 Probe 子类，"
        f"请用 \"class\" 指定用哪个: {[c.__name__ for c in candidates]}")


# ─────────────────────────────────────────────────────────
# 执行器（agent 用）
# ─────────────────────────────────────────────────────────


class ProbeRunner:
    """一个 Agent 一个 runner：管实例缓存、per-case setup/teardown、单次 check。

    **轮询不在这里做** —— agent 主循环每 turn 调一次 ``check``，把 inconclusive
    当作「本 turn 没推进但不算无进展」，这样每次尝试都有独立的报告条目和日志，
    跟现有 wait 动作同一套语义。预算判定用 ``entry.timeout_s``。
    """

    def __init__(self, config: dict | None = None):
        cfg = config or {}
        probes_cfg = cfg.get("probes") or {}
        self.registry = ProbeRegistry.from_config(probes_cfg.get("config_path") or None)
        # .env 里的全局覆盖（PROBE_TIMEOUT_S / PROBE_POLL_INTERVAL_S）
        self._timeout_override = probes_cfg.get("timeout_s")
        self._poll_override = probes_cfg.get("poll_interval_s")
        self._instances: dict[str, Probe] = {}
        self._sessions: dict[str, dict] = {}
        self._setup_done: set[str] = set()
        self._case_ctx: ProbeContext | None = None
        self._closed = False
        atexit.register(self._atexit_close)
        if self.registry:
            log.info("probes 注册表: %d 个 → %s（%s）",
                     len(self.registry.entries), self.registry.names(),
                     self.registry.config_path)

    # ── 注册表状态 ──

    @property
    def available(self) -> bool:
        return bool(self.registry)

    def has(self, name: str) -> bool:
        return name in self.registry.entries

    def limits(self, name: str) -> tuple[float, float]:
        """返回 (总预算秒, 轮询间隔秒)。.env 覆盖 > 单 probe > defaults > 内置。"""
        entry = self.registry.entries.get(name)
        timeout = entry.timeout_s if entry else DEFAULT_TIMEOUT_S
        poll = entry.poll_interval_s if entry else DEFAULT_POLL_INTERVAL_S
        if self._timeout_override:
            timeout = _as_float(self._timeout_override, timeout)
        if self._poll_override:
            poll = _as_float(self._poll_override, poll)
        return timeout, max(MIN_POLL_INTERVAL_S, poll)

    def missing_reason(self, name: str) -> str:
        """probe 名不可用时给一句能直接照做的报错（会进报告，写清楚怎么修）。"""
        if not self.registry:
            return (f"用例声明了 probe「{name}」但没有可用的 probes 注册表"
                    f"（找不到 {self.registry.config_path}）。"
                    f"非视觉断言拿不到验证通道 → 判 fail，绝不静默放过。"
                    f"修法：配置 {self.registry.config_path}（可 cp .argus/probes.json.example）")
        return (f"用例声明了 probe「{name}」，但注册表 {self.registry.config_path} "
                f"里没有这个名字。已注册: {self.registry.names()}")

    # ── per-case 生命周期 ──

    def begin_case(self, ctx: ProbeContext) -> None:
        """开始一个新 case：先给上一个 case 收尾，再重置 session / setup 标记。"""
        self.end_case()
        self._case_ctx = ctx
        self._sessions = {}
        self._setup_done = set()

    def end_case(self) -> None:
        """给当前 case 收尾（调各 probe 的 teardown，best-effort）。"""
        if self._case_ctx is None:
            return
        for name in list(self._setup_done):
            inst = self._instances.get(name)
            if inst is None:
                continue
            try:
                ctx = self._case_ctx
                ctx.session = self._sessions.setdefault(name, {})
                inst.teardown(ctx)
            except Exception as e:
                log.warning("probe %s teardown 异常（忽略）: %s", name, e)
        self._setup_done = set()
        self._case_ctx = None

    def _atexit_close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self.end_case()
        except Exception:
            pass

    # ── 单次检查 ──

    def check(self, spec: ProbeSpec, ctx: ProbeContext) -> ProbeResult:
        """跑一次 probe。**永不抛异常** —— 插件炸了转成 ProbeResult.failed
        （= inconclusive + error），由 agent 的预算逻辑决定重试还是判 fail。"""
        entry = self.registry.entries.get(spec.name)
        if entry is None:
            return ProbeResult(verdict=FAIL, evidence=self.missing_reason(spec.name),
                               error="probe not registered")

        ctx.session = self._sessions.setdefault(spec.name, {})
        try:
            inst = self._get_instance(spec.name, entry)
        except Exception as e:
            return ProbeResult.failed(f"probe「{spec.name}」加载失败: {e}")

        if spec.name not in self._setup_done:
            self._setup_done.add(spec.name)
            try:
                inst.setup(ctx)
            except Exception as e:
                log.warning("probe %s setup 异常（继续 check）: %s", spec.name, e)

        t0 = time.time()
        try:
            result = inst.check(ctx, dict(spec.args))
        except Exception as e:
            log.warning("probe %s check 抛异常: %s", spec.name, e)
            result = ProbeResult.failed(f"{type(e).__name__}: {e}")
        if not isinstance(result, ProbeResult):
            result = ProbeResult.failed(
                f"probe check 返回了 {type(result).__name__}，应返回 ProbeResult")
        if result.verdict not in VALID_VERDICTS:
            result = ProbeResult.failed(f"probe 返回了无效 verdict {result.verdict!r}")

        log.info("probe %s → %s (%.2fs, attempt %d): %s",
                 spec.name, result.verdict, time.time() - t0, ctx.attempt,
                 (result.evidence or result.error or "")[:200])
        return result

    def _get_instance(self, name: str, entry: ProbeEntry) -> Probe:
        inst = self._instances.get(name)
        if inst is None:
            inst = build_probe(entry)
            self._instances[name] = inst
            log.info("probe %s 已加载 (%s)", name, entry.kind)
        return inst


def summarize_data(data: dict) -> str:
    """把 probe 的 data 序列化成进报告的字符串（超长截断，不可序列化转 str）。"""
    if not data:
        return ""
    try:
        s = json.dumps(data, ensure_ascii=False, indent=2, default=str)
    except Exception as e:
        return f"<data 序列化失败: {e}>"
    if len(s) > MAX_DATA_CHARS:
        return s[:MAX_DATA_CHARS] + f"\n… (已截断，原始 {len(s)} 字符)"
    return s


__all__ = [
    "PASS", "FAIL", "INCONCLUSIVE", "VALID_VERDICTS",
    "Probe", "ProbeContext", "ProbeResult", "ProbeSpec",
    "ProbeEntry", "ProbeRegistry", "ProbeRunner",
    "PROBE_DIRECTIVE_RE", "PROBE_DIRECTIVE_SEARCH_RE", "has_probe_directive",
    "parse_directive", "parse_directive_line",
    "build_probe", "summarize_data",
    "DEFAULT_TIMEOUT_S", "DEFAULT_POLL_INTERVAL_S", "MIN_POLL_INTERVAL_S",
    "MAX_ATTEMPTS_PER_STEP",
]
