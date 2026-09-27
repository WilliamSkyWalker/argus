"""跨进程 device session —— 让 `saygo device` CLI 子命令（每次一个进程）复用同一个
常驻 Appium session，不必每次重建（重建要 2-4s 且丢前台状态）。

原理：
- Appium **server** 由 AppiumServerManager 常驻（起了就一直在）。
- Appium **session** 在 newCommandTimeout(600s) 内存活于 server 上。
- `saygo device start` 建 session 后，把 {server_url, session_id, os, 屏幕尺寸} 落到
  状态文件 `~/.saygo/device-sessions/<serial>.json`。
- 后续 `saygo device tap/screenshot/...` 读状态文件，用 session_id **重连**已有 session
  （不建新 session），做完即退进程，session 仍留在 server 上供下次复用。

重连用的是 selenium 已知手法：临时拦截 newSession 命令，让 webdriver.Remote 直接认领
已有 session_id，而不真正开新 session。

任何能跑 shell 的 agent 都可调 `saygo device *`，故这是 saygo 对外的通用设备驱动接口。
"""

import json
import os
from pathlib import Path

from ..logger import get_logger

log = get_logger("device.session")

STATE_DIR = Path(os.environ.get("SAYGO_HOME_DIR", Path.home() / ".saygo")) / "device-sessions"


def release_controller(plat, kind=None):
    """Release a temporary controller without ending the persistent device session."""
    if plat is None:
        return
    kind = kind or getattr(plat, "platform_name", None)
    if kind in {"mac", "windows"}:
        plat.teardown()
    elif hasattr(plat, "disconnect"):
        plat.disconnect()
    elif kind == "browser":
        # ChromeDriver may be temporary, but Chrome belongs to the saved session.
        service = getattr(getattr(plat, "_driver", None), "service", None)
        if service:
            service.stop()
    # Appium sessions remain alive for the next command; never call quit here.


def _key(serial: str | None) -> str:
    import re
    value = serial or "default"
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", value):
        raise ValueError("Invalid session name")
    return value


def _state_path(serial: str | None) -> Path:
    return STATE_DIR / f"{_key(serial)}.json"


def save_state(serial: str | None, data: dict) -> None:
    import tempfile
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    target = _state_path(serial)
    fd, temporary = tempfile.mkstemp(prefix=".session-", suffix=".json", dir=STATE_DIR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        Path(temporary).unlink(missing_ok=True)


def load_state(serial: str | None) -> dict | None:
    p = _state_path(serial)
    if not p.is_file():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def clear_state(serial: str | None) -> None:
    try:
        _state_path(serial).unlink(missing_ok=True)
    except Exception:
        pass


def _attach_driver(server_url: str, session_id: str, os_name: str):
    """重连到 server 上已存在的 Appium session（不建新 session）。"""
    from appium import webdriver
    from appium.options.android import UiAutomator2Options
    from appium.options.ios import XCUITestOptions
    from selenium.webdriver.remote.webdriver import WebDriver

    opts = XCUITestOptions() if os_name == "ios" else UiAutomator2Options()
    # 给一组最小 caps，避免 options 校验报错（不会被真正使用，newSession 被拦截）
    opts.set_capability("appium:automationName", "XCUITest" if os_name == "ios" else "UiAutomator2")

    original_execute = WebDriver.execute

    def _patched_execute(self, command, params=None):
        if command == "newSession":
            # W3C 响应结构：selenium start_session 取 value.sessionId / value.capabilities
            return {"value": {"sessionId": session_id, "capabilities": {}}}
        return original_execute(self, command, params)

    WebDriver.execute = _patched_execute
    try:
        drv = webdriver.Remote(command_executor=server_url, options=opts)
    finally:
        WebDriver.execute = original_execute
    drv.session_id = session_id
    return drv


def _platform_from_driver(drv, os_name: str, state: dict):
    """用已连上的 driver 组装一个 AppiumPlatform（还原屏幕尺寸缓存）。"""
    from .appium import AppiumPlatform
    plat = AppiumPlatform()
    plat._driver = drv
    plat._os = os_name
    plat._server_url = state.get("server_url", "")
    plat._screen_width = int(state.get("screen_width") or 0)
    plat._screen_height = int(state.get("screen_height") or 0)
    if not (plat._screen_width and plat._screen_height):
        plat._detect_screen_size()
    return plat


def start(serial: str | None, os_name: str = "android", browser_backend: str | None = None, appium_config: dict | None = None) -> "object":
    """新建一个 device session 并落状态文件，返回平台对象。已有存活 session 则复用。

    os_name="browser" → 常驻 Chrome（remote-debugging-port，跨进程按 debuggerAddress
    重连，与 Appium 那套对称）；否则走 Appium（android/ios）。"""
    if os_name == "browser":
        return _browser_start(serial, backend=browser_backend)
    if browser_backend is not None:
        raise ValueError("--backend is only supported for browser sessions")
    # 已有状态且能连通 → 直接复用，不重复建
    existing = attach(serial, quiet=True)
    if existing is not None:
        return existing
    from .appium import AppiumPlatform
    plat = AppiumPlatform()
    config = {"os": os_name, "device": serial or "", **(appium_config or {})}
    plat.setup({"appium": config})
    save_state(serial, {
        "server_url": plat._server_url,
        "session_id": plat._driver.session_id,
        "os": plat._os,
        "screen_width": plat._screen_width,
        "screen_height": plat._screen_height,
        "serial": serial or "",
        "device_id": config["device"],
    })
    # start 亲手起的 server 不能在进程退出时被关（要留给后续 device 命令）→ 交出所有权
    if plat._server is not None:
        plat._server._owned = False
    log.info("device session 就绪: serial=%s session=%s", _key(serial), plat._driver.session_id[:8])
    return plat


def attach(serial: str | None, quiet: bool = False) -> "object | None":
    """重连到状态文件里记录的 session；连不上返回 None。"""
    state = load_state(serial)
    if not state:
        if not quiet:
            log.warning("无 device session 状态文件（先跑 saygo device start）: %s", _key(serial))
        return None
    if state.get("handoff"):
        raise RuntimeError("Session waiting_for_human; resume before operating")
    if state.get("disconnected"):
        return None
    if state.get("kind") == "desktop":
        return attach_desktop(state, serial=serial)
    if state.get("kind") == "browser":
        return _browser_attach(serial, state, quiet=quiet)
    try:
        drv = _attach_driver(state["server_url"], state["session_id"], state.get("os", "android"))
        # 探活：读一下 window size，失败说明 session 已过期
        drv.get_window_size()
        return _platform_from_driver(drv, state.get("os", "android"), state)
    except ImportError as e:
        raise RuntimeError('Mobile Python dependencies are missing; install the mobile extra') from e
    except Exception as e:
        if not quiet:
            log.warning("重连 session 失败（可能已过期，重新 start）: %s", e)
        return None


def stop(serial: str | None) -> bool:
    """退出 session 并清状态文件。"""
    state = load_state(serial)
    if state and state.get("kind") == "desktop":
        clear_state(serial)
        return True
    if state and state.get("kind") == "browser":
        return _browser_stop(serial, state)
    plat = attach(serial, quiet=True)
    if plat is not None:
        try:
            plat._driver.quit()
        except Exception as e:
            log.debug("quit 失败: %s", e)
    clear_state(serial)
    return True


# ---------------------------------------------------------------------------
# Browser（Selenium / Playwright CDP）session —— 与上面的 Appium session 对称：
# Chrome 由独立子进程常驻（--remote-debugging-port + 独立 user-data-dir），
# 各 `saygo device` 命令用 debuggerAddress 重连该 Chrome，做完即退（只杀本次
# 的 chromedriver，Chrome 存活供下次复用）。`stop` 才真正杀 Chrome 进程。
# ---------------------------------------------------------------------------

def _browser_port(serial: str | None) -> int:
    """按 serial 派生稳定端口（跨进程一致，故用 md5 而非内建 hash）。"""
    import hashlib
    h = int(hashlib.md5(_key(serial).encode()).hexdigest(), 16)
    return 9222 + (h % 300)


def _chrome_binary() -> str:
    import shutil
    env = os.environ.get("SAYGO_CHROME_BIN")
    if env and Path(env).exists():
        return env
    candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("chrome"),
    ]
    for c in candidates:
        if c and Path(c).exists():
            return c
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            bundled = pw.chromium.executable_path
        if Path(bundled).is_file():
            return bundled
    except ImportError:
        pass
    raise RuntimeError("找不到 Chrome；设 SAYGO_CHROME_BIN 或运行 python3 -m playwright install chromium")


def _devtools_ready(port: int, timeout: float = 15.0) -> bool:
    import time as _time
    import urllib.request
    end = _time.time() + timeout
    url = f"http://127.0.0.1:{port}/json/version"
    while _time.time() < end:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except Exception:
            _time.sleep(0.3)
    return False


def _browser_attach(serial: str | None, state: dict, quiet: bool = False) -> "object | None":
    try:
        return attach_browser(serial, state=state)
    except Exception as exc:
        if not quiet:
            log.warning("Browser attach failed: %s", exc)
        return None


def attach_desktop(state, *, serial=None, foreground=False, window_id=None):
    from . import create_platform
    kind = state["os"]
    if state.get("handoff"):
        from .desktop import DesktopHandoffRequired
        raise DesktopHandoffRequired(state["handoff"])
    options = {"app": state["app"], "launch": ""}
    options.update({key: state[key] for key in ("launch", "process_name", "process_id", "new_window", "new_window_args", "background", "input_binding") if key in state})
    if foreground:
        if kind != "windows":
            raise ValueError("Foreground runner requires Windows")
        options.update(background=True, foreground=True)
    if window_id is not None:
        options["window_id"] = window_id
    if serial is not None and kind == "windows":
        def save_binding(binding):
            current = load_state(serial)
            if not current or current.get("handoff") or current.get("disconnected"):
                return
            if current.get("app") != state.get("app") or current.get("process_id") != state.get("process_id"):
                return
            if binding:
                current["input_binding"] = binding
            else:
                current.pop("input_binding", None)
            save_state(serial, current)
        options["_binding_callback"] = save_binding
    cfg = {"win" if kind == "windows" else "mac": options}
    plat = create_platform(kind, cfg)
    try:
        plat.setup(cfg)
        return plat
    except Exception:
        plat.teardown()
        raise


def attach_browser(serial, *, state=None, backend=None, page_id=None, manage_pages=False):
    """Attach without launching/replacing Chrome; explicit backend overrides session default."""
    state = state if state is not None else load_state(serial)
    if not state or state.get("kind") != "browser":
        raise RuntimeError("Browser session missing; start the named browser session first")
    if state.get("handoff"):
        raise RuntimeError("Session waiting_for_human; resume before operating")
    if state.get("disconnected"):
        raise RuntimeError("Session disconnected; use device connect to reconnect")
    backend = backend or state.get("browser_backend", "selenium")
    if state.get("browser_backend") == "extension" and backend != "extension":
        raise ValueError("Extension sessions require backend=extension")
    if backend in {"playwright", "extension"}:
        from .browser_playwright import PlaywrightBrowserPlatform
        from .browser_extension import ExtensionBrowserPlatform

        def save_selection(target_id):
            current = load_state(serial) or state.copy()
            current["page_id"] = target_id
            save_state(serial, current)

        platform = ExtensionBrowserPlatform() if backend == "extension" else PlaywrightBrowserPlatform()
        endpoint = state["bridge_directory"] if backend == "extension" else state["debugger_address"]
        platform.connect(endpoint,
                         page_id=page_id if page_id is not None else state.get("page_id"),
                         selection_callback=save_selection, manage_pages=manage_pages)
        return platform
    if backend != "selenium":
        raise ValueError(f"Unknown browser backend: {backend}")
    if page_id is not None:
        raise ValueError("Explicit page IDs require the Playwright backend")
    platform = _selenium_browser_attach(serial, state)
    if platform is None:
        raise RuntimeError("Selenium could not attach to the browser session")
    return platform


def _selenium_browser_attach(serial: str | None, state: dict, quiet: bool = False) -> "object | None":
    try:
        from selenium import webdriver
        opts = webdriver.ChromeOptions()
        opts.debugger_address = state["debugger_address"]
        drv = webdriver.Chrome(options=opts)
    except Exception as e:
        if not quiet:
            log.warning("连接 browser session 失败（先跑 saygo device start --platform browser）: %s", e)
        return None
    from .browser import BrowserPlatform
    plat = BrowserPlatform()
    plat._driver = drv
    plat._viewport_width = int(state.get("viewport_width") or 414)
    plat._viewport_height = int(state.get("viewport_height") or 896)
    plat._is_remote = False
    plat._scale = None
    return plat


def _browser_start(serial: str | None, backend: str | None = None) -> "object":
    import subprocess
    port = _browser_port(serial)
    udd = str(STATE_DIR / f"{_key(serial)}-chrome")
    w = int(os.environ.get("SAYGO_BROWSER_W", 414))
    h = int(os.environ.get("SAYGO_BROWSER_H", 896))
    headless = os.environ.get("SAYGO_BROWSER_HEADLESS", "") not in ("", "0", "false", "False")

    if backend == "extension":
        return attach_browser(serial, backend="extension")
    if backend not in (None, "selenium", "playwright"):
        raise ValueError(f"Unknown browser backend: {backend}")
    # A live saved endpoint is authoritative. Never replace it because tab selection failed.
    st = load_state(serial)
    if st and st.get("kind") == "browser" and st.get("browser_backend") == "extension":
        return attach_browser(serial, backend="extension")
    if st and st.get("kind") == "browser" and _devtools_ready(st["port"], timeout=1):
        selected_backend = backend or st.get("browser_backend", "selenium")
        plat = attach_browser(serial, state=st, backend=selected_backend)
        current = load_state(serial) or st
        current["browser_backend"] = selected_backend
        save_state(serial, current)
        return plat
    if _devtools_ready(port, timeout=1):
        raise RuntimeError(f"Debugging port {port} is already occupied by another browser")

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    chrome = _chrome_binary()
    argv = [chrome, f"--remote-debugging-port={port}", f"--user-data-dir={udd}",
            f"--window-size={w},{h}", "--no-first-run", "--no-default-browser-check"]
    if headless:
        argv.insert(1, "--headless=new")
    argv.append("about:blank")
    proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            start_new_session=True)
    if not _devtools_ready(port, timeout=20):
        raise RuntimeError(f"Chrome devtools 未就绪 (port {port})")

    state = {"kind": "browser", "port": port, "pid": proc.pid,
             "debugger_address": f"127.0.0.1:{port}", "user_data_dir": udd,
             "viewport_width": w, "viewport_height": h, "headless": headless,
             "serial": serial or ""}
    state["browser_backend"] = backend or "selenium"
    # A new Chrome process must not inherit the old process's selected target ID.
    save_state(serial, state)
    plat = attach_browser(serial, state=state)
    state = load_state(serial) or state
    if state["browser_backend"] == "selenium":
        try:
            w, h = plat._driver.execute_script("return [window.innerWidth, window.innerHeight];")
            if w and h:
                plat._viewport_width, plat._viewport_height = int(w), int(h)
        except Exception:
            pass
    state["viewport_width"], state["viewport_height"] = plat.screen_size
    save_state(serial, state)
    log.info("browser session 就绪: serial=%s port=%s pid=%s", _key(serial), port, proc.pid)
    return plat


def _browser_stop(serial: str | None, state: dict) -> bool:
    import signal
    pid = state.get("pid")
    if pid:
        try:
            os.kill(int(pid), signal.SIGTERM)
        except Exception as e:
            log.debug("kill chrome 失败: %s", e)
    clear_state(serial)
    return True
