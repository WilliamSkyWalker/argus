"""Launch background runs and inspect their process/report status."""

import getpass as _getpass
import json
import os
import re
import subprocess
import sys
import tempfile as _tempfile
from datetime import datetime
from pathlib import Path


_REPORT_SAVED_RE = re.compile(r"报告已保存[:：]\s*(\S+?\.html)")

RUNS_DIR = Path(_tempfile.gettempdir()) / f"argus_runs_{_getpass.getuser()}"


def register(sub):
    # argus status
    status_p = sub.add_parser("status", help="Check background run status")
    status_p.add_argument("run_id", nargs="?", default=None,
                          help="Specific run ID to inspect")
    status_p.set_defaults(handler=dispatch)


def dispatch(args):
    cmd_status(args.run_id)


def _launch_background(args, android_serial: str | None = None,
                       shard: str | None = None,
                       extra_devices: list[str] | None = None,
                       extra_env: dict | None = None) -> str:
    """Spawn a detached child process for the test run.

    Returns the ``run_id`` (timestamp dir name under ``RUNS_DIR``) so callers
    that don't parse stdout (e.g. the MCP server) can still locate the run.

    Args:
        shard: Optional "N/M" string; passed to child via ARGUS_SHARD env so
            cmd_run only runs that fraction of test_cases. Used for **manual**
            shard control — multi-device mode no longer auto-shards (it uses
            the in-process scheduler instead).
        extra_env: Extra env vars for the child only (e.g. PROBES_MODE from the
            MCP server, which must not mutate its own long-lived os.environ).
        extra_devices: Other device serials beyond android_serial. When set,
            the child process spawns the dispatched scheduler via
            ``--device s1 s2 ...`` and distributes cases across devices itself.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    # 同秒连续启动（如 MCP run_target/run_case back-to-back）会撞目录名 —
    # FileExistsError 时加 -1/-2/... 后缀重试
    n = 0
    while True:
        run_dir = RUNS_DIR / (f"{run_id}-{n}" if n else run_id)
        try:
            run_dir.mkdir()
            break
        except FileExistsError:
            n += 1
    if n:
        run_id = f"{run_id}-{n}"

    log_file = run_dir / "output.log"

    # Report 路径处理：
    # - 用户显式指定路径（如 --report /tmp/foo.html）→ 用该路径
    # - 用户传 --report 但没值（args.report == "__auto__"）→ 透传 "__auto__" 给子进程，
    #   由子进程 cmd_run 内统一算 tests/<first-level>/reports/ 路径，再回填 meta（确保
    #   父子流程使用同一份 auto 路径算法）
    # - 用户没传 --report → 默认写到 run_dir/report.html
    if args.report and args.report != "__auto__":
        report_file = args.report
    elif args.report == "__auto__":
        report_file = "__auto__"  # 子进程 cmd_run 会解析为实际路径
    else:
        report_file = str(run_dir / "report.html")

    # Re-build the command without --bg, add --report if not already set
    cmd = [sys.executable, "-m", "argus.cli", "run", args.test,
           "--report", report_file]
    if args.platform:
        cmd += ["--platform", args.platform]
    if args.url:
        cmd += ["--url", args.url]
    if args.max_steps:
        cmd += ["--max-steps", str(args.max_steps)]
    if args.grid:
        cmd += ["--grid", args.grid]
    # 多设备：把所有 serial 透传给 child，让 child 内部走调度器
    if extra_devices:
        all_devices = [android_serial] + list(extra_devices) if android_serial else list(extra_devices)
        cmd += ["--device"] + all_devices

    # Force headless for browser platform in background mode
    env = os.environ.copy()
    env["BROWSER_HEADLESS"] = "true"
    env["ARGUS_BG_RUN"] = "1"
    env["ARGUS_RUN_ID"] = run_id   # probe 上下文用它标记「本次跑测」
    env.update({k: str(v) for k, v in (extra_env or {}).items()})
    if android_serial:
        env["ANDROID_SERIAL"] = android_serial
    # 账号绑定不走 env：多设备由 dispatcher 按 worker_idx 直接取 accounts[i]，
    # 单设备用 accounts[0]。账号池 _accounts.json 仅承载“密钥 + 并发互斥资源”，
    # 普通测试数据请用 Gherkin Examples / Data Table（BDD 原生，无需代码）。
    # 分片：让 child cmd_run 只跑自己那 1/N 的 suite
    if shard:
        env["ARGUS_SHARD"] = shard

    with open(log_file, "w") as lf:
        proc = subprocess.Popen(
            cmd, stdout=lf, stderr=subprocess.STDOUT,
            env=env, start_new_session=True,
        )

    # 注：bg 模式 report=="__auto__" 时实际路径由子进程 cmd_run 决定，写在 reports_dir/
    # 的 -final.json 里；这里 meta 先记 "__auto__" 占位，cmd_status 时再 resolve。
    meta = {
        "run_id": run_id,
        "pid": proc.pid,
        "test": args.test,
        "platform": args.platform,
        "device": android_serial,
        "report": report_file,
        "log": str(log_file),
        "started_at": datetime.now().isoformat(),
    }
    (run_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    print(f"后台任务已启动")
    print(f"  Run ID:  {run_id}")
    print(f"  PID:     {proc.pid}")
    print(f"  日志:    {log_file}")
    print(f"  报告:    {report_file}")
    print(f"\n查看状态:  argus status {run_id}")
    return run_id


def cmd_status(run_id: str | None = None):
    """Show status of background runs."""
    if not RUNS_DIR.exists():
        print("没有后台运行记录。")
        return

    if run_id:
        _show_run_detail(run_id)
        return

    # List all runs
    runs = sorted(RUNS_DIR.iterdir(), reverse=True)
    if not runs:
        print("没有后台运行记录。")
        return

    print(f"{'Run ID':<20s} {'状态':<10s} {'测试':<40s}")
    print("-" * 72)
    for run_dir in runs:
        if not run_dir.is_dir():
            continue
        meta_file = run_dir / "meta.json"
        if not meta_file.exists():
            continue
        meta = json.loads(meta_file.read_text())
        status = _check_run_status(meta, run_dir)
        test_name = meta.get("test", "")[:38]
        print(f"  {meta['run_id']:<18s} {status:<10s} {test_name}")


def _show_run_detail(run_id: str):
    """Show detailed status of a specific run."""
    run_dir = RUNS_DIR / run_id
    meta_file = run_dir / "meta.json"
    if not meta_file.exists():
        print(f"找不到运行记录: {run_id}")
        return

    meta = json.loads(meta_file.read_text())
    status = _check_run_status(meta, run_dir)

    print(f"Run ID:    {meta['run_id']}")
    print(f"状态:      {status}")
    print(f"PID:       {meta['pid']}")
    print(f"测试:      {meta['test']}")
    print(f"启动时间:  {meta['started_at']}")
    print(f"日志:      {meta['log']}")
    print(f"报告:      {meta['report']}")

    # Show tail of log
    log_path = Path(meta["log"])
    if log_path.exists():
        lines = log_path.read_text().splitlines()
        tail = lines[-10:] if len(lines) > 10 else lines
        print(f"\n── 最近日志 ──")
        for line in tail:
            print(f"  {line}")

    # Show result summary if report exists
    report_path = meta.get("report", "")
    if report_path.endswith(".json") and Path(report_path).exists():
        data = json.loads(Path(report_path).read_text())
        s = data.get("summary", {})
        print(f"\n结果: {s.get('passed', 0)}/{s.get('total', 0)} 通过")


def _resolve_report_path(meta: dict, run_dir: Path) -> str:
    """Resolve a run's actual report path.

    bg 模式 `--report __auto__` 时实际路径由子进程 cmd_run 决定并打进
    output.log（"报告已保存: <path>"），meta.json 里只是 "__auto__" 占位。
    本函数从 log 解析真实路径并**回填 meta.json**（幂等），让 status / report
    工具能按 run_id 找到报告。非 auto（用户显式指定路径）时原样返回。
    """
    report = meta.get("report", "") or ""
    if report and report != "__auto__":
        return report

    log_path_str = meta.get("log", "")
    if not log_path_str or not Path(log_path_str).exists():
        return report
    try:
        text = Path(log_path_str).read_text(errors="replace")
    except Exception:
        return report
    matches = _REPORT_SAVED_RE.findall(text)
    if not matches:
        return report
    resolved = matches[-1]
    if not Path(resolved).exists():
        return report

    # 回填 meta.json，避免下次再扫 log
    meta["report"] = resolved
    try:
        (run_dir / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2))
    except Exception:
        pass
    return resolved


def _pid_running(pid: int | None) -> bool:
    """True if pid is a live, non-zombie process.

    僵尸进程（已退出但未被父进程 waitpid 回收）的 PID 仍在进程表里，
    `os.kill(pid, 0)` 不报错——直接判活会让已结束的 run 永远卡在"运行中"。
    故：先尝试回收自己的子进程（MCP server 是 detached run 的父进程），
    再用 `ps` 的 state 字段排除 zombie（CLI 调用时 run 不是其子进程）。
    """
    if not pid:
        return False
    try:
        wpid, _ = os.waitpid(pid, os.WNOHANG)
        if wpid == pid:
            return False  # 刚回收掉的僵尸 → 已结束
    except (ChildProcessError, OSError):
        pass  # 不是本进程的子进程（如 CLI status），交给后面的检查
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # PID 存在，但可能是未被回收的僵尸——查进程 state
    try:
        out = subprocess.run(
            ["ps", "-o", "state=", "-p", str(pid)],
            capture_output=True, text=True, timeout=3,
        )
        st = out.stdout.strip()
        if st and st[0] == "Z":
            return False
    except Exception:
        pass
    return True


def _check_run_status(meta: dict, run_dir: Path) -> str:
    """Check if the background process is still running."""
    if _pid_running(meta.get("pid")):
        return "运行中"

    # Process is gone — check if report was generated（resolve __auto__）
    report_path = _resolve_report_path(meta, run_dir)
    if report_path and report_path != "__auto__" and Path(report_path).exists():
        return "已完成"
    return "异常退出"
