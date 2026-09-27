"""Android reset, device readiness, installation and reconnection."""

import os
import shutil
import subprocess
import time
from saygo.config import load_config
from saygo.logger import get_logger


log = get_logger("qa.device_setup")


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
            "或跑测时 `ANDROID_PACKAGE=com.example.app python3 -m saygo.cli run …` 覆盖。"
            "（不设默认值，防止静默测错 App。）"
        )
    return pkg


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
    # 非 Android 平台直接 no-op（docstring 承诺过，但之前 _require_android_package
    # 会先抛错，导致 windows/desktop 上带 `Reset before` 的 case 整个 run 崩掉）。
    if getattr(platform, "platform_name", "") != "android":
        return

    # 包名取不到即报错（在 try 之外，确保不被下方 except 吞成 warning）
    package = package or _require_android_package()

    # Appium 路径（当前唯一在用的驱动）：走进程内 Appium 原语拉起/清数据，不碰 adb。
    # 这是 case 启动时把被测 App 切到前台的唯一可靠入口——不重置就会误操作前台残留的
    # 其它 App（如设备上开着的另一个被测包）。
    if hasattr(platform, "reset_app") and not hasattr(platform, "_adb"):
        platform.reset_app(package, mode)
        return
    if not hasattr(platform, "_adb"):
        return
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
        # Last non-empty line is the component name
        # (e.g. "com.example.app/com.example.app.MainActivity")
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
