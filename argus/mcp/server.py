"""argus MCP server — stdio transport.

把 argus 的能力暴露成 MCP tools，让 Claude Code / Desktop / Cursor 这类
MCP-aware 客户端不用每次 spawn ``python3 -m argus.cli`` 就能：
  - 只读：list_targets / list_cases / list_runs / get_run_status / get_report
  - 跑测：run_target / run_case / cancel_run
  - 设备：list_devices / install_apk / adb_reconnect / setup_simulator
  - 设备原语（/argus-drive 用，Claude 当 brain）：device_screenshot /
    device_tap / device_input / device_swipe / device_key /
    device_launch —— 通过共享持久会话服务控制 Android/iOS、浏览器和桌面

启动:
    python3 -m argus.mcp.server

stdio 注意事项:
- FastMCP 用 stdout 跑 JSON-RPC，任何 print() 都会污染 protocol stream。
- argus.logger 已经走 stderr 没问题；commands.background 的 _launch_background 和 qa.device_setup 的
  _install_apk_on_devices / _ensure_devices_connected 有 print，本模块通过
  ``_silenced_stdout`` 把它们的 stdout 重定向到 stderr（客户端 debug log 可见）。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

# mcp SDK 的服务端类在 2.0 改了名和位置：FastMCP → MCPServer（`mcp.server`）。
# 我们只用 tool() / remove_tool() / run() 三个接口，两版签名兼容，故按版本取类即可。
try:                                          # mcp >= 2.0
    from mcp.server import MCPServer as _MCPServerClass
except ImportError:                           # mcp 1.x
    from mcp.server.fastmcp import FastMCP as _MCPServerClass

from argus.commands.background import RUNS_DIR, _check_run_status, _launch_background, _resolve_report_path
from argus.qa.cases import TESTS_DIR, _resolve_test_target
from argus.qa.device_setup import _ensure_devices_connected, _install_apk_on_devices

from argus.qa.gherkin import parse_feature_file
from argus.devices.simulator import boot, create_device, list_devices as _list_ios_devices

mcp = _MCPServerClass("argus")


@contextlib.contextmanager
def _silenced_stdout():
    """Redirect stdout → stderr for the duration of the block.

    Used to wrap command helpers that print human-readable progress —
    we keep the messages visible in MCP client debug logs (which surface
    stderr) without corrupting the stdio JSON-RPC channel.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


def _read_meta(meta_file: Path, retries: int = 3, delay: float = 0.05) -> dict | None:
    """读 meta.json，容忍 cli 并发原地重写导致的瞬时截断。

    连续 retries 次解析失败返回 None，调用方给出明确错误信息。
    """
    for i in range(retries):
        try:
            return json.loads(meta_file.read_text())
        except (json.JSONDecodeError, OSError):
            if i < retries - 1:
                time.sleep(delay)
    return None


# ──────────────────────────────────────────────────────────────────
# Read-only tools
# ──────────────────────────────────────────────────────────────────


@mcp.tool()
def list_targets() -> list[dict]:
    """列 tests/ 下所有可用 target。

    每个 target 返回:
      - name: 目录名（如 "my-app" / "web-demo"）
      - path: 绝对路径
      - feature_files / md_files: 各格式 case 文件计数
      - reports: 历史报告数
      - description: README.md 第一行非标题文本
    """
    out: list[dict] = []
    if not TESTS_DIR.exists():
        return out
    for d in sorted(TESTS_DIR.iterdir()):
        if not d.is_dir() or d.name.startswith("_") or d.name.startswith("."):
            continue
        feature_files = list(d.glob("**/*.feature"))
        md_files: list[Path] = []
        cases_dir = d / "cases"
        if cases_dir.is_dir():
            md_files = list(cases_dir.glob("*.md"))
        if not feature_files and not md_files:
            continue
        reports_dir = d / "reports"
        reports = list(reports_dir.glob("**/*.html")) if reports_dir.exists() else []
        desc = ""
        readme = d / "README.md"
        if readme.exists():
            for line in readme.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("["):
                    desc = line[:140]
                    break
        out.append({
            "name": d.name,
            "path": str(d),
            "feature_files": len(feature_files),
            "md_files": len(md_files),
            "reports": len(reports),
            "description": desc,
        })
    return out


@mcp.tool()
def list_cases(target: str, limit: int = 100, offset: int = 0) -> dict:
    """列 target 下所有 .feature scenario 的结构化元数据。

    target 接受三种形式:
      - target 名: "my-app" / "web-demo"
      - 子目录: "my-app/mobile/02-home"
      - 单文件: 任意 .feature 路径（绝对 / 相对 cwd / TESTS_DIR 相对）

    返回每个 scenario 的 tc_id / feature / file / priority / automation /
    platform / tags（来自 @tag 解析）。.md 旧格式不展开 scenario，会被忽略。

    分页：默认每页 limit=100 条 scenario（大 target 上千 scenario 会撑爆
    client token 上限，故必须分页）。case_count 始终是全量计数；truncated=True
    表示还有更多，用 next_offset 继续翻页。file 字段是相对 base_path 的路径
    （省去逐条重复绝对路径前缀的冗余）。
    """
    candidates = [Path(target), TESTS_DIR / target]
    base: Path | None = None
    for c in candidates:
        if c.exists():
            base = c
            break
    if base is None:
        return {"target": target, "error": "target not found", "cases": []}

    if base.is_file() and base.suffix == ".feature":
        files = [base]
    elif base.is_dir():
        files = sorted(base.glob("**/*.feature"))
    else:
        files = []

    base_dir = base.parent if base.is_file() else base

    def _rel(f: Path) -> str:
        try:
            return str(f.relative_to(base_dir))
        except ValueError:
            return str(f)

    scenarios: list[dict] = []
    for f in files:
        try:
            for _body, meta in parse_feature_file(f):
                scenarios.append({
                    "tc_id": meta.get("tc_id"),
                    "feature": meta.get("feature_name"),
                    "file": _rel(f),
                    "priority": meta.get("priority"),
                    "automation": meta.get("automation"),
                    "platform": meta.get("platform"),
                    "reset_mode": meta.get("reset_mode"),
                    "tags": meta.get("tags", []),
                })
        except Exception as e:
            scenarios.append({"file": _rel(f), "error": str(e)})

    total = len(scenarios)
    if offset < 0:
        offset = 0
    page = scenarios[offset:offset + limit] if limit and limit > 0 else scenarios[offset:]
    end = offset + len(page)
    truncated = end < total

    result = {
        "target": target,
        "base_path": str(base),
        "feature_count": len(files),
        "case_count": total,
        "offset": offset,
        "returned": len(page),
        "truncated": truncated,
        "cases": page,
    }
    if truncated:
        result["next_offset"] = end
    return result


@mcp.tool()
def list_runs(limit: int = 20) -> list[dict]:
    """列最近的后台 run 任务（默认 20 条），按时间倒序。

    每条返回 run_id / status / test / platform / device / started_at /
    report / log / pid。status 由进程探活 + report 存在性推断:
    "运行中" / "已完成" / "异常退出"。
    """
    if not RUNS_DIR.exists():
        return []
    out: list[dict] = []
    for run_dir in sorted(RUNS_DIR.iterdir(), reverse=True)[:limit]:
        meta_file = run_dir / "meta.json"
        if not meta_file.exists():
            continue
        meta = _read_meta(meta_file)
        if meta is None:
            continue
        status = _check_run_status(meta, run_dir)
        out.append({
            "run_id": meta.get("run_id"),
            "status": status,
            "test": meta.get("test", ""),
            "platform": meta.get("platform"),
            "device": meta.get("device"),
            "started_at": meta.get("started_at"),
            "report": meta.get("report"),
            "log": meta.get("log"),
            "pid": meta.get("pid"),
        })
    return out


@mcp.tool()
def get_run_status(run_id: str, tail_lines: int = 30) -> dict:
    """查指定后台 run 的状态 + 日志 tail。

    run_id 从 list_runs / run_target 返回值拿。tail_lines 控制 log_tail 长度。
    若已经跑完并写出 .json 报告，summary 字段含 passed/failed/total 速读。
    """
    run_dir = RUNS_DIR / run_id
    meta_file = run_dir / "meta.json"
    if not meta_file.exists():
        return {"run_id": run_id, "error": "run not found"}
    meta = _read_meta(meta_file)
    if meta is None:
        return {"run_id": run_id,
                "error": "meta.json 解析失败（可能正被并发重写），请稍后重试"}
    status = _check_run_status(meta, run_dir)

    log_tail: list[str] = []
    log_path_str = meta.get("log", "")
    if log_path_str:
        log_path = Path(log_path_str)
        if log_path.exists():
            try:
                lines = log_path.read_text().splitlines()
                log_tail = lines[-tail_lines:] if len(lines) > tail_lines else lines
            except Exception as e:
                log_tail = [f"<log read error: {e}>"]

    summary = None
    report_path = meta.get("report", "")
    if report_path and report_path != "__auto__" and Path(report_path).exists():
        json_p = Path(report_path)
        if json_p.suffix == ".html":
            json_p = json_p.with_suffix(".json")
        if json_p.suffix == ".json" and json_p.exists():
            try:
                data = json.loads(json_p.read_text())
                summary = data.get("summary")
            except Exception:
                pass

    return {
        "run_id": run_id,
        "status": status,
        "pid": meta.get("pid"),
        "started_at": meta.get("started_at"),
        "test": meta.get("test"),
        "platform": meta.get("platform"),
        "device": meta.get("device"),
        "report_path": report_path,
        "log_path": log_path_str,
        "log_tail": log_tail,
        "summary": summary,
    }


def _case_label(case_text: str | None, limit: int = 120) -> str | None:
    """从 case 全文里抽一个轻量标签供 summary 用。

    case 文本通常被 _preconditions.md auto-prepend（可达数 KB），summary
    不该把整段塞回去。优先取 `### TC-...` 标题行，否则取首个非空行，再
    截断到 limit 字符。
    """
    if not case_text:
        return case_text
    # case 文本可能被 _preconditions.md（自带 ### 小标题）prepend，真正的用例
    # 标题是 `### TC-...`（cli 按此切块），优先精确匹配它，避免抓到 preconditions
    # 里的子标题。
    title = None
    fallback = None
    for line in case_text.splitlines():
        s = line.strip()
        if not s:
            continue
        if fallback is None:
            fallback = s
        if s.startswith("### TC-"):
            title = s.lstrip("# ").strip()
            break
    title = title or fallback or case_text
    return title if len(title) <= limit else title[:limit] + "…"


@mcp.tool()
def get_report(run_id: str | None = None,
               report_path: str | None = None,
               format: str = "json") -> dict:
    """读测试报告。

    run_id 或 report_path 二选一。format:
      - "json": 返回报告完整结构（含每 case 的 result / reason / steps_detail）
      - "summary": 只返回 summary + 每 case 一行结论（轻量）
      - "html_path": 只返回 HTML 路径（让客户端自己打开浏览器）
    """
    if run_id:
        run_dir = RUNS_DIR / run_id
        meta_file = run_dir / "meta.json"
        if not meta_file.exists():
            return {"error": f"run {run_id} not found"}
        meta = _read_meta(meta_file)
        if meta is None:
            return {"error": f"run {run_id} 的 meta.json 解析失败"
                             "（可能正被并发重写），请稍后重试"}
        # __auto__ 报告路径由子进程在 log 里给出，这里 resolve + 回填 meta
        report_path = _resolve_report_path(meta, run_dir)

    if not report_path:
        return {"error": "must provide run_id or report_path"}
    if report_path == "__auto__":
        return {"error": "report is still '__auto__' (run not finished or failed before writing)"}

    p = Path(report_path)
    if not p.exists():
        return {"error": f"report not found: {report_path}"}

    if format == "html_path":
        return {"path": str(p),
                "format": "html" if p.suffix == ".html" else p.suffix.lstrip(".")}

    # 找伴生 .json
    json_p = p if p.suffix == ".json" else p.with_suffix(".json")
    if not json_p.exists():
        return {
            "error": "no JSON companion report; use format='html_path' or open the HTML",
            "html_path": str(p),
        }

    try:
        data = json.loads(json_p.read_text())
    except Exception as e:
        return {"error": f"failed to parse {json_p}: {e}"}

    if format == "summary":
        # save_json 的 per-case 列表键是 "test_cases"（见 report.save_json）
        cases = data.get("test_cases", []) if isinstance(data, dict) else []
        return {
            "summary": data.get("summary") if isinstance(data, dict) else None,
            "cases": [
                {
                    "case": _case_label(c.get("case")),
                    "result": c.get("result"),
                    "reason": c.get("reason"),
                    "duration": c.get("duration"),
                    "steps": c.get("steps"),
                }
                for c in cases
            ],
        }
    return data


# ──────────────────────────────────────────────────────────────────
# Run tools
# ──────────────────────────────────────────────────────────────────


def _run_target_impl(target: str,
                     platform: str | None = None,
                     devices: list[str] | None = None,
                     apk: str | None = None,
                     url: str | None = None,
                     max_steps: int | None = None,
                     grid: str | None = None,
                     shard: str | None = None,
                     probes: str | None = None) -> dict:
    devices = list(devices or [])
    # 非视觉断言（probe 插件）偏置：走 child env，**不动** MCP server 自己的
    # os.environ（server 是长驻进程，改全局会污染后续每个 run）
    probes_mode = (probes or "all").strip().lower()
    if probes_mode not in ("all", "skip", "only"):
        return {"error": f"probes={probes!r} 无效，可选 all|skip|only"}
    # 指定了 apk 却没给 devices 时不能静默跳过安装（跑测前必须重装 APK）
    if apk and not devices:
        return {"error": "指定了 apk 但 devices 为空 — 不会静默跳过安装，"
                         "请显式传 devices 列表（跑测前必须重装 APK）"}
    with _silenced_stdout():
        if devices:
            devices = _ensure_devices_connected(devices)
        if apk and devices:
            devices = _install_apk_on_devices(apk, devices)

        ns = argparse.Namespace(
            test=target,
            platform=platform,
            url=url,
            max_steps=max_steps,
            report="__auto__",
            grid=grid,
            bg=True,
            device=devices,
            apk=apk,
            shard=shard,
        )

        primary = devices[0] if devices else None
        extras = devices[1:] if len(devices) > 1 else None
        run_id = _launch_background(
            ns, android_serial=primary, extra_devices=extras, shard=shard,
            extra_env={"PROBES_MODE": probes_mode} if probes_mode != "all" else None,
        )

    meta_file = RUNS_DIR / run_id / "meta.json"
    meta = json.loads(meta_file.read_text()) if meta_file.exists() else {}
    return {
        "run_id": run_id,
        "pid": meta.get("pid"),
        "test": meta.get("test"),
        "platform": platform,
        "devices": devices,
        "report_path": meta.get("report"),
        "log_path": meta.get("log"),
        "started_at": meta.get("started_at"),
        "hint": "用 get_run_status(run_id) 轮询；跑完 get_report(run_id) 拿结果",
    }


@mcp.tool()
async def run_target(target: str,
                     platform: str | None = None,
                     devices: list[str] | None = None,
                     apk: str | None = None,
                     url: str | None = None,
                     max_steps: int | None = None,
                     grid: str | None = None,
                     shard: str | None = None,
                     probes: str | None = None) -> dict:
    """后台启动一个测试 run，立即返回 run_id（不阻塞 MCP 调用）。

    用 get_run_status(run_id) 轮询；跑完 get_report(run_id) 读结果。

    参数:
      target: target 名 / 子目录 / .feature 文件路径 / inline 文本
      platform: "ios" | "android" | "browser"（缺省读 .env PLATFORM）
      devices: Android serial 列表；>1 时走多设备调度器 + 账号池绑定
      apk: APK 路径，跑前 3 次重试并行装到所有 devices（失败的设备剔除）
      url: 浏览器起始 URL（覆盖 README 提取）
      max_steps: 单 case 兜底 max_steps（覆盖 .env）
      grid: Selenium Grid URL，搭配 -j 并发
      shard: "N/M" 手动切片（取 cases[N*total/M : (N+1)*total/M]）
      probes: 非视觉断言（埋点等 probe 插件）偏置 —— "all"(默认) |
        "skip"(probe step 标 skip 不验证) | "only"(只跑声明了 probe 的 case，
        其 UI 步照跑，因为埋点得靠操作触发)
    """
    # 设备连接 + APK 安装可阻塞数分钟，丢线程池跑，不冻结 JSON-RPC 事件循环
    return await asyncio.to_thread(
        _run_target_impl,
        target=target, platform=platform, devices=devices, apk=apk,
        url=url, max_steps=max_steps, grid=grid, shard=shard, probes=probes,
    )


@mcp.tool()
async def run_case(case_path: str,
                   platform: str | None = None,
                   devices: list[str] | None = None,
                   apk: str | None = None,
                   url: str | None = None,
                   max_steps: int | None = None,
                   grid: str | None = None,
                   probes: str | None = None) -> dict:
    """run_target 的语义化别名 — 跑单 .feature / 单 case / inline 文本。

    与 run_target 等价（实现完全一致），方便客户端意图清晰区分批量 vs 单点。
    """
    return await asyncio.to_thread(
        _run_target_impl,
        target=case_path, platform=platform, devices=devices, apk=apk,
        url=url, max_steps=max_steps, grid=grid, probes=probes,
    )


def _cancel_run_impl(run_id: str) -> dict:
    run_dir = RUNS_DIR / run_id
    meta_file = run_dir / "meta.json"
    if not meta_file.exists():
        return {"run_id": run_id, "error": "run not found"}
    meta = _read_meta(meta_file)
    if meta is None:
        return {"run_id": run_id,
                "error": "meta.json 解析失败（可能正被并发重写），请稍后重试"}
    pid = meta.get("pid")
    if not pid:
        return {"run_id": run_id, "error": "no pid in meta"}

    try:
        pgid = os.getpgid(pid)
    except ProcessLookupError:
        return {"run_id": run_id, "result": "already dead"}

    try:
        os.killpg(pgid, signal.SIGTERM)
    except ProcessLookupError:
        return {"run_id": run_id, "result": "already dead"}
    except Exception as e:
        return {"run_id": run_id, "error": str(e)}

    time.sleep(2)
    try:
        os.kill(pid, 0)
        try:
            os.killpg(pgid, signal.SIGKILL)
            return {"run_id": run_id, "result": "SIGKILL"}
        except ProcessLookupError:
            return {"run_id": run_id, "result": "SIGTERM"}
    except ProcessLookupError:
        return {"run_id": run_id, "result": "SIGTERM"}


@mcp.tool()
async def cancel_run(run_id: str) -> dict:
    """终止后台 run。

    先给进程组 SIGTERM 留 2s graceful，仍存活则 SIGKILL。
    """
    # 内部 sleep(2) 等 graceful 退出，丢线程池跑避免冻结事件循环
    return await asyncio.to_thread(_cancel_run_impl, run_id)


# ──────────────────────────────────────────────────────────────────
# Device tools
# ──────────────────────────────────────────────────────────────────


@mcp.tool()
def list_devices() -> dict:
    """列所有可用设备：iOS simulator (xcrun simctl) + Android (adb devices -l)."""
    ios_out: list[dict] = []
    try:
        for d in _list_ios_devices():
            ios_out.append({"name": d.name, "udid": d.udid, "state": d.state})
    except Exception as e:
        ios_out = [{"error": str(e)}]

    android_out: list[dict] = []
    adb = shutil.which("adb") or os.path.expanduser(
        "~/Library/Android/sdk/platform-tools/adb"
    )
    try:
        out = subprocess.run(
            [adb, "devices", "-l"], capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines()[1:]:
            line = line.strip()
            if not line:
                continue
            parts = line.split()
            if len(parts) >= 2:
                android_out.append({
                    "serial": parts[0],
                    "state": parts[1],
                    "info": " ".join(parts[2:]),
                })
    except FileNotFoundError:
        android_out = [{"error": "adb not found on PATH"}]
    except Exception as e:
        android_out = [{"error": str(e)}]

    return {"ios_simulators": ios_out, "android_devices": android_out}


@mcp.tool()
async def install_apk(apk_path: str, devices: list[str]) -> dict:
    """并行装 APK 到多台 Android 设备，3 次重试。

    返回成功装上的设备列表 + 失败列表（全失败时 error 字段非空）。
    """
    if not Path(apk_path).exists():
        return {"error": f"apk not found: {apk_path}"}
    if not devices:
        return {"error": "devices list empty"}

    # 安装最长 300s×3 重试，丢线程池跑；_silenced_stdout 留在线程体内
    # （redirect_stdout 只换 sys.stdout 对象，stdio transport 持有原始
    # stdout 引用，JSON-RPC 不受影响）
    def _do_install() -> list[str]:
        with _silenced_stdout():
            return _install_apk_on_devices(apk_path, devices)

    try:
        alive = await asyncio.to_thread(_do_install)
    except RuntimeError as e:
        return {"error": str(e), "total": len(devices)}
    return {
        "installed": alive,
        "failed": [d for d in devices if d not in alive],
        "total": len(devices),
    }


@mcp.tool()
async def adb_reconnect(serials: list[str], timeout_s: int = 20) -> dict:
    """主动 ``adb connect`` 每个 serial + 轮询等设备上线，最多 timeout_s 秒。

    USB serial / mDNS 服务名不会被 adb connect（mDNS 名 adb host 不接受）。
    返回最终 online 的设备列表 — offline 的呼叫方应剔除。
    """
    if not serials:
        return {"error": "serials list empty"}

    # 最长 timeout_s 秒的 time.sleep 轮询，丢线程池跑避免冻结事件循环
    def _do_reconnect() -> list[str]:
        with _silenced_stdout():
            return _ensure_devices_connected(serials, timeout_s=timeout_s)

    try:
        alive = await asyncio.to_thread(_do_reconnect)
    except RuntimeError as e:
        return {"error": str(e), "total": len(serials)}
    return {
        "online": alive,
        "offline": [s for s in serials if s not in alive],
        "total": len(serials),
    }


@mcp.tool()
def setup_simulator(name: str | None = None,
                    device_type: str | None = None) -> dict:
    """创建并启动 iOS simulator。

    name 缺省读 SIMULATOR_DEVICE_NAME (.env, 默认 "Argus")；
    device_type 缺省读 SIMULATOR_DEVICE_TYPE (.env, 默认 "iPhone 16 Pro")。
    同名已存在时只 boot 不重建。
    """
    from ..config import load_config
    cfg = load_config()["simulator"]
    name = name or cfg["device_name"]
    device_type = device_type or cfg["device_type"]

    existing = None
    for d in _list_ios_devices():
        if d.name == name:
            existing = d
            break

    if existing:
        if existing.state == "Shutdown":
            boot(existing.udid)
        return {"name": name, "udid": existing.udid, "state": "Booted",
                "created": False}

    udid = create_device(name=name, device_type=device_type)
    boot(udid)
    return {"name": name, "udid": udid, "state": "Booted", "created": True}


# ──────────────────────────────────────────────────────────────────
# Device control primitives: external agents decide; shared service performs input.
# CLI and MCP reconnect the same persisted binding for every call. No per-server cache.
# ──────────────────────────────────────────────────────────────────

def _device(command, serial=None, **options):
    from argus.devices.service import execute
    with _silenced_stdout():
        return execute(command, serial, **options)


@mcp.tool()
def device_sessions() -> dict:
    """List persisted mobile, browser and desktop sessions shared with CLI."""
    from argus.devices.control import sessions
    return {"sessions": sessions()}


@mcp.tool()
def device_command(command: str, session: str, options: dict | None = None) -> dict:
    """Shared CLI commands: start, stop, pages, select-page, new-page, close-page,
    open, navigate, capabilities, wait. Use device_connect for a new binding.
    """
    return _device(command, session, **(options or {}))


@mcp.tool()
def device_connect(platform: str, session: str, options: dict | None = None) -> dict:
    """Bind Android/iOS, browser, or a desktop window to a persistent named session."""
    from argparse import Namespace
    from argus.devices.control import connect
    defaults = dict(device=None, server_url=None, team_id=None, app=None, backend=None,
                    bridge_directory=None, page_id=None)
    defaults.update(options or {})
    try:
        with _silenced_stdout():
            return connect(Namespace(platform=platform, session=session, **defaults))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def device_screenshot(serial: str | None = None, out_path: str | None = None) -> dict:
    """Capture a named session. Returns observation ID, target and coordinate mappings."""
    return _device("screenshot", serial, out=out_path)


def _image_result(result, observations):
    from mcp.types import TextContent, ImageContent
    import base64
    content = [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
    for observation in observations:
        path = observation.get("crop", {}).get("path", observation.get("path"))
        if path:
            content.append(ImageContent(type="image", data=base64.b64encode(Path(path).read_bytes()).decode(), mimeType="image/png"))
    return content


@mcp.tool()
def device_observe(session: str, crop: list[int] | None = None):
    """Return PNG image content plus observation metadata. Optional crop is image-pixel LTRB, magnified 2x."""
    result = _device("screenshot", session, crop=crop)
    return _image_result(result, [result] if result.get("ok") else [])


@mcp.tool()
def device_act(session: str, action: dict, observation_id: str | None = None,
               observe_after: bool = True, timeout: float = 5):
    """Dispatch one action, optionally wait for stability and return a new observation.
    coordinate_space: screen, percent, image, crop. Image/crop require observation_id.
    dispatched means input was sent; business_success remains unverified.
    """
    result = _device("act", session, action=action, observation_id=observation_id,
                     observe_after=observe_after, timeout=timeout)
    return _image_result(result, [result["observation"]] if result.get("observation") else [])


@mcp.tool()
def device_tap(x: int, y: int, serial: str | None = None) -> dict:
    """Click in screen coordinates on the saved session, regardless of platform."""
    return _device("tap", serial, x=x, y=y)


@mcp.tool()
def device_input(text: str, serial: str | None = None) -> dict:
    """Type into the focused field without submitting."""
    return _device("input", serial, text=text)


@mcp.tool()
def device_type_send(text: str, input_x: int, input_y: int, send_x: int, send_y: int,
                     wait_s: float = 12, serial: str | None = None, out_path: str | None = None) -> dict:
    """Focus, type, submit and capture. On uncertain results observe before retrying."""
    return _device("type-send", serial, text=text, input_x=input_x, input_y=input_y,
                   send_x=send_x, send_y=send_y, wait_s=wait_s, out=out_path)


@mcp.tool()
def device_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300,
                 serial: str | None = None) -> dict:
    """Swipe on a saved session; custom duration requires a supporting backend."""
    return _device("swipe", serial, x1=x1, y1=y1, x2=x2, y2=y2, duration_ms=duration_ms)


@mcp.tool()
def device_key(key: str, serial: str | None = None) -> dict:
    """Press a platform key; unsupported input returns an error."""
    return _device("key", serial, key=key)


@mcp.tool()
def device_launch(package: str, activity: str | None = None,
                  serial: str | None = None, force_stop: bool = False) -> dict:
    """Activate a mobile package on the saved Android or iOS session."""
    return _device("launch", serial, package=package, force_stop=force_stop)


@mcp.tool()
def device_handoff(session: str, instructions: str, reason: str = "login") -> dict:
    """Pause a session for human control. All automatic input is blocked until resume."""
    from argparse import Namespace
    from argus.devices.control import handoff
    try:
        return handoff(Namespace(session=session, instructions=instructions, reason=reason))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def device_resume(session: str, note: str) -> dict:
    """Return control after human work; capture a fresh observation, without claiming login success."""
    from argparse import Namespace
    from argus.devices.control import resume
    try:
        return resume(Namespace(session=session, note=note))
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


@mcp.tool()
def agent_task(command: str, task_id: str | None = None, options: dict | None = None):
    """Incremental durable task: create(bindings), observe(resource), submit(resource, action,
    observation_id, request_id, note), status, events, timeline, recover, resolve(outcome,note),
    handoff(instructions), resume(note), finish(note), cancel(note), export(out), list.
    Requests are never replayed automatically. needs_review requires evidence and explicit resolution.
    """
    from argus.runtime.interactive import call
    try:
        with _silenced_stdout():
            result = call(command, task_id, **(options or {}))
        if command in {"observe", "submit", "resume", "recover"}:
            observed = result.get("observations", {})
            resource = (options or {}).get("resource")
            images = [observed[resource]] if resource in observed else list(observed.values())
            return _image_result(result, images)
        return result
    except Exception as exc:
        return {"ok": False, "error": str(exc), "error_type": type(exc).__name__}


# ──────────────────────────────────────────────────────────────────
# Tool profile（给 Claude Code 插件用：device 档只暴露设备原语）
# ──────────────────────────────────────────────────────────────────

# device 档保留的 tool：设备驱动原语 + 设备管理。
# 意义：这一档**不需要 LLM_API_KEY、不需要 tests/ 目录、不需要 .env** 就能用，
# 挂给任意 agent 当"手和眼"（见 plugins/argus-device）。
_DEVICE_PROFILE_KEEP = {
    "device_sessions", "device_command", "device_connect", "device_observe", "device_act",
    "device_handoff", "device_resume", "agent_task",
    "device_screenshot", "device_tap", "device_swipe", "device_input",
    "device_type_send", "device_key", "device_launch",
    "list_devices", "install_apk", "adb_reconnect", "setup_simulator",
}

# full 档独有（= device 档要摘掉的跑测/报告类）。
# ⚠️ 新增这类 tool 时必须同步这里，否则 _apply_profile 的自检会在启动时直接报错。
_RUN_PROFILE_ONLY = {
    "list_targets", "list_cases", "list_runs", "get_run_status",
    "get_report", "run_target", "run_case", "cancel_run",
}


def _apply_profile(profile: str) -> None:
    """按 profile 裁剪暴露的 tool 集合。``full``（默认）= 全给，行为不变。"""
    profile = (profile or "full").strip().lower()
    if profile == "full":
        return
    if profile != "device":
        raise SystemExit(
            f"[argus.mcp] 未知 profile {profile!r}（可选 'device' / 'full'）")

    failed = []
    for name in sorted(_RUN_PROFILE_ONLY):
        try:
            mcp.remove_tool(name)
        except Exception as e:                       # mcp 版本不兼容 / 已改名
            failed.append(f"{name}({e})")
    if failed:
        raise SystemExit(
            "[argus.mcp] device 档裁剪失败，拒绝以 full 集合启动: " + ", ".join(failed))

    # 自检：确认剩下的都在白名单里（新加的跑测类 tool 忘了登记会在这里炸）
    try:
        left = {t.name for t in mcp._tool_manager.list_tools()}
    except Exception:                                # 私有 API 没了就跳过自检
        return
    stray = left - _DEVICE_PROFILE_KEEP
    if stray:
        raise SystemExit(
            "[argus.mcp] device 档出现未登记的 tool: " + ", ".join(sorted(stray))
            + " —— 请把它加进 _DEVICE_PROFILE_KEEP 或 _RUN_PROFILE_ONLY")


# ──────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────


def main() -> None:
    """Launch the MCP server over stdio.

    Claude Code / Desktop / Cursor 通过 ``command: python3 -m argus.mcp.server``
    + 工作目录指到 argus repo 即可挂载。

    ``--profile device`` / ``ARGUS_MCP_PROFILE=device`` 只暴露设备原语（Claude Code
    插件 argus-device 走这一档）；缺省 full = 全部 tool，与历史行为一致。

    stdout 守护：每个调到 cli helper（会 print）的 tool 在自己内部用
    ``_silenced_stdout`` 把 stdout 重定向到 stderr；这里不能全局换 sys.stdout
    因为 FastMCP 用 stdout 跑 JSON-RPC framing。
    """
    ap = argparse.ArgumentParser(prog="argus.mcp.server", add_help=True)
    ap.add_argument("--profile", choices=("device", "full"), default=None,
                    help="暴露的 tool 集合（默认取 ARGUS_MCP_PROFILE，再默认 full）")
    args = ap.parse_args()
    _apply_profile(args.profile or os.environ.get("ARGUS_MCP_PROFILE") or "full")
    mcp.run()


if __name__ == "__main__":
    main()
