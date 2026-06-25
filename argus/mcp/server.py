"""argus MCP server — stdio transport.

把 argus 的能力暴露成 MCP tools，让 Claude Code / Desktop / Cursor 这类
MCP-aware 客户端不用每次 spawn ``python3 -m argus.cli`` 就能：
  - 只读：list_targets / list_cases / list_runs / get_run_status / get_report
  - 跑测：run_target / run_case / cancel_run
  - 设备：list_devices / install_apk / adb_reconnect / setup_simulator

启动:
    python3 -m argus.mcp.server

stdio 注意事项:
- FastMCP 用 stdout 跑 JSON-RPC，任何 print() 都会污染 protocol stream。
- argus.logger 已经走 stderr 没问题；cli.py 里面 _launch_background /
  _install_apk_on_devices / _ensure_devices_connected 有 print，本模块通过
  ``_silenced_stdout`` 把它们的 stdout 重定向到 stderr（客户端 debug log 可见）。
"""

from __future__ import annotations

import argparse
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

from mcp.server.fastmcp import FastMCP

from ..cli import (
    RUNS_DIR,
    TESTS_DIR,
    _check_run_status,
    _ensure_devices_connected,
    _install_apk_on_devices,
    _launch_background,
    _resolve_report_path,
    _resolve_test_target,
)
from ..gherkin import parse_feature_file
from ..simulator import boot, create_device, list_devices as _list_ios_devices

mcp = FastMCP("argus")


@contextlib.contextmanager
def _silenced_stdout():
    """Redirect stdout → stderr for the duration of the block.

    Used to wrap helpers in cli.py that print human-readable progress —
    we keep the messages visible in MCP client debug logs (which surface
    stderr) without corrupting the stdio JSON-RPC channel.
    """
    with contextlib.redirect_stdout(sys.stderr):
        yield


# ──────────────────────────────────────────────────────────────────
# Read-only tools
# ──────────────────────────────────────────────────────────────────


@mcp.tool()
def list_targets() -> list[dict]:
    """列 tests/ 下所有可用 target。

    每个 target 返回:
      - name: 目录名（如 "nb_cases" / "eve-kit"）
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
      - target 名: "nb_cases" / "eve-kit"
      - 子目录: "nb_cases/nb_mobile/02-feed"
      - 单文件: 任意 .feature 路径（绝对 / 相对 cwd / TESTS_DIR 相对）

    返回每个 scenario 的 tc_id / feature / file / priority / automation /
    platform / tags（来自 @tag 解析）。.md 旧格式不展开 scenario，会被忽略。

    分页：默认每页 limit=100 条 scenario（整个 nb_cases 上千 scenario 会撑爆
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
        try:
            meta = json.loads(meta_file.read_text())
        except Exception:
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
    meta = json.loads(meta_file.read_text())
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
        meta = json.loads(meta_file.read_text())
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
                     shard: str | None = None) -> dict:
    devices = list(devices or [])
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
def run_target(target: str,
               platform: str | None = None,
               devices: list[str] | None = None,
               apk: str | None = None,
               url: str | None = None,
               max_steps: int | None = None,
               grid: str | None = None,
               shard: str | None = None) -> dict:
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
    """
    return _run_target_impl(
        target=target, platform=platform, devices=devices, apk=apk,
        url=url, max_steps=max_steps, grid=grid, shard=shard,
    )


@mcp.tool()
def run_case(case_path: str,
             platform: str | None = None,
             devices: list[str] | None = None,
             apk: str | None = None,
             url: str | None = None,
             max_steps: int | None = None,
             grid: str | None = None) -> dict:
    """run_target 的语义化别名 — 跑单 .feature / 单 case / inline 文本。

    与 run_target 等价（实现完全一致），方便客户端意图清晰区分批量 vs 单点。
    """
    return _run_target_impl(
        target=case_path, platform=platform, devices=devices, apk=apk,
        url=url, max_steps=max_steps, grid=grid,
    )


@mcp.tool()
def cancel_run(run_id: str) -> dict:
    """终止后台 run。

    先给进程组 SIGTERM 留 2s graceful，仍存活则 SIGKILL。
    """
    run_dir = RUNS_DIR / run_id
    meta_file = run_dir / "meta.json"
    if not meta_file.exists():
        return {"run_id": run_id, "error": "run not found"}
    meta = json.loads(meta_file.read_text())
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
def install_apk(apk_path: str, devices: list[str]) -> dict:
    """并行装 APK 到多台 Android 设备，3 次重试。

    返回成功装上的设备列表 + 失败列表（全失败时 error 字段非空）。
    """
    if not Path(apk_path).exists():
        return {"error": f"apk not found: {apk_path}"}
    if not devices:
        return {"error": "devices list empty"}
    try:
        with _silenced_stdout():
            alive = _install_apk_on_devices(apk_path, devices)
    except RuntimeError as e:
        return {"error": str(e), "total": len(devices)}
    return {
        "installed": alive,
        "failed": [d for d in devices if d not in alive],
        "total": len(devices),
    }


@mcp.tool()
def adb_reconnect(serials: list[str], timeout_s: int = 20) -> dict:
    """主动 ``adb connect`` 每个 serial + 轮询等设备上线，最多 timeout_s 秒。

    USB serial / mDNS 服务名不会被 adb connect（mDNS 名 adb host 不接受）。
    返回最终 online 的设备列表 — offline 的呼叫方应剔除。
    """
    if not serials:
        return {"error": "serials list empty"}
    try:
        with _silenced_stdout():
            alive = _ensure_devices_connected(serials, timeout_s=timeout_s)
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
# Entry point
# ──────────────────────────────────────────────────────────────────


def main() -> None:
    """Launch the MCP server over stdio.

    Claude Code / Desktop / Cursor 通过 ``command: python3 -m argus.mcp.server``
    + 工作目录指到 argus repo 即可挂载。

    stdout 守护：每个调到 cli helper（会 print）的 tool 在自己内部用
    ``_silenced_stdout`` 把 stdout 重定向到 stderr；这里不能全局换 sys.stdout
    因为 FastMCP 用 stdout 跑 JSON-RPC framing。
    """
    mcp.run()


if __name__ == "__main__":
    main()
