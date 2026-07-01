"""Command-line interface."""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from .agent import Agent
from .config import init_config, load_config, PROJECT_ROOT
from .logger import get_logger, set_level
from .simulator import boot, create_device, list_devices

log = get_logger("cli")

# Matches "open_url ... https://..." or "用 open_url 打开 https://..."
_OPEN_URL_RE = re.compile(r"open_url[^\n]*?(https?://\S+)", re.IGNORECASE)

# Matches `**Reset before**: pm_clear` / `relaunch` / `none` in a case body.
# Lets test authors declare per-case Android state-reset requirements without
# having to duplicate `adb shell pm clear` in every When step.
_RESET_RE = re.compile(
    r"\*\*Reset before\*\*:\s*(pm_clear|relaunch|none)",
    re.IGNORECASE,
)

# Matches `**Platform**: ios|android|both` in a case body. Cases tagged
# @ios/@android in .feature files render with `**Platform**: ios|android`.
# Used to skip iOS-only cases when running on Android (or vice versa).
_PLATFORM_RE = re.compile(
    r"\*\*Platform\*\*:\s*(ios|android|both)",
    re.IGNORECASE,
)

def _require_android_package() -> str:
    """被测 Android 包名，来自配置 ANDROID_PACKAGE（`.env` 文件或同名环境变量，
    env var 覆盖 .env）。

    **刻意不提供默认值**：取不到直接报错。曾因写死默认包名导致整轮跑测静默
    打到错误的 App（把生产 OTP 输进 test 包全挂）。宁可报错也不静默兜底。
    """
    pkg = (load_config()["android"].get("package") or "").strip()
    if not pkg:
        raise RuntimeError(
            "ANDROID_PACKAGE 未配置：请在 .env 里写 `ANDROID_PACKAGE=<你的包名>`，"
            "或跑测时 `ANDROID_PACKAGE=com.your.app python3 -m argus.cli run …` 覆盖。"
            "（不设默认值，防止静默测错 App。）"
        )
    return pkg


def _extract_target_url(case_text: str) -> str | None:
    """Pull a target URL out of a scenario's text if it mentions open_url."""
    m = _OPEN_URL_RE.search(case_text)
    if not m:
        return None
    return m.group(1).strip().strip('"\'').rstrip(".,;)")


def _extract_reset_mode(case_text: str) -> str | None:
    """Return the value of `**Reset before**: <mode>` if present, else None."""
    m = _RESET_RE.search(case_text)
    return m.group(1).lower() if m else None


def _extract_platform(case_text: str) -> str | None:
    """Return the value of `**Platform**: <ios|android|both>` if present."""
    m = _PLATFORM_RE.search(case_text)
    return m.group(1).lower() if m else None


def _should_skip_by_platform(case_text: str, current_platform: str) -> str | None:
    """Return a skip reason if the case's @ios/@android tag doesn't match the
    current run platform. Returns None when the case can run.

    `**Platform**: both` (default) and `**Platform**: <current>` → run.
    `**Platform**: ios` on Android run → skip with reason.
    `**Platform**: android` on iOS run → skip with reason.
    Case without **Platform** field → run (no constraint).
    """
    case_platform = _extract_platform(case_text)
    if case_platform is None or case_platform == "both":
        return None
    cur = (current_platform or "").lower()
    if case_platform == cur:
        return None
    return f"platform mismatch: case requires {case_platform}, run is {cur}"


def _should_skip_by_automation(case_text: str) -> str | None:
    """Skip cases tagged @manual / @partial. These cases require real OAuth /
    real network / real device behavior that automation can't reliably check.
    Returns a skip reason or None.

    Detection: matches `**Automation**: manual` / `**Automation**: partial`
    field that gherkin.py emits from @manual / @partial tags.
    """
    m = re.search(r"^\s*-\s*\*\*Automation\*\*:\s*(\w+)", case_text, re.M)
    if not m:
        return None
    automation = m.group(1).lower()
    if automation in ("manual", "partial"):
        return f"automation mode '{automation}' — requires human review"
    return None


def _substitute_placeholders(case_text: str) -> str:
    """Substitute runtime placeholders in a case body.

    Currently supported:
      {epoch}  → Unix timestamp at run start (10 digits)
      {uuid}   → random 8-char hex (for unique-email cases)
    """
    import uuid as _uuid
    return (case_text
            .replace("{epoch}", str(int(time.time())))
            .replace("{uuid}", _uuid.uuid4().hex[:8]))


def _reset_android_state(platform, mode: str, package: str | None = None) -> None:
    """Reset Android app state per `**Reset before**: <mode>` directive.

    mode:
      pm_clear  — `pm clear PACKAGE` + relaunch (clears login, cache, prefs —
                  useful for fresh-install / Onboarding cases).
      relaunch  — `am force-stop` + relaunch (keeps data, returns to splash —
                  useful for cold-start / no-effect-on-data cases).

    Launcher is resolved via `cmd package resolve-activity` so this isn't
    hard-coded to a specific main activity. Falls back to `monkey` if resolve
    fails (note: monkey exits 251 on simulators even on success, so we
    tolerate non-zero by going through subprocess directly).

    No-op for non-Android platforms or unrecognized mode.
    """
    if not hasattr(platform, "_adb"):
        return
    # 包名取不到即报错（在 try 之外，确保不被下方 except 吞成 warning）
    package = package or _require_android_package()
    try:
        # 先发 HOME 收回所有 system overlay（通知栏 / 快速设置 / 多任务视图等）。
        # force-stop 只杀 App，不动 SystemUI 渲染的 overlay；不先收 overlay 会导致
        # 下个 case 启动后画面仍被通知栏遮住，agent 看到的不是 App。
        platform._adb("shell", "input", "keyevent", "KEYCODE_HOME")
        time.sleep(0.3)

        if mode == "pm_clear":
            platform._adb("shell", "pm", "clear", package)
            time.sleep(1)
            _android_launch(platform, package)
            log.info("Android 状态重置: pm clear + 重启 %s", package)
            time.sleep(6)
        elif mode == "relaunch":
            platform._adb("shell", "am", "force-stop", package)
            time.sleep(0.5)
            _android_launch(platform, package)
            log.info("Android 状态重置: 重启 %s", package)
            time.sleep(5)
    except Exception as e:
        log.warning("Android 状态重置失败 (%s): %s", mode, e)


def _android_launch(platform, package: str) -> None:
    """Launch an Android app by package. Resolves the launcher activity from
    the package manager so it's not hard-coded per app."""
    # Resolve launcher activity via `cmd package resolve-activity`
    try:
        out = platform._adb(
            "shell", "cmd", "package", "resolve-activity",
            "--brief", "-c", "android.intent.category.LAUNCHER", package,
        )
        # Last non-empty line is the component name (e.g. "com.x/com.x.MainActivity")
        component = ""
        for line in reversed(out.strip().splitlines()):
            line = line.strip()
            if "/" in line:
                component = line
                break
        if component:
            platform._adb("shell", "am", "start", "-n", component)
            return
    except Exception as e:
        log.debug("resolve-activity failed for %s: %s", package, e)
    # Fallback: monkey via raw subprocess (monkey exits 251 on success on emulator)
    serial = getattr(platform, "_serial", None)
    cmd = ["adb"]
    if serial:
        cmd += ["-s", serial]
    cmd += ["shell", "monkey", "-p", package,
            "-c", "android.intent.category.LAUNCHER", "1"]
    subprocess.run(cmd, capture_output=True, timeout=10)

# 后台跑的临时数据（PID / meta / log）放系统 /tmp 下，不污染项目目录。
# 用 getpass 让多用户共享同一机器时各自独立；目录在用户登出后由 OS 清理。
import getpass as _getpass
import tempfile as _tempfile
RUNS_DIR = Path(_tempfile.gettempdir()) / f"argus_runs_{_getpass.getuser()}"
TESTS_DIR = PROJECT_ROOT / "tests"


def main():
    parser = argparse.ArgumentParser(description="Argus — LLM QA Agent")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Enable debug logging")
    sub = parser.add_subparsers(dest="command")

    # argus init
    sub.add_parser("init", help="Create default .env config file")

    # argus new <target>
    new_p = sub.add_parser("new", help="Scaffold a new test target under tests/")
    new_p.add_argument("name", help="Target name (becomes tests/<name>/)")
    new_p.add_argument("--platform", choices=["ios", "android", "browser"],
                       required=True, help="Target platform")
    new_p.add_argument("--package", default=None, metavar="PKG",
                       help="Android package name (e.g. com.x.y); android only")
    new_p.add_argument("--url", default=None, metavar="URL",
                       help="Site URL for browser targets (written into README)")
    new_p.add_argument("--force", action="store_true",
                       help="Overwrite if tests/<name>/ already exists")

    # argus devices
    sub.add_parser("devices", help="List available simulators")

    # argus setup
    setup_p = sub.add_parser("setup", help="Create and boot a simulator")
    setup_p.add_argument("--name", default=None)
    setup_p.add_argument("--type", default=None, dest="device_type")

    # argus run <test case file or inline text>
    run_p = sub.add_parser("run", help="Run test case(s)")
    run_p.add_argument("test", help="Test case text or path to .md/.txt file")
    run_p.add_argument("--platform", choices=["ios", "android", "browser"], default=None,
                       help="Platform to test on (default: from config)")
    run_p.add_argument("--url", default=None,
                       help="Open this URL before running (browser platform)")
    run_p.add_argument("--max-steps", type=int, default=None,
                       help="Override max steps for the test")
    run_p.add_argument("--report", nargs="?", const="__auto__", default=None,
                       metavar="PATH",
                       help="Save report to file (.json or .html). "
                            "Without value: auto-save to tests/<target>/reports/")
    run_p.add_argument("--grid", default=None, metavar="URL",
                       help="Selenium Grid URL (e.g. http://localhost:4444)")
    run_p.add_argument("-j", "--concurrency", type=int, default=1, metavar="N",
                       help="Concurrent sessions (requires Selenium Grid, default: 1)")
    run_p.add_argument("--device", nargs="+", metavar="SERIAL",
                       help="Android device serial(s). Multiple serials run a DYNAMIC "
                            "scheduler: cases enter one shared queue and each device pulls "
                            "the next when idle (slow device on hard case ≠ block others). "
                            "Account from _accounts.json[i] auto-bound to device i.")
    run_p.add_argument("--apk", default=None, metavar="PATH",
                       help="Install this APK on all --device targets before running")
    run_p.add_argument("--shard", default=None, metavar="N/M",
                       help="Manually run only shard N of M of cases (0-based). E.g. "
                            "--shard 0/3 跑前 1/3。与 --device 多设备调度无关，是单进程内"
                            "对 cases 列表的硬切片，便于手动拆批跑。")
    run_p.add_argument("--bg", action="store_true",
                       help="Run in background (headless, silent)")

    # argus list
    sub.add_parser("list", help="List available test targets")

    # argus status
    status_p = sub.add_parser("status", help="Check background run status")
    status_p.add_argument("run_id", nargs="?", default=None,
                          help="Specific run ID to inspect")

    # argus figma <subcommand>
    figma_p = sub.add_parser("figma", help="Figma design integration")
    figma_sub = figma_p.add_subparsers(dest="figma_command")

    # argus figma frames <url>
    frames_p = figma_sub.add_parser("frames", help="List frames in a Figma file")
    frames_p.add_argument("url", help="Figma file URL or file key")
    frames_p.add_argument("--page", default=None, help="Filter by page name")

    # argus figma gen-tests <url>
    gen_p = figma_sub.add_parser("gen-tests", help="Generate test cases from Figma design")
    gen_p.add_argument("url", help="Figma URL (with optional node-id)")
    gen_p.add_argument("-o", "--output", default=None,
                       help="Save generated tests to file (.md/.yaml)")

    # argus figma review <url>
    review_p = figma_sub.add_parser("review", help="Visual review: Figma vs actual screenshot")
    review_p.add_argument("url", help="Figma URL (with node-id for specific frame)")
    review_p.add_argument("--platform", choices=["ios", "android", "browser"], default=None)
    review_p.add_argument("--screenshot", default=None,
                          help="Path to screenshot PNG (instead of live capture)")
    review_p.add_argument("-o", "--output", default=None,
                          help="Save review report to file (.json/.html)")

    args = parser.parse_args()

    if getattr(args, "verbose", False):
        set_level("DEBUG")

    if args.command == "init":
        init_config()
    elif args.command == "new":
        cmd_new(args.name, args.platform, package=args.package, url=args.url,
                force=args.force)
    elif args.command == "devices":
        cmd_devices()
    elif args.command == "setup":
        cfg = load_config()["simulator"]
        name = args.name or cfg["device_name"]
        device_type = args.device_type or cfg["device_type"]
        cmd_setup(name, device_type)
    elif args.command == "list":
        cmd_list_targets()
    elif args.command == "run":
        devices = args.device or []
        # 先确保设备 online。TLS/WiFi adb 是会自动断的，每次跑前主动
        # connect 一遍 + 等待最多 20s，能省去手动 adb reconnect 的麻烦。
        if devices:
            devices = _ensure_devices_connected(devices)
        apk = getattr(args, "apk", None)
        if apk and devices:
            # 装不上的设备直接剔除，幸存的继续 — 跟后续 Agent 启动失败的容错思路一致
            devices = _install_apk_on_devices(apk, devices)
        # 手动 --shard 透传给 cmd_run 通过 env
        if args.shard:
            os.environ["ARGUS_SHARD"] = args.shard
        if args.bg:
            # 后台模式：单 daemon process。多设备时 daemon 内部跑调度器
            # （前台 cmd_run(devices=...)），不再 spawn N 个 child。
            primary_serial = devices[0] if devices else None
            _launch_background(args, android_serial=primary_serial,
                               extra_devices=devices[1:] if len(devices) > 1 else None)
        else:
            # 前台：cmd_run 直接收 devices，>1 时走调度器
            if devices and len(devices) == 1:
                os.environ["ANDROID_SERIAL"] = devices[0]
            cmd_run(args.test, platform=args.platform, url=args.url,
                    max_steps=args.max_steps, report_path=args.report,
                    grid_url=args.grid, concurrency=args.concurrency,
                    devices=devices if len(devices) > 1 else None)
    elif args.command == "status":
        cmd_status(args.run_id)
    elif args.command == "figma":
        cmd_figma(args)
    else:
        parser.print_help()


# ── Background execution ──────────────────────────────────────


def _ensure_devices_connected(serials: list[str], timeout_s: int = 20) -> list[str]:
    """Try to bring every target device to ``device`` state before tests run.

    TLS / TCP adb 连接（host:port 或 mDNS ``adb-XXX._adb-tls-connect._tcp``）
    会因 host 睡眠、网络切换、设备息屏等原因变成 offline/missing。每次跑
    测前主动 ``adb connect <serial>`` 一遍能救回大部分情况。

    Returns the subset of ``serials`` that are confirmed online after the
    wait. Offline / unreachable devices are dropped — caller skips them.
    """
    import shutil
    import time as _t

    adb = shutil.which("adb") or os.path.expanduser(
        "~/Library/Android/sdk/platform-tools/adb"
    )

    def _adb_devices_online() -> set[str]:
        try:
            out = subprocess.run(
                [adb, "devices"], capture_output=True, text=True, timeout=5,
            ).stdout
        except Exception:
            return set()
        online = set()
        for line in out.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                online.add(parts[0])
        return online

    # 1) 主动 connect 各类 serial。三种处理：
    #    a) host:port 格式（如 192.0.2.10:5555）→ adb connect 重连
    #    b) mDNS 服务名（含 `_adb-tls-` / `_adb._tcp` 等）→ **不能** adb connect，
    #       adb host 不接受 mDNS 名做 hostname。这类设备只能等设备自己重新
    #       广播 mDNS（开屏 + WiFi adb 开启），我们能做的只是等。
    #    c) 纯 USB serial → 不用 connect，kernel 管。
    print(f"检查 {len(serials)} 台设备连接...")
    for s in serials:
        if "_adb-tls-" in s or "._tcp" in s:
            # mDNS 服务名，adb 不支持手动 connect
            print(f"  {s}: mDNS 名，等设备自动广播")
            continue
        if ":" not in s:
            # USB serial
            continue
        try:
            res = subprocess.run(
                [adb, "connect", s], capture_output=True, text=True, timeout=8,
            )
            tag = (res.stdout + res.stderr).strip().splitlines()[-1] if res.stdout or res.stderr else ""
            print(f"  adb connect {s} → {tag}")
        except Exception as e:
            print(f"  adb connect {s} 异常: {e}")

    # 2) 轮询等待，直到全部上线或超时
    deadline = _t.time() + timeout_s
    while _t.time() < deadline:
        online = _adb_devices_online()
        if all(s in online for s in serials):
            print(f"✓ {len(serials)} 台设备全部 online")
            return list(serials)
        _t.sleep(1)

    # 3) 超时仍未全部上线 — 报告状态，剔除离线的
    online = _adb_devices_online()
    alive = [s for s in serials if s in online]
    failed = [s for s in serials if s not in online]
    if failed:
        print(f"⚠️  {len(failed)}/{len(serials)} 台设备 {timeout_s}s 内未上线，"
              f"跳过这些设备继续：{failed}")
    if not alive:
        raise RuntimeError(
            f"全部 {len(serials)} 台设备都连不上，无法跑测。"
            f"请检查 adb 状态 + 网络/USB 连接后重试。"
        )
    return alive


def _install_apk_on_devices(apk_path: str, serials: list[str]) -> list[str]:
    """Install APK in parallel on every device.

    Returns the list of devices where install **succeeded** — caller should
    skip the failures (their device state is mismatched / stale and not safe
    to run tests against).

    Timeout 300s per device because TLS-over-WiFi adb install of a 100+ MB
    APK on Pixel-class real devices commonly takes 60-150s; original 120s
    was hitting timeouts mid-stream.
    """
    import shutil
    from concurrent.futures import ThreadPoolExecutor, as_completed

    adb = shutil.which("adb") or os.path.expanduser(
        "~/Library/Android/sdk/platform-tools/adb"
    )

    INSTALL_MAX_ATTEMPTS = 3  # 1 次主 + 2 次重试

    def _try_install_once(serial: str) -> tuple[bool, str]:
        try:
            result = subprocess.run(
                # -g：安装时授予全部运行时权限（含 POST_NOTIFICATIONS）→ 默认允许推送、
                # 消除首启权限弹窗干扰（测试环境默认放行）。
                [adb, "-s", serial, "install", "-r", "-g", apk_path],
                capture_output=True, text=True, timeout=300,
            )
            ok = result.returncode == 0 and "Success" in result.stdout
            msg = result.stdout.strip() or result.stderr.strip()
            return ok, msg
        except subprocess.TimeoutExpired:
            return False, "install timed out after 300s"
        except Exception as e:
            return False, str(e)

    def install_one(serial: str) -> tuple[str, bool, str]:
        """Install APK on one device with retries.

        Network/TLS adb 路径的 install 偶发 mid-stream 断流（看到的失败信号
        是 stdout 只 echo 出 "Performing Streamed Install" 没 Success）。
        重试通常能救回来 — 一次失败就剔设备代价太大（少一台 worker）。
        """
        last_msg = ""
        for attempt in range(1, INSTALL_MAX_ATTEMPTS + 1):
            ok, msg = _try_install_once(serial)
            if ok:
                if attempt > 1:
                    msg = f"{msg} (第 {attempt} 次尝试成功)"
                return serial, True, msg
            last_msg = msg
            if attempt < INSTALL_MAX_ATTEMPTS:
                print(f"  [↻] {serial}: 第 {attempt} 次失败，重试中... ({msg})")
                time.sleep(2)
        return serial, False, f"{INSTALL_MAX_ATTEMPTS} 次尝试均失败: {last_msg}"

    print(f"正在安装 {apk_path} 到 {len(serials)} 台设备...")
    alive: list[str] = []
    failed: list[tuple[str, str]] = []
    with ThreadPoolExecutor(max_workers=len(serials)) as pool:
        futures = {pool.submit(install_one, s): s for s in serials}
        for f in as_completed(futures):
            serial, ok, msg = f.result()
            status = "✓" if ok else "✗"
            print(f"  [{status}] {serial}: {msg}")
            if ok:
                alive.append(serial)
            else:
                failed.append((serial, msg))

    if not alive:
        raise RuntimeError(
            f"全部 {len(serials)} 台设备 APK 安装均失败: {failed}"
        )
    if failed:
        print(f"⚠️  {len(failed)}/{len(serials)} 台 APK 安装失败，仅用剩余 "
              f"{len(alive)} 台继续测试 — 失败列表: {[s for s, _ in failed]}")
    print("安装完成。\n")
    return alive


def _launch_background(args, android_serial: str | None = None,
                       shard: str | None = None,
                       extra_devices: list[str] | None = None) -> str:
    """Spawn a detached child process for the test run.

    Returns the ``run_id`` (timestamp dir name under ``RUNS_DIR``) so callers
    that don't parse stdout (e.g. the MCP server) can still locate the run.

    Args:
        shard: Optional "N/M" string; passed to child via ARGUS_SHARD env so
            cmd_run only runs that fraction of test_cases. Used for **manual**
            shard control — multi-device mode no longer auto-shards (it uses
            the in-process scheduler instead).
        extra_devices: Other device serials beyond android_serial. When set,
            the child process spawns the dispatched scheduler via
            ``--device s1 s2 ...`` and distributes cases across devices itself.
    """
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = RUNS_DIR / run_id
    run_dir.mkdir()

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


_REPORT_SAVED_RE = re.compile(r"报告已保存[:：]\s*(\S+?\.html)")


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


# ── Foreground execution ──────────────────────────────────────


def cmd_new(name: str, platform: str, package: str | None = None,
            url: str | None = None, force: bool = False):
    """Scaffold tests/<name>/ from tests/_template/, filling the .feature
    metadata header for the chosen platform."""
    template_dir = TESTS_DIR / "_template"
    target = TESTS_DIR / name
    if not template_dir.exists():
        print(f"模板缺失：{template_dir} 不存在，无法新建。")
        return
    if target.exists():
        if not force:
            print(f"目标已存在：{target}（要覆盖请加 --force）")
            return
        shutil.rmtree(target)
    if platform == "android" and not package:
        print("⚠️ android target 没给 --package，先填占位 com.example.app；"
              "记得改 .feature 头并在跑测时配 ANDROID_PACKAGE。")

    shutil.copytree(template_dir, target)
    # 旧 .md 样例不应再带入新 target（统一 .feature）
    for stale in (target / "cases").glob("*.md"):
        stale.unlink()

    # 定制 example.feature 的元数据头
    feat = target / "cases" / "example.feature"
    if feat.exists():
        out = []
        for ln in feat.read_text(encoding="utf-8").splitlines():
            s = ln.strip()
            if s.startswith("# argus-target:"):
                out.append(f"# argus-target: {name}")
            elif s.startswith("# argus-platform:"):
                out.append(f"# argus-platform: {platform}")
            elif s.startswith("# argus-package:"):
                if platform == "android":
                    out.append(f"# argus-package: {package or 'com.example.app'}")
                # 非 android 丢掉 package 行
            elif s.startswith("# argus-reset-default:"):
                if platform == "android":
                    out.append(ln)
                # reset-default 仅 android 有意义，非 android 丢掉
            elif s.startswith("@") and "@android" in s:
                # scenario 平台 tag：android 保留；ios 换成 @ios；browser 去掉
                # （平台 tag 只有 @ios/@android/@both，browser case 不带平台 tag）
                if platform == "ios":
                    out.append(ln.replace("@android", "@ios"))
                elif platform == "browser":
                    out.append(re.sub(r"\s*@android\b", "", ln))
                else:
                    out.append(ln)
            else:
                out.append(ln)
        feat.write_text("\n".join(out) + "\n", encoding="utf-8")

    # 轻量定制 README
    readme = target / "README.md"
    if readme.exists():
        txt = readme.read_text(encoding="utf-8").replace("# [项目名称]", f"# {name}", 1)
        if url:
            txt = txt.replace("https://example.com", url)
        if platform == "android" and package:
            txt = txt.replace("com.example.app", package)
        readme.write_text(txt, encoding="utf-8")

    print(f"✓ 已创建 target: {target.relative_to(PROJECT_ROOT)}  (platform={platform}"
          + (f", package={package}" if platform == "android" and package else "") + ")")
    for p in sorted(target.rglob("*")):
        if p.is_file():
            print(f"    {p.relative_to(TESTS_DIR)}")
    print("\n下一步：")
    print(f"  1. 改 tests/{name}/cases/example.feature 写真实用例（自包含；值行别写行内 # 注释）")
    print(f"  2. 改 tests/{name}/_preconditions.md 为你产品真实的状态恢复 + 首页→子页导航")
    print(f"  3. 多账号并发：cp tests/{name}/_accounts.json.example tests/{name}/_accounts.json 填真账号（别 commit）")
    print(f"  跑：argus run {name}")


def cmd_list_targets():
    """List available test targets under tests/."""
    if not TESTS_DIR.exists():
        print("没有测试目录。请创建 tests/ 目录。")
        return

    targets = sorted(
        d for d in TESTS_DIR.iterdir()
        if d.is_dir() and not d.name.startswith("_") and (d / "cases").is_dir()
    )
    if not targets:
        print("没有可用的测试目标。")
        return

    print(f"{'目标':<20s} {'用例数':<8s} {'报告数':<8s} 说明")
    print("-" * 65)
    for t in targets:
        cases = list((t / "cases").rglob("*.feature")) + list((t / "cases").rglob("*.md"))
        reports_dir = t / "reports"
        reports = list(reports_dir.glob("*.html")) if reports_dir.exists() else []
        # Read first line of README for description
        readme = t / "README.md"
        desc = ""
        if readme.exists():
            for line in readme.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and not line.startswith("["):
                    desc = line[:30]
                    break
        print(f"  {t.name:<18s} {len(cases):<8d} {len(reports):<8d} {desc}")


def cmd_devices():
    devices = list_devices()
    if not devices:
        print("No simulators found. Run: argus setup")
        return
    for d in devices:
        print(f"  {d.name:25s} {d.state:10s} {d.udid}")


def cmd_setup(name: str, device_type: str):
    for d in list_devices():
        if d.name == name:
            print(f"Device '{name}' already exists (udid={d.udid})")
            if d.state == "Shutdown":
                print("Booting...")
                boot(d.udid)
                print("Done. Simulator is running.")
            return

    print(f"Creating '{name}' ({device_type})...")
    udid = create_device(name=name, device_type=device_type)
    print(f"Created: {udid}")
    print("Booting...")
    boot(udid)
    print("Done. Simulator is running.")


def _read_target_url(target_dir: Path) -> str | None:
    """Read the URL field from a target's README.md.

    Looks for patterns like:
      - **URL**: https://example.com
      - URL: https://example.com
      - [example.com](https://example.com)
    """
    import re
    readme = target_dir / "README.md"
    if not readme.exists():
        return None
    for line in readme.read_text().splitlines():
        # - **URL**: https://...
        m = re.search(r'\*\*URL\*\*\s*[:：]\s*(https?://\S+)', line)
        if m:
            return m.group(1).rstrip(")")
        # URL: https://...
        m = re.search(r'^[-\s]*URL\s*[:：]\s*(https?://\S+)', line, re.IGNORECASE)
        if m:
            return m.group(1).rstrip(")")
        # [text](https://...)  on first link-only line
        m = re.match(r'^\[.*?\]\((https?://\S+)\)', line.strip())
        if m:
            return m.group(1).rstrip(")")
    return None


def _load_accounts(target_dir: Path | None) -> list[dict]:
    """加载 tests 下顶级目录里的 `_accounts.json` 账号池（如有）。

    ⚠️ 定位：这个池**只承载两类东西** ——
      1. **密钥/凭据**（账号、密码）：不能写进 .feature（会进 git 泄敏），
         所以放 gitignored 的 _accounts.json，case 里用 `${EMAIL}` 等占位符引用。
      2. **并发互斥的可互换资源**：同一 case 同时在 N 台设备跑须各用不同账号。
    **普通测试数据（输入变体、不同入参→不同结果）请用 Gherkin `Examples` 表 /
    Data Table**（BDD 原生、gherkin.py 已支持、零代码），不要往这个池塞。

    多设备并发时按 --device 顺序分配（设备 1 → accounts[0]，设备 2 → accounts[1]，
    ...）由 dispatcher 按 worker_idx 直接绑定；单设备用 accounts[0]。不再有
    ARGUS_ACCOUNT_INDEX 之类的 env 透传。

    文件格式（JSON list）：
        [
          {"email": "test1@example.com", "password": "pwd1"},
          {"email": "test2@example.com", "password": "pwd2"}
        ]

    每个 dict 的所有键会被翻译成 `${KEY_UPPER}` 占位符在 case 文本里替换
    （见 _apply_account_placeholders）。约定常用键：email / password /
    phone / username / user_id 等。

    路径算法与 _load_preconditions 一致。文件缺失或解析失败时返回 []，
    不阻塞测试运行 — 用例里的占位符会原样保留，LLM 看到会注意到。
    """
    if target_dir is None:
        return []
    try:
        rel = target_dir.resolve().relative_to(TESTS_DIR.resolve())
    except ValueError:
        return []
    if not rel.parts:
        return []
    f = TESTS_DIR / rel.parts[0] / "_accounts.json"
    if not f.exists():
        return []
    try:
        import json
        data = json.loads(f.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            log.warning("_accounts.json 顶层应是 list，实际是 %s，忽略", type(data).__name__)
            return []
        # 每项必须是 dict
        accounts = [item for item in data if isinstance(item, dict)]
        if len(accounts) != len(data):
            log.warning("_accounts.json 含非-dict 项，已忽略")
        return accounts
    except Exception as e:
        log.warning("_accounts.json 解析失败: %s", e)
        return []


def _apply_account_placeholders(text: str, account: dict) -> str:
    """把 ``${EMAIL}`` / ``${PASSWORD}`` 等占位符替换为 ``account`` 里的值。

    替换规则：account 里每个键 ``foo`` → 占位符 ``${FOO}``（大写）。值为
    str/int/float 时直接 stringify 写入；其他类型跳过。

    无匹配键的占位符保留原样（LLM 看到会注意到异常，比静默替换更稳妥）。
    """
    if not account:
        return text
    for key, val in account.items():
        if not isinstance(val, (str, int, float)):
            continue
        text = text.replace(f"${{{key.upper()}}}", str(val))
    return text


def _load_preconditions(target_dir: Path | None) -> str | None:
    """加载 tests 下第一级目录里的 `_preconditions.md`（如有）。

    用例：tests/nb_cases/_preconditions.md 描述 "登录态怎么判断、不在登录态怎么登录、
    Onboarding 怎么完成、常见拦截弹窗怎么 dismiss"。运行时 prepend 到每个 case 文本前，
    让 LLM 在发现当前屏幕不符合 Background Given 时能照指南先恢复再开始测试。

    路径算法与 auto-report 一致：取 target_dir 相对 TESTS_DIR 的第一级目录名。
    """
    if target_dir is None:
        return None
    try:
        rel = target_dir.resolve().relative_to(TESTS_DIR.resolve())
    except ValueError:
        return None
    if not rel.parts:
        return None
    pre_file = TESTS_DIR / rel.parts[0] / "_preconditions.md"
    if not pre_file.exists():
        return None
    return pre_file.read_text(encoding="utf-8").rstrip()


def _find_target_dir(p: Path) -> Path | None:
    """从一个文件或目录路径向上回溯，找到最近的"含 README.md 的祖先目录"作为 target_dir。

    用于决定 report 目录位置等。约定一个 target 是含 README.md 的目录：
      - tests/eve-kit/                  (README.md + cases/)
      - tests/nb_cases/nb_mobile/       (README.md + 子模块/*.feature)
    """
    resolved = p.resolve()
    tests_resolved = TESTS_DIR.resolve()
    current = resolved if resolved.is_dir() else resolved.parent
    # 向上找直到 TESTS_DIR 之外
    while True:
        if (current / "README.md").exists():
            return current
        if current == tests_resolved or current == current.parent:
            break
        current = current.parent
    # 兜底：用 TESTS_DIR 下的第一个 path part 作为 target_dir
    try:
        rel = resolved.relative_to(tests_resolved)
        if rel.parts:
            return TESTS_DIR / rel.parts[0]
    except ValueError:
        pass
    return None


def _collect_feature_cases(path: Path) -> list[str]:
    """从一个 .feature 文件或含 .feature 的目录（递归）收集 case body。"""
    from . import gherkin
    if path.is_file() and path.suffix == ".feature":
        return gherkin.parse_feature_to_cases(path)
    if path.is_dir():
        cases: list[str] = []
        for ff in sorted(path.rglob("*.feature")):
            cases.extend(gherkin.parse_feature_to_cases(ff))
        return cases
    return []


def _resolve_test_target(test: str) -> tuple[list[str], Path | None]:
    """Resolve test argument to (test_cases, target_dir).

    支持（按检测顺序）：
      - "path/to/file.feature"          → 单 .feature 文件（Gherkin 解析）
      - "path/to/dir/" 或 "tests-rel"   → 已存在目录，递归找 .feature 文件
      - "tests-rel/file.feature"        → TESTS_DIR 相对路径下的 .feature 文件
      - "eve-kit" / "eve-kit/homepage"  → 兼容旧 .md：tests/<name>/cases/
      - "path/to/file.md"               → 兼容旧 .md/.txt 单文件
      - "inline text"                   → 字面 case 文本
    """
    # 1) .feature 文件（绝对路径 / 相对当前目录 / TESTS_DIR 相对路径）
    if test.endswith(".feature"):
        candidates = [Path(test)]
        if not Path(test).is_absolute():
            candidates.append(TESTS_DIR / test)
        for candidate in candidates:
            if candidate.is_file():
                cases = _collect_feature_cases(candidate)
                return cases, _find_target_dir(candidate)

    # 2) 已存在的目录路径（含 .feature 文件，递归）
    dir_candidates = [Path(test), TESTS_DIR / test]
    for candidate in dir_candidates:
        if candidate.is_dir():
            feature_cases = _collect_feature_cases(candidate)
            if feature_cases:
                return feature_cases, _find_target_dir(candidate)

    # 3) （兼容旧 .md 流）target/sub_name → tests/<target>/cases/<sub>.md
    if "/" in test and not test.endswith((".md", ".txt")):
        parts = test.split("/", 1)
        target_dir = TESTS_DIR / parts[0]
        case_file = target_dir / "cases" / f"{parts[1]}.md"
        if case_file.exists():
            with open(case_file) as f:
                return _parse_md_cases(f.read()), target_dir
        # Try as-is (maybe the user typed "eve-kit/cases/foo.md")
        case_file = target_dir / parts[1]
        if case_file.exists():
            with open(case_file) as f:
                return _parse_md_cases(f.read()), target_dir

    # 4) （兼容旧）target 名字（无斜杠）→ tests/<target>/cases/*.md
    target_dir = TESTS_DIR / test
    if target_dir.is_dir() and (target_dir / "cases").is_dir():
        cases_dir = target_dir / "cases"
        all_cases = []
        for case_file in sorted(cases_dir.glob("*.md")):
            with open(case_file) as f:
                all_cases.extend(_parse_md_cases(f.read()))
        if all_cases:
            return all_cases, target_dir

    # 5) （兼容旧）显式 .md/.txt 文件路径
    if test.endswith((".md", ".txt")):
        test_path = Path(test)
        with open(test_path) as f:
            cases = _parse_md_cases(f.read())
        resolved = test_path.resolve()
        if TESTS_DIR.resolve() in resolved.parents:
            rel = resolved.relative_to(TESTS_DIR.resolve())
            target_name = rel.parts[0] if rel.parts else None
            if target_name:
                return cases, TESTS_DIR / target_name
        return cases, None

    # 6) Inline case text
    return [test], None


def cmd_run(test: str, platform: str | None = None, url: str | None = None,
            max_steps: int | None = None, report_path: str | None = None,
            grid_url: str | None = None, concurrency: int = 1,
            devices: list[str] | None = None):
    cfg = load_config()

    if not cfg["llm"]["api_key"]:
        print("Error: API key not configured.")
        print("Run: argus init")
        print("Then edit .env to add your LLM_API_KEY.")
        return

    # CLI overrides
    if platform:
        cfg["platform"] = platform
    if max_steps:
        cfg["agent"]["max_steps"] = max_steps
    if grid_url:
        cfg["browser"]["grid_url"] = grid_url

    # Background mode forces headless
    if os.environ.get("ARGUS_BG_RUN"):
        cfg.setdefault("browser", {})["headless"] = True

    # Resolve test target
    test_cases, target_dir = _resolve_test_target(test)

    # 分片：--shard N/M 让本进程只跑 cases[start:end]，便于多设备并发分摊
    shard_spec = os.environ.get("ARGUS_SHARD", "")  # 兜底：env 也接受
    # 上游 cmd_run() 调用方式两种：args.shard（main）和 env（_launch_background）；
    # 这里统一从 env 取，main() 在派发到 cmd_run 前把 args.shard 写入 env
    if shard_spec:
        try:
            n_str, m_str = shard_spec.split("/", 1)
            shard_idx, shard_total = int(n_str), int(m_str)
            if not (0 <= shard_idx < shard_total):
                raise ValueError(f"shard index {shard_idx} 越界 (total={shard_total})")
            total = len(test_cases)
            # 连续切片（不用步长抽样）：报告里设备 0 跑 case [0:147]、设备 1 跑 [147:294]，
            # 排查时易于追踪。chunk 上取整避免余数 case 漏掉。
            chunk = (total + shard_total - 1) // shard_total
            start = shard_idx * chunk
            end = min(start + chunk, total)
            test_cases = test_cases[start:end]
            log.info("Shard %d/%d: 取 case [%d:%d] / %d 共 %d 个",
                     shard_idx, shard_total, start, end, total, len(test_cases))
        except Exception as e:
            log.error("--shard 解析失败 (%s)，将跑全集: %s", shard_spec, e)

    # 加载 tests/<first-level>/_preconditions.md（如存在）prepend 到每个 case
    # 让 LLM 在 Background fixture 失效时按指南自己恢复，而不是直接 fail
    preconditions = _load_preconditions(target_dir)
    if preconditions:
        test_cases = [f"{preconditions}\n\n{tc}" for tc in test_cases]
        log.info("已加载 _preconditions.md (%d 字符)，prepend 到 %d 个 case",
                 len(preconditions), len(test_cases))

    # 加载账号池。**不在这里做占位符替换** — 把决策推到 runner：
    #   - _run_sequential: 单 device 用 1 个账号（env 指定的 index 或 0）
    #   - _run_dispatched_devices: 每个 worker thread 用各自的账号
    # 这样多设备调度时不同 worker 能拿到不同账号绑定。
    accounts = _load_accounts(target_dir)
    if accounts:
        log.info("已加载账号池：%d 个账号", len(accounts))

    # Default report path: tests/<target>/reports/<timestamp>.html
    if report_path == "" or (report_path is not None and report_path == "auto"):
        report_path = None  # will be set below
    if report_path is None and target_dir:
        # --report flag present without value → auto-generate path
        pass  # only auto-generate when --report is explicitly used
    # 当 --report 走 auto 路径时，记录顶层 reports/ 目录，用于 latest.html 软链
    auto_reports_root: Path | None = None
    if report_path == "__auto__" and target_dir:
        # Auto report 路径：tests 下第一级目录的 reports/<ts>/ 子目录
        # 示例：nb_cases/nb_mobile/01-account/foo.feature → tests/nb_cases/reports/<ts>/foo-<ts>.html
        # 每个 run 单独一个时间戳目录，避免多次跑后 reports/ 根目录散落几十个文件
        # 注意不能用 target_dir/reports/ — 那会落到含 README.md 的祖先目录下，与 cases 混在一起
        log_path = None
        try:
            rel = target_dir.resolve().relative_to(TESTS_DIR.resolve())
        except ValueError:
            rel = None
        if rel and rel.parts:
            timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            auto_reports_root = TESTS_DIR / rel.parts[0] / "reports"
            reports_dir = auto_reports_root / timestamp
            reports_dir.mkdir(parents=True, exist_ok=True)
            test_p = Path(test)
            basename = test_p.stem or "run"
            if test_p.parent.name == "_auto_filter":
                basename = f"{basename}-auto"
            report_path = str(reports_dir / f"{basename}-{timestamp}.html")
            log_path = reports_dir / f"{basename}-{timestamp}.log"
        else:
            # inline 文本或 target_dir 不在 TESTS_DIR 下：不自动落盘，走 stderr
            report_path = None
        # Also attach a file handler so the run log lands next to the HTML report.
        # logger.py sets propagate=False on each argus.* logger to avoid duplicate
        # stderr output, which also means attaching to the `argus` root would
        # collect nothing — we have to walk the existing argus.* loggers.
        if log_path is not None:
            try:
                file_handler = logging.FileHandler(str(log_path), mode="w", encoding="utf-8")
                file_handler.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s",
                                      datefmt="%H:%M:%S")
                )
                for name, logger_obj in list(logging.Logger.manager.loggerDict.items()):
                    if name.startswith("argus") and isinstance(logger_obj, logging.Logger):
                        logger_obj.addHandler(file_handler)
                # Also patch get_logger so loggers created AFTER this point (e.g.
                # platforms instantiated later) also pick up the handler.
                from . import logger as _logger_mod
                _orig_get_logger = _logger_mod.get_logger
                def _patched_get_logger(name):
                    logger = _orig_get_logger(name)
                    if file_handler not in logger.handlers:
                        logger.addHandler(file_handler)
                    return logger
                _logger_mod.get_logger = _patched_get_logger
                log.info("Run log → %s", log_path)
            except Exception as e:
                log.debug("无法附加 file handler: %s", e)

    # Auto-read URL from target README if not specified via CLI
    if not url and target_dir:
        url = _read_target_url(target_dir)
        if url:
            log.info("从 README.md 读取起始 URL: %s", url)

    log.info("共 %d 个测试用例", len(test_cases))

    if devices and len(devices) > 1:
        # 多 Android 设备：动态调度器，N worker 各持 Agent 抢任务
        log.info("调度模式: %d 台设备", len(devices))
        if accounts and len(accounts) < len(devices):
            log.warning("账号池 %d 个 < 设备 %d 台，最后 %d 台将共用 accounts[0]",
                        len(accounts), len(devices), len(devices) - len(accounts))
        results = _run_dispatched_devices(cfg, test_cases, devices, accounts, url)
    elif concurrency > 1:
        # Browser + Selenium Grid：thread pool 多 agent 共抢任务
        results = _run_concurrent(cfg, test_cases, url, concurrency)
    else:
        # 单设备（或单 browser）：顺序跑，用账号池第 0 项（无并发，无需分配）
        single_account = accounts[0] if accounts else None
        if single_account:
            log_safe = {k: v for k, v in single_account.items()
                        if "pass" not in k.lower() and "secret" not in k.lower()}
            log.info("单设备账号 [1/%d]: %s", len(accounts), log_safe)
        results = _run_sequential(cfg, test_cases, url, account=single_account)

    # Console summary
    print(f"\n{'='*60}")
    print("测试报告")
    print(f"{'='*60}")
    passed = sum(1 for r in results if r["result"] == "pass")
    skipped = sum(1 for r in results if r["result"] == "skipped")
    total = len(results)
    failed = total - passed - skipped
    total_dur = sum(r.get("duration", 0) for r in results)
    for r in results:
        if r["result"] == "pass":
            status = "PASS"
        elif r["result"] == "skipped":
            status = "SKIP"
        else:
            status = "FAIL"
        dur = f"{r.get('duration', 0):.1f}s"
        print(f"  [{status}] {r['case'][:60]}  ({dur})")
        print(f"        {r.get('reason', '')} ({r['steps']} steps)")
    print(f"\n  总计: {passed}/{total} 通过, {failed} 失败, {skipped} 跳过 | 总耗时: {total_dur:.1f}s")

    # Export report if requested
    if report_path:
        from .report import save_html, save_json
        if report_path.endswith(".json"):
            save_json(results, report_path)
        else:
            if not report_path.endswith(".html"):
                report_path += ".html"
            save_html(results, report_path)
            # 同时落一份伴生 .json：后台 run 的 status/report 工具（CLI &
            # MCP）靠同名 .json 读 summary 和结构化结果，HTML 无法解析。
            save_json(results, str(Path(report_path).with_suffix(".json")))
        print(f"\n  报告已保存: {report_path}")

        # Update latest symlink
        # auto 路径下 report 落在 reports/<ts>/ 里，latest.html 要放到顶层 reports/ 才有意义
        # 显式 --report PATH 时仍把 latest.html 放在该路径同级目录
        report_p = Path(report_path)
        if auto_reports_root is not None:
            latest = auto_reports_root / "latest.html"
            target_rel = f"{report_p.parent.name}/{report_p.name}"
        else:
            latest = report_p.parent / "latest.html"
            target_rel = report_p.name
        if latest.is_symlink() or latest.exists():
            latest.unlink()
        latest.symlink_to(target_rel)
        print(f"  最新报告:   {latest}")


def _run_sequential(cfg: dict, test_cases: list[str], url: str | None,
                    account: dict | None = None) -> list[dict]:
    """Run test cases sequentially with a single Agent.

    If ``account`` provided, every case has its ${EMAIL}/${PASSWORD}/...
    placeholders substituted before being sent to Agent.run().
    """
    log.info("创建 Agent...")
    agent = Agent(config=cfg)
    log.info("Agent 创建完成")

    results = []
    current_platform = cfg.get("platform", "")
    for i, raw_tc in enumerate(test_cases):
        tc = _apply_account_placeholders(raw_tc, account) if account else raw_tc
        # Platform tag mismatch → skip this case before running pm_clear etc
        skip_reason = _should_skip_by_platform(tc, current_platform) or _should_skip_by_automation(tc)
        if skip_reason:
            log.info("[%d/%d] SKIP: %s", i + 1, len(test_cases), skip_reason)
            results.append({
                "case": tc,
                "result": "skipped",
                "reason": skip_reason,
                "steps": 0,
                "duration": 0,
                "steps_detail": [],
            })
            continue

        reset_url = url or _extract_target_url(tc)
        if reset_url:
            log.info("[%d/%d] 复位导航: %s", i + 1, len(test_cases), reset_url)
            # Clear browser cookies / storage so scenarios don't inherit prior
            # state (language preference, modal-dismissal flags, etc.).
            try:
                drv = getattr(agent.platform, "_driver", None)
                if drv is not None:
                    drv.delete_all_cookies()
                    drv.execute_script(
                        "try { window.localStorage.clear(); "
                        "window.sessionStorage.clear(); } catch (e) {}"
                    )
            except Exception as e:
                log.debug("状态清理跳过: %s", e)

            agent.platform.open_target(reset_url)
            log.info("[%d/%d] 导航完成, 等待 3s...", i + 1, len(test_cases))
            time.sleep(3)

        # Android per-case state reset via `**Reset before**: <mode>` directive.
        reset_mode = _extract_reset_mode(tc)
        if reset_mode and reset_mode != "none":
            log.info("[%d/%d] Android reset: %s", i + 1, len(test_cases), reset_mode)
            _reset_android_state(agent.platform, reset_mode)

        tc = _substitute_placeholders(tc)

        log.info("[%d/%d] 开始执行: %s", i + 1, len(test_cases), tc[:60])
        result = agent.run(tc)
        log.info("[%d/%d] 结果: %s", i + 1, len(test_cases), result.get("result", "?"))
        results.append({"case": tc, **result})

    return results


def _check_or_reconnect_device(serial: str, agent, max_attempts: int = 2) -> bool:
    """Quick device health check; on failure try adb connect + u2 reconnect.

    Worker 用这个在每个 case 开始前检查设备。WiFi/TLS adb 经常在跑测中
    掉线（设备息屏 / 网络抖动 / adb daemon 重启），掉了之后 worker 持有
    的 platform handle 是死的，后续每个 case screenshot 都瞬间抛
    `adb: device offline`，每个 case 0.9s instant fail。

    返回 True 表示设备 reachable 且 u2 已重连可用；False 表示尽力后仍连
    不上，调用方应跳过该 case 或让 worker 退出。
    """
    import shutil
    import time as _t

    adb_path = shutil.which("adb") or os.path.expanduser(
        "~/Library/Android/sdk/platform-tools/adb"
    )

    def is_online() -> bool:
        try:
            res = subprocess.run(
                [adb_path, "-s", serial, "get-state"],
                capture_output=True, text=True, timeout=5,
            )
            return res.returncode == 0 and res.stdout.strip() == "device"
        except Exception:
            return False

    if is_online():
        return True

    log.warning("Device %s 离线，尝试重连...", serial)

    # 只有 host:port 形式可以 adb connect 重连；mDNS 服务名只能等设备
    # 主动广播（参考 _ensure_devices_connected 注释）
    is_network_addr = ":" in serial and "_adb-tls-" not in serial
    for attempt in range(1, max_attempts + 1):
        if is_network_addr:
            try:
                res = subprocess.run(
                    [adb_path, "connect", serial],
                    capture_output=True, text=True, timeout=8,
                )
                tag = (res.stdout + res.stderr).strip().splitlines()[-1] if (res.stdout or res.stderr) else ""
                log.info("  adb connect %s (attempt %d): %s", serial, attempt, tag)
            except Exception as e:
                log.debug("  adb connect %s 异常: %s", serial, e)
        _t.sleep(2)
        if is_online():
            # 重置 u2 client，它在设备 offline 期间可能 cache 了 broken HTTP session
            try:
                import uiautomator2 as u2
                if hasattr(agent, "platform") and hasattr(agent.platform, "_u2"):
                    agent.platform._u2 = u2.connect(serial)
                    log.info("  Reset u2 client for %s", serial)
            except Exception as e:
                log.warning("  u2 reconnect 失败: %s", e)
            return True

    return False


def _run_dispatched_devices(cfg: dict, test_cases: list[str],
                            devices: list[str], accounts: list[dict],
                            url: str | None) -> list[dict]:
    """N-Android-device 动态调度器。

    一个共享 case 队列 + N 个 worker thread，每个 worker：
      * 自己的 Agent 实例（绑定一台 serial）
      * 自己的账号（accounts[i]）
      * 谁先空闲谁拉下一个 case

    跟静态预分片 (cases[start:end]) 比，动态调度自动负载均衡：某台设
    备在难 case 上卡 N 分钟时，其他设备照常拉下一个 case 跑，不会被
    最慢的设备拖死。

    报告聚合按原始 case index 回填，最终顺序跟 test_cases 输入一致。
    """
    import copy
    import threading
    from concurrent.futures import as_completed, ThreadPoolExecutor
    from queue import Empty, Queue

    n = len(devices)

    # Per-device cfg：复制顶层 cfg 后改 serial（避免 share 同一 dict 引起 race）
    per_device_cfg: list[dict] = []
    for serial in devices:
        c = copy.deepcopy(cfg)
        c.setdefault("android", {})["serial"] = serial
        per_device_cfg.append(c)

    # Per-device Agent 实例 — 单台失败时降级，其他设备继续。
    # 容错动机：3 台并发跑大批 case 时，WiFi 设备掉线 / 某台 adb 卡死 / 某台
    # uiautomator2 推 apk 失败 都不该让整批 abort。残存 N-1 台仍能跑完，
    # 失败的设备/账号记 log，最终报告里能看到「跑了 X 台、Y 台 fail-to-start」。
    log.info("调度: 创建 %d 个 Agent (每台设备一个)...", n)
    agents: list[Agent] = []
    alive_devices: list[str] = []
    alive_accounts: list[dict] = []
    failed_devices: list[tuple[str, str]] = []
    for i, c in enumerate(per_device_cfg):
        serial = devices[i]
        acct = accounts[i] if i < len(accounts) else (accounts[0] if accounts else {})
        log_safe = {k: v for k, v in acct.items()
                    if "pass" not in k.lower() and "secret" not in k.lower()}
        log.info("  Agent #%d device=%s account=%s", i + 1, serial, log_safe)
        try:
            agents.append(Agent(config=c))
            alive_devices.append(serial)
            alive_accounts.append(acct)
        except Exception as e:
            log.error("  Agent #%d (device=%s) 启动失败，跳过该设备: %s",
                      i + 1, serial, e)
            failed_devices.append((serial, str(e)))

    if not agents:
        raise RuntimeError(
            f"全部 {n} 台设备 Agent 启动均失败，无法运行调度器: {failed_devices}"
        )

    if failed_devices:
        log.warning("⚠️ %d/%d 台设备启动失败，仅用剩余 %d 台跑 — 失败列表: %s",
                    len(failed_devices), n, len(agents),
                    [f[0] for f in failed_devices])

    # 用幸存的 device / account 列表覆盖 — 后续 worker 函数引用的是这些
    n = len(agents)
    devices = alive_devices
    accounts = alive_accounts
    log.info("%d 个 Agent ready (跳过 %d 台)", n, len(failed_devices))

    # 共享 case 队列：(原始 index, raw_case_text)
    queue: "Queue[tuple[int, str]]" = Queue()
    for i, tc in enumerate(test_cases):
        queue.put((i, tc))

    total = len(test_cases)
    results: list[dict | None] = [None] * total
    results_lock = threading.Lock()
    counter = [0]
    counter_lock = threading.Lock()
    current_platform = cfg.get("platform", "")

    def worker(worker_idx: int):
        # 在 worker thread 入口给 logger 设 worker 标签，让 log 行能区分是哪个
        # worker 打的。case 开始时再细化为 W{idx}/c{N}。
        from .logger import set_case_context
        set_case_context(f"W{worker_idx}")

        agent = agents[worker_idx]
        device = devices[worker_idx]
        # 账号池小于设备数时尾部设备 fallback 用 accounts[0]
        account = (accounts[worker_idx] if worker_idx < len(accounts)
                   else (accounts[0] if accounts else None))
        acct_email = account.get("email", "(no-account)") if account else "(no-account)"
        log.info("[Worker %d device=%s account=%s] start", worker_idx, device, acct_email)

        # 设备健康自愈状态：连续 N 次 health check 失败该 worker 退出，
        # 把剩余 case 让其他 worker 抢走。避免一台设备挂了拖死一摞 case。
        consecutive_offline = 0
        MAX_OFFLINE_BEFORE_WORKER_EXIT = 3

        while True:
            try:
                idx, raw_case = queue.get_nowait()
            except Empty:
                break

            # 进入该 case，更新 logger context — 后续所有 log 行都带 [W{idx}/c{N}]
            set_case_context(f"W{worker_idx}/c{idx + 1}")

            # 0) Pre-case health check：检查设备 online + 必要时 reconnect
            #    防止 worker 持有 dead platform handle 后续 case 全 instant fail
            if not _check_or_reconnect_device(device, agent):
                consecutive_offline += 1
                log.warning(
                    "[Worker %d/%s] case %d pre-check 设备离线 (连续 %d/%d 次)，"
                    "把 case 放回队列让其他 worker 尝试",
                    worker_idx, device, idx, consecutive_offline,
                    MAX_OFFLINE_BEFORE_WORKER_EXIT
                )
                queue.put((idx, raw_case))
                if consecutive_offline >= MAX_OFFLINE_BEFORE_WORKER_EXIT:
                    log.error(
                        "[Worker %d/%s] 连续 %d 次设备离线，worker 退出 — "
                        "剩余 case 由其他 worker 接管",
                        worker_idx, device, MAX_OFFLINE_BEFORE_WORKER_EXIT
                    )
                    return
                time.sleep(5)
                continue
            consecutive_offline = 0

            # 1) 替换账号占位符（per-worker，每台设备用各自账号）
            tc = _apply_account_placeholders(raw_case, account) if account else raw_case

            # 2) 平台 / automation tag 过滤
            skip_reason = (_should_skip_by_platform(tc, current_platform)
                           or _should_skip_by_automation(tc))
            with counter_lock:
                counter[0] += 1
                n_done = counter[0]
            if skip_reason:
                log.info("[%d/%d Worker %d] SKIP: %s", n_done, total, worker_idx, skip_reason)
                with results_lock:
                    results[idx] = {
                        "case": tc, "result": "skipped", "reason": skip_reason,
                        "steps": 0, "duration": 0, "steps_detail": [],
                        "device": device,
                    }
                continue

            # 3) Android per-case state reset
            reset_mode = _extract_reset_mode(tc)
            if reset_mode and reset_mode != "none":
                try:
                    _reset_android_state(agent.platform, reset_mode)
                except Exception as e:
                    log.warning("[Worker %d] reset 失败: %s", worker_idx, e)

            tc = _substitute_placeholders(tc)

            log.info("[%d/%d Worker %d/%s] 执行: %s",
                     n_done, total, worker_idx, device, tc[:60])
            try:
                result = agent.run(tc)
                log.info("[%d/%d Worker %d] 结果: %s",
                         n_done, total, worker_idx, result.get("result", "?"))
            except Exception as e:
                # exc_info so a framework crash (vs a test fail) leaves a stack
                # in the log instead of a bare message — a bare "division by
                # zero" cost a full debug cycle once.
                log.error("[%d/%d Worker %d] 用例异常: %s",
                          n_done, total, worker_idx, e, exc_info=True)
                result = {"result": "error", "reason": str(e),
                          "steps": 0, "duration": 0, "steps_detail": []}

            with results_lock:
                results[idx] = {"case": tc, **result, "device": device}

        log.info("[Worker %d device=%s] 队列空，退出", worker_idx, device)

    # 启动 N 个 worker thread
    with ThreadPoolExecutor(max_workers=n, thread_name_prefix="argus-dev") as pool:
        futures = [pool.submit(worker, i) for i in range(n)]
        for f in as_completed(futures):
            try:
                f.result()
            except Exception as e:
                log.error("Worker 异常退出: %s", e)

    # Teardown agents
    for ag in agents:
        try:
            ag.platform.teardown()
        except Exception:
            pass

    # 填充任何因异常残留的 None slot
    for i, r in enumerate(results):
        if r is None:
            results[i] = {
                "case": test_cases[i], "result": "error",
                "reason": "worker did not produce result",
                "steps": 0, "duration": 0, "steps_detail": [],
            }

    return results  # type: ignore[return-value]


def _run_concurrent(cfg: dict, test_cases: list[str], url: str | None,
                    concurrency: int) -> list[dict]:
    """Run test cases concurrently with multiple Agents on Selenium Grid."""
    import copy
    from concurrent.futures import ThreadPoolExecutor, as_completed
    import threading

    # Clean up Grid sessions once before creating agents
    grid_url = cfg.get("browser", {}).get("grid_url", "")
    if grid_url:
        _cleanup_grid_once(grid_url)

    # Disable per-agent Grid cleanup
    cfg_no_cleanup = copy.deepcopy(cfg)
    cfg_no_cleanup.setdefault("browser", {})["_skip_grid_cleanup"] = True

    log.info("并发模式: 创建 %d 个 Agent...", concurrency)
    agents = []
    for i in range(concurrency):
        log.info("创建 Agent #%d...", i + 1)
        agent = Agent(config=copy.deepcopy(cfg_no_cleanup))
        agents.append(agent)
    log.info("%d 个 Agent 创建完成", len(agents))

    # Thread-safe agent pool
    agent_pool = agents[:]
    pool_lock = threading.Lock()
    total = len(test_cases)
    counter = [0]  # mutable counter for progress
    counter_lock = threading.Lock()

    current_platform = cfg.get("platform", "")

    def run_one(tc: str) -> dict:
        # Platform tag mismatch → skip before acquiring agent
        skip_reason = _should_skip_by_platform(tc, current_platform) or _should_skip_by_automation(tc)
        if skip_reason:
            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            log.info("[%d/%d] SKIP: %s", idx, total, skip_reason)
            return {
                "case": tc, "result": "skipped", "reason": skip_reason,
                "steps": 0, "duration": 0, "steps_detail": [],
            }
        # Acquire an agent from the pool
        with pool_lock:
            agent = agent_pool.pop()
        try:
            # Reset browser state between scenarios (cookies, storage, URL)
            # so each case starts fresh regardless of which case ran before.
            reset_url = url or _extract_target_url(tc)
            if reset_url:
                try:
                    drv = getattr(agent.platform, "_driver", None)
                    if drv is not None:
                        drv.delete_all_cookies()
                        drv.execute_script(
                            "try { window.localStorage.clear(); "
                            "window.sessionStorage.clear(); } catch (e) {}"
                        )
                except Exception as e:
                    log.debug("状态清理跳过: %s", e)
                agent.platform.open_target(reset_url)
                time.sleep(3)

            with counter_lock:
                counter[0] += 1
                idx = counter[0]
            log.info("[%d/%d] 开始执行: %s", idx, total, tc[:60])
            result = agent.run(tc)
            log.info("[%d/%d] 结果: %s", idx, total, result.get("result", "?"))
            return {"case": tc, **result}
        finally:
            # Return agent to pool
            with pool_lock:
                agent_pool.append(agent)

    results = [None] * total
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        future_to_idx = {
            executor.submit(run_one, tc): i
            for i, tc in enumerate(test_cases)
        }
        for future in as_completed(future_to_idx):
            idx = future_to_idx[future]
            try:
                results[idx] = future.result()
            except Exception as e:
                log.error("用例执行异常: %s", e)
                results[idx] = {
                    "case": test_cases[idx],
                    "result": "error",
                    "reason": str(e),
                    "steps": 0,
                    "duration": 0,
                    "steps_detail": [],
                }

    # Teardown all agents
    for agent in agents:
        try:
            agent.platform.teardown()
        except Exception:
            pass

    return results


def _cleanup_grid_once(grid_url: str):
    """Clean up all stale Grid sessions (called once before creating agents)."""
    import json
    import urllib.request
    try:
        status_url = grid_url.rstrip("/") + "/status"
        with urllib.request.urlopen(status_url, timeout=5) as resp:
            data = json.loads(resp.read())
        nodes = data.get("value", {}).get("nodes", [])
        for node in nodes:
            for slot in node.get("slots", []):
                session = slot.get("session")
                if session:
                    sid = session["sessionId"]
                    log.info("清理残留 session: %s", sid)
                    delete_url = grid_url.rstrip("/") + f"/session/{sid}"
                    req = urllib.request.Request(delete_url, method="DELETE")
                    try:
                        urllib.request.urlopen(req, timeout=5)
                    except Exception:
                        pass
    except Exception as e:
        log.debug("Grid 清理跳过: %s", e)


# ── Figma commands ────────────────────────────────────────────


def cmd_figma(args):
    """Handle figma subcommands."""
    cfg = load_config()
    figma_token = cfg["figma"]["token"]

    if not figma_token:
        print("Error: Figma token not configured.")
        print("Set FIGMA_TOKEN in .env (Figma → Settings → Personal Access Tokens)")
        return

    if args.figma_command == "frames":
        cmd_figma_frames(figma_token, args.url, page=args.page)
    elif args.figma_command == "gen-tests":
        cmd_figma_gen_tests(figma_token, args.url, cfg["llm"], output=args.output)
    elif args.figma_command == "review":
        cmd_figma_review(figma_token, args.url, cfg,
                         platform_name=args.platform,
                         screenshot_path=args.screenshot,
                         output=args.output)
    else:
        print("Usage: argus figma {frames|gen-tests|review}")


def cmd_figma_frames(token: str, url: str, page: str | None = None):
    """List all frames in a Figma file."""
    from .figma import parse_figma_url
    from .figma_via_mcp import get_figma_client
    client = get_figma_client(token)
    file_key, _ = parse_figma_url(url) if "figma.com" in url else (url, None)

    frames = client.list_frames(file_key, page_name=page)
    if not frames:
        print("No frames found.")
        return

    print(f"{'Frame':<40s} {'Page':<20s} {'Size':<15s} {'ID'}")
    print("-" * 90)
    for f in frames:
        size = f"{f['width']:.0f}x{f['height']:.0f}"
        print(f"  {f['name']:<38s} {f['page']:<20s} {size:<15s} {f['id']}")


def cmd_figma_gen_tests(token: str, url: str, llm_config: dict,
                         output: str | None = None):
    """Generate test cases from Figma design."""
    from .figma_ops import gen_tests_from_figma

    if not llm_config.get("api_key"):
        print("Error: LLM API key not configured. Set LLM_API_KEY in .env")
        return

    print("正在从 Figma 设计稿生成测试用例...\n")
    tests_yaml = gen_tests_from_figma(token, url, llm_config)

    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(tests_yaml)
        print(f"\n测试用例已保存: {output}")
    else:
        print(tests_yaml)


def cmd_figma_review(token: str, url: str, cfg: dict,
                      platform_name: str | None = None,
                      screenshot_path: str | None = None,
                      output: str | None = None):
    """Visual review: compare Figma design with actual screenshot."""
    from .figma_ops import visual_review, review_with_platform

    llm_config = cfg["llm"]
    if not llm_config.get("api_key"):
        print("Error: LLM API key not configured. Set LLM_API_KEY in .env")
        return

    if screenshot_path:
        # Use provided screenshot
        screenshot_png = Path(screenshot_path).read_bytes()
        print(f"使用截图: {screenshot_path}")
        result = visual_review(token, url, screenshot_png, llm_config)
    else:
        # Take live screenshot from platform
        pname = platform_name or cfg.get("platform", "ios")
        from .platforms import create_platform
        platform = create_platform(pname, cfg)
        platform.setup(cfg)
        print(f"正在从 {pname} 平台截图...")
        result = review_with_platform(token, url, platform, llm_config)
        platform.teardown()

    # Display result
    score = result.get("score", 0)
    summary = result.get("summary", "")
    issues = result.get("issues", [])
    highlights = result.get("highlights", [])

    print(f"\n{'='*60}")
    print(f"视觉走查报告")
    print(f"{'='*60}")
    print(f"\n  还原度评分: {score}/100")
    print(f"  总结: {summary}\n")

    if issues:
        print("  问题列表:")
        for i, issue in enumerate(issues, 1):
            sev = issue.get("severity", "?")
            cat = issue.get("category", "?")
            desc = issue.get("description", "")
            loc = issue.get("location", "")
            sev_icon = {"high": "!!!", "medium": " !!", "low": "  !"}.get(sev, "  ?")
            print(f"    {sev_icon} [{cat}] {desc}")
            if loc:
                print(f"          位置: {loc}")

    if highlights:
        print("\n  亮点:")
        for h in highlights:
            print(f"    + {h}")

    # Save report
    if output:
        report_data = {k: v for k, v in result.items() if k != "design_png"}
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        if output.endswith(".json"):
            Path(output).write_text(json.dumps(report_data, ensure_ascii=False, indent=2))
        else:
            # Simple HTML report for visual review
            _save_review_html(result, output if output.endswith(".html") else output + ".html")
        print(f"\n  报告已保存: {output}")


def _save_review_html(result: dict, path: str):
    """Save visual review result as HTML with embedded images."""
    import base64
    score = result.get("score", 0)
    summary = result.get("summary", "")
    issues = result.get("issues", [])
    highlights = result.get("highlights", [])

    design_png = result.get("design_png", b"")
    design_b64 = base64.standard_b64encode(design_png).decode() if design_png else ""

    score_color = "#22c55e" if score >= 80 else "#f59e0b" if score >= 60 else "#ef4444"

    issues_html = ""
    for issue in issues:
        sev = issue.get("severity", "low")
        sev_cls = {"high": "sev-high", "medium": "sev-med"}.get(sev, "sev-low")
        issues_html += f"""
        <div class="issue {sev_cls}">
          <span class="sev">{sev.upper()}</span>
          <span class="cat">[{issue.get('category', '')}]</span>
          {issue.get('description', '')}
          <div class="loc">{issue.get('location', '')}</div>
        </div>"""

    highlights_html = "".join(f"<li>{h}</li>" for h in highlights)
    design_img = (f'<img src="data:image/png;base64,{design_b64}" class="design-img"/>'
                  if design_b64 else "")

    html = f"""<!DOCTYPE html>
<html lang="zh"><head><meta charset="utf-8">
<title>Argus 视觉走查报告</title>
<style>
  body {{ font-family: -apple-system, sans-serif; background: #f5f5f5; padding: 24px; color: #333; }}
  h1 {{ font-size: 22px; }} h2 {{ font-size: 16px; margin-top: 20px; }}
  .score {{ font-size: 48px; font-weight: 700; color: {score_color}; }}
  .summary {{ font-size: 15px; color: #666; margin: 8px 0 16px; }}
  .issue {{ padding: 8px 12px; margin: 6px 0; background: #fff; border-radius: 6px;
            border-left: 4px solid #ccc; font-size: 14px; }}
  .sev {{ font-weight: 700; margin-right: 6px; }}
  .cat {{ color: #888; margin-right: 4px; }}
  .loc {{ font-size: 12px; color: #999; margin-top: 2px; }}
  .sev-high {{ border-left-color: #ef4444; }} .sev-high .sev {{ color: #ef4444; }}
  .sev-med {{ border-left-color: #f59e0b; }} .sev-med .sev {{ color: #f59e0b; }}
  .sev-low {{ border-left-color: #3b82f6; }} .sev-low .sev {{ color: #3b82f6; }}
  .design-img {{ max-width: 400px; border: 1px solid #ddd; border-radius: 8px; margin-top: 12px; }}
  ul {{ padding-left: 20px; }} li {{ margin: 4px 0; font-size: 14px; }}
  .footer {{ text-align: center; color: #bbb; font-size: 12px; margin-top: 24px; }}
</style></head><body>
  <h1>Argus 视觉走查报告</h1>
  <div class="score">{score}</div>
  <div class="summary">{summary}</div>
  {f'<h2>设计稿</h2>{design_img}' if design_img else ''}
  <h2>问题 ({len(issues)})</h2>
  {issues_html if issues else '<p style="color:#999">无问题</p>'}
  {f'<h2>亮点</h2><ul>{highlights_html}</ul>' if highlights else ''}
  <div class="footer">Generated by Argus</div>
</body></html>"""

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(html)


def _parse_md_cases(text: str) -> list[str]:
    """Parse a TDD-style markdown test file into individual cases.

    Splits on `### TC-XXX` headers. Each case spans from its header to the
    next `### TC-` header or EOF.

    If the file contains a `## Hints` (or `## 元素位置 Hints`) section before
    the first case, that section is prepended to every case as shared context
    (useful for documenting hard-to-locate small UI elements once per file).

    Other text before the first `### TC-` header (intro, matrix table, etc.)
    is discarded.

    If no `### TC-` headers are present, the whole text is treated as one
    inline case (for use with ad-hoc cli runs).
    """
    cases = []
    current = None
    preamble_lines: list[str] = []

    def _is_case_header(line: str) -> bool:
        s = line.lstrip()
        return s.startswith("###") and "TC-" in s

    for line in text.splitlines():
        if _is_case_header(line):
            if current is not None:
                cases.append("\n".join(current).rstrip())
            current = [line]
        elif current is not None:
            current.append(line)
        else:
            preamble_lines.append(line)

    if current is not None:
        cases.append("\n".join(current).rstrip())

    if not cases:
        stripped = text.strip()
        return [stripped] if stripped else []

    # Extract a `## Hints` section from the preamble if present, and
    # prepend it to every case so the LLM always sees the position hints.
    preamble = "\n".join(preamble_lines)
    hints_match = re.search(
        r"^(##\s*(?:Hints|元素位置 Hints|元素位置参考)[^\n]*\n.*?)(?=\n##\s|\Z)",
        preamble, re.MULTILINE | re.DOTALL,
    )
    if hints_match:
        hints_block = hints_match.group(1).rstrip()
        cases = [f"{hints_block}\n\n{c}" for c in cases]

    return cases


if __name__ == "__main__":
    main()
