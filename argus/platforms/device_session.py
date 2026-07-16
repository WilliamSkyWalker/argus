"""跨进程 device session —— 让 `argus device` CLI 子命令（每次一个进程）复用同一个
常驻 Appium session，不必每次重建（重建要 2-4s 且丢前台状态）。

原理：
- Appium **server** 由 AppiumServerManager 常驻（起了就一直在）。
- Appium **session** 在 newCommandTimeout(600s) 内存活于 server 上。
- `argus device start` 建 session 后，把 {server_url, session_id, os, 屏幕尺寸} 落到
  状态文件 `~/.argus/device-sessions/<serial>.json`。
- 后续 `argus device tap/screenshot/...` 读状态文件，用 session_id **重连**已有 session
  （不建新 session），做完即退进程，session 仍留在 server 上供下次复用。

重连用的是 selenium 已知手法：临时拦截 newSession 命令，让 webdriver.Remote 直接认领
已有 session_id，而不真正开新 session。

任何能跑 shell 的 agent 都可调 `argus device *`，故这是 argus 对外的通用设备驱动接口。
"""

import json
import os
from pathlib import Path

from ..logger import get_logger

log = get_logger("device.session")

STATE_DIR = Path(os.environ.get("ARGUS_HOME_DIR", Path.home() / ".argus")) / "device-sessions"


def _key(serial: str | None) -> str:
    return serial or "default"


def _state_path(serial: str | None) -> Path:
    return STATE_DIR / f"{_key(serial)}.json"


def save_state(serial: str | None, data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _state_path(serial).write_text(json.dumps(data, ensure_ascii=False, indent=2))


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


def start(serial: str | None, os_name: str = "android") -> "object":
    """新建一个 device session 并落状态文件，返回 AppiumPlatform。已有存活 session 则复用。"""
    # 已有状态且能连通 → 直接复用，不重复建
    existing = attach(serial, quiet=True)
    if existing is not None:
        return existing
    from .appium import AppiumPlatform
    plat = AppiumPlatform()
    plat.setup({"appium": {"os": os_name, "device": serial or ""}})
    save_state(serial, {
        "server_url": plat._server_url,
        "session_id": plat._driver.session_id,
        "os": plat._os,
        "screen_width": plat._screen_width,
        "screen_height": plat._screen_height,
        "serial": serial or "",
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
            log.warning("无 device session 状态文件（先跑 argus device start）: %s", _key(serial))
        return None
    try:
        drv = _attach_driver(state["server_url"], state["session_id"], state.get("os", "android"))
        # 探活：读一下 window size，失败说明 session 已过期
        drv.get_window_size()
        return _platform_from_driver(drv, state.get("os", "android"), state)
    except Exception as e:
        if not quiet:
            log.warning("重连 session 失败（可能已过期，重新 start）: %s", e)
        clear_state(serial)
        return None


def stop(serial: str | None) -> bool:
    """退出 session 并清状态文件。"""
    plat = attach(serial, quiet=True)
    if plat is not None:
        try:
            plat._driver.quit()
        except Exception as e:
            log.debug("quit 失败: %s", e)
    clear_state(serial)
    return True
