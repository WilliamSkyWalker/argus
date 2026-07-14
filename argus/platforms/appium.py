"""Appium platform — unified driver for Android (UiAutomator2) and iOS (XCUITest).

argus 的移动端统一走 Appium：一套 W3C WebDriver 协议同时驱动本地 Android/iOS
真机、模拟器，以及云真机农场（云上 iOS 唯一通路就是 Appium）。brain/agent 不感知
Appium —— 本类实现 base.Platform 接口，纯视觉：截图 + 坐标 tap（不取 UI 树），与
android.py / ios.py 行为对齐。

config["appium"]:
  os:          "android" | "ios"          （必填，选 driver）
  server_url:  Appium server 地址          （默认 http://127.0.0.1:4723）
  device:      udid / adb serial           （不填则第一台）
  bundle_id / package:  被测 App           （不填则附着当前前台，不启动/不重置）
  team_id:     iOS 签名 team（xcodeOrgId）  （iOS 首次装 WDA 需要）
  signing_id:  iOS 签名身份                 （默认 "Apple Development"）
  wda_bundle_id: 自定义 WDA bundleid        （可选，配合 team 自动签名）
  caps:        额外 desired capabilities dict（透传/覆盖，逃生舱）
"""

import io
import os
import re
import time

from PIL import Image

from ..grid import draw_coordinate_grid, img_to_png_bytes
from ..logger import get_logger
from .base import Platform

log = get_logger("appium")

_ANDROID_PROMPT_SEGMENT = """你正在操作一个 Android 设备来执行测试用例。

可用操作类型：
- tap: {"x_pct": 0-100, "y_pct": 0-100}  — 点击(百分比坐标,相对截图宽/高)
- swipe: {"x1_pct": .., "y1_pct": .., "x2_pct": .., "y2_pct": ..}  — 滑动(百分比)
- swipe_up / swipe_down  — 上/下滚动
- input: {"text": "string"}  — 输入文字
- press_key: {"key": "enter|delete|tab|space|escape|back|home|recent"}
- open_app: {"package": "string"}  — 打开 App（包名）
- long_press: {"x_pct": .., "y_pct": .., "duration": float}
- wait: {"seconds": int}
- done: {"result": "pass|fail", "reason": "string"}

注意：
- 原生 UI 元素优先用 UI 树里的 bounds 坐标点中心；网页/自绘内容按视觉估算坐标
- Android 三个导航键：back / home / recent"""

_IOS_PROMPT_SEGMENT = """你正在操作一个 iOS 设备来执行测试用例。

可用操作类型：
- tap: {"x_pct": 0-100, "y_pct": 0-100}  — 点击(百分比坐标,相对截图宽/高)
- swipe: {"x1_pct": .., "y1_pct": .., "x2_pct": .., "y2_pct": ..}  — 滑动(百分比)
- swipe_up / swipe_down  — 上/下滚动
- input: {"text": "string"}  — 输入文字（需先点中输入框聚焦）
- press_key: {"key": "enter|delete|home"}
- open_app: {"bundle_id": "string"}  — 打开 App
- wait: {"seconds": int}
- done: {"result": "pass|fail", "reason": "string"}

注意：
- 坐标用百分比（x_pct/y_pct 相对截图宽/高，0-100），分辨率/Retina 无关，框架自动换算
- iOS 无返回键，返回靠点左上角返回箭头或从左缘右滑"""


class AppiumPlatform(Platform):
    """Unified Appium driver for Android + iOS."""

    def __init__(self):
        self._driver = None
        self._server = None           # AppiumServerManager（自动起/复用 server）
        self._os = "android"          # android | ios
        self._screen_width = 0
        self._screen_height = 0
        self._last_shot_size: tuple[int, int] | None = None

    # --- Lifecycle ---

    def setup(self, config: dict) -> None:
        from appium import webdriver
        from appium.options.android import UiAutomator2Options
        from appium.options.ios import XCUITestOptions

        from .appium_server import AppiumServerManager

        cfg = config.get("appium", {}) or {}
        self._os = (cfg.get("os") or config.get("platform") or "android").lower()
        server_url = cfg.get("server_url") or os.environ.get(
            "APPIUM_SERVER_URL", "http://127.0.0.1:4723")
        device = cfg.get("device") or ""

        # argus 自己确保 Appium server 就绪（已在跑则复用，否则拉起并在 teardown 关闭）
        self._server = AppiumServerManager(server_url, cfg)
        server_url = self._server.ensure_running()

        if self._os == "ios":
            opts = XCUITestOptions()
            opts.automation_name = "XCUITest"
            if device:
                opts.udid = device
            bundle = cfg.get("bundle_id") or config.get("ios", {}).get("bundle_id")
            if bundle:
                opts.bundle_id = bundle
            # 附着当前状态，不重装/不清数据
            opts.set_capability("noReset", True)
            # WDA 自动签名（首次装 WDA 需要 team）：给了 team 就让 Appium+xcodebuild
            # 自动建证书/profile/注册设备（省掉手工 provisioning）。
            if cfg.get("team_id"):
                opts.set_capability("xcodeOrgId", cfg["team_id"])
                opts.set_capability("xcodeSigningId", cfg.get("signing_id", "Apple Development"))
                opts.set_capability("allowProvisioningUpdates", True)
                opts.set_capability("allowProvisioningDeviceRegistration", True)
            if cfg.get("wda_bundle_id"):
                opts.set_capability("updatedWDABundleId", cfg["wda_bundle_id"])
            opts.set_capability("shouldTerminateApp", False)
        else:
            opts = UiAutomator2Options()
            opts.automation_name = "UiAutomator2"
            if device:
                opts.udid = device
            pkg = cfg.get("package") or config.get("android", {}).get("package")
            if pkg:
                opts.set_capability("appPackage", pkg)
            # 附着当前前台，不启动/不重置
            opts.set_capability("noReset", True)
            opts.set_capability("autoLaunch", False)
            opts.set_capability("skipDeviceInitialization", True)
            opts.set_capability("skipServerInstallation", False)
            # Flutter 文字输入：Flutter 的输入框不是原生 EditText，ACTION_SET_TEXT
            # 无效；必须走真 IME 提交。开 UnicodeIME（io.appium.settings 提供，
            # 云农场预装）才能把文字打进 Flutter 字段，且支持 CJK。
            opts.set_capability("unicodeKeyboard", True)
            opts.set_capability("resetKeyboard", True)

        # 通用：命令超时给足，避免长决策间隔掉 session
        opts.set_capability("newCommandTimeout", 600)
        # 逃生舱：透传/覆盖任意 caps
        for k, v in (cfg.get("caps") or {}).items():
            opts.set_capability(k, v)

        log.info("连接 Appium server %s (os=%s, device=%s)...", server_url, self._os, device or "auto")
        self._driver = webdriver.Remote(server_url, options=opts)
        self._detect_screen_size()
        log.info("Appium session 就绪: %s %dx%d", self._os, self._screen_width, self._screen_height)

    def teardown(self) -> None:
        if self._driver is not None:
            try:
                self._driver.quit()
            except Exception as e:
                log.debug("driver.quit 失败: %s", e)
            self._driver = None
        if self._server is not None:
            self._server.stop()   # 仅关闭 argus 自己起的 server
            self._server = None

    def _detect_screen_size(self) -> None:
        """逻辑屏幕尺寸（点），来自 driver.get_window_size()。"""
        try:
            size = self._driver.get_window_size()
            self._screen_width = int(size["width"])
            self._screen_height = int(size["height"])
        except Exception as e:
            log.warning("get_window_size 失败，回退 0: %s", e)
            self._screen_width, self._screen_height = 0, 0

    # --- Observation ---

    def _note_screenshot_size(self, width: int, height: int) -> None:
        """记录截图像素尺寸（供 scale 换算），并检测横竖屏旋转（同 android.py）。"""
        self._last_shot_size = (width, height)
        w, h = self._screen_width, self._screen_height
        if w and h and w != h and width != height and (width > height) != (w > h):
            log.info("检测到屏幕旋转: 缓存 %dx%d → %dx%d", w, h, h, w)
            self._screen_width, self._screen_height = h, w

    def screenshot_raw(self) -> bytes:
        """无 grid 截图（LLM 输入 + 报告都用这个）。"""
        raw = self._driver.get_screenshot_as_png()
        try:
            img = Image.open(io.BytesIO(raw))
            self._note_screenshot_size(img.width, img.height)
        except Exception as e:
            log.debug("截图尺寸读取失败: %s", e)
        return raw

    def screenshot_png(self) -> bytes:
        """带 grid 截图（legacy）。"""
        raw = self._driver.get_screenshot_as_png()
        img = Image.open(io.BytesIO(raw))
        self._note_screenshot_size(img.width, img.height)
        scale = img.width / self._screen_width if self._screen_width else 1.0
        img = draw_coordinate_grid(img, scale, self._screen_width, self._screen_height)
        return img_to_png_bytes(img)

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._screen_width, self._screen_height)

    @property
    def scale(self) -> float:
        """截图 px / 逻辑点 比例。iOS 通常 2 或 3（Retina）；Android 多为 1。"""
        if self._last_shot_size and self._screen_width:
            return self._last_shot_size[0] / self._screen_width
        return 1.0

    # --- Actions ---

    def _w3c_tap(self, x: int, y: int, duration_ms: int = 0) -> None:
        """W3C pointer tap，跨 Android/iOS 通用。"""
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.actions import interaction

        pointer = PointerInput(interaction.POINTER_TOUCH, "touch")
        ab = ActionBuilder(self._driver, mouse=pointer)
        ab.pointer_action.move_to_location(x, y).pointer_down()
        if duration_ms:
            ab.pointer_action.pause(duration_ms / 1000.0)
        ab.pointer_action.pointer_up()
        ab.perform()

    def _w3c_swipe(self, x1: int, y1: int, x2: int, y2: int, duration_ms: int = 500) -> None:
        from selenium.webdriver.common.actions.action_builder import ActionBuilder
        from selenium.webdriver.common.actions.pointer_input import PointerInput
        from selenium.webdriver.common.actions import interaction

        pointer = PointerInput(interaction.POINTER_TOUCH, "touch")
        ab = ActionBuilder(self._driver, mouse=pointer)
        ab.pointer_action.move_to_location(x1, y1).pointer_down()
        ab.pointer_action.pause(0.1)
        ab.pointer_action.move_to_location(x2, y2)
        ab.pointer_action.pause(duration_ms / 1000.0)
        ab.pointer_action.pointer_up()
        ab.perform()

    def tap(self, x: int, y: int) -> None:
        self._w3c_tap(x, y)

    def long_press(self, x: int, y: int, duration: float = 1.5) -> None:
        self._w3c_tap(x, y, duration_ms=int(duration * 1000))

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._w3c_swipe(x1, y1, x2, y2)

    def scroll_up(self) -> None:
        cx, h = self._screen_width // 2, self._screen_height
        self._w3c_swipe(cx, h * 3 // 4, cx, h // 4)

    def scroll_down(self) -> None:
        cx, h = self._screen_width // 2, self._screen_height
        self._w3c_swipe(cx, h // 4, cx, h * 3 // 4)

    def input_text(self, text: str) -> None:
        """向当前聚焦的输入框写文字。

        Android：走 `mobile: type` —— 经 UnicodeIME（unicodeKeyboard cap 已激活，
        io.appium.settings 提供）真 IME 提交。IME 对「聚焦的输入框」提交文字，
        **不管是原生 EditText 还是 Flutter 自绘都通吃**，且支持 CJK；比只对原生有效的
        ACTION_SET_TEXT 更普适，故原生/Flutter 不分叉。
        iOS：聚焦元素 send_keys / mobile: keys。
        """
        if not text:
            return
        if self._os == "android":
            try:
                self._driver.execute_script("mobile: type", {"text": text})
                return
            except Exception as e:
                log.debug("mobile: type 失败，试 active_element: %s", e)
        # 兜底 / iOS：聚焦元素 send_keys
        try:
            self._driver.switch_to.active_element.send_keys(text)
            return
        except Exception as e:
            log.debug("active_element send_keys 失败: %s", e)
        if self._os == "ios":
            try:
                self._driver.execute_script("mobile: keys", {"keys": list(text)})
                return
            except Exception as e:
                log.warning("iOS mobile: keys 失败: %s", e)
        log.warning("input_text 失败：无法写入（先 tap 聚焦输入框再 input）")

    def press_key(self, key: str) -> None:
        if self._os == "android":
            key_map = {
                "enter": 66, "delete": 67, "tab": 61, "space": 62,
                "escape": 111, "back": 4, "home": 3, "recent": 187,
            }
            code = key_map.get(key)
            if code is None:
                cand = str(key).strip()
                if cand.isdigit():
                    code = int(cand)
                else:
                    log.warning("press_key 不识别: %r", key)
                    return
            self._driver.press_keycode(code)
        else:
            # iOS：硬件键有限
            if key in ("home",):
                try:
                    self._driver.execute_script("mobile: pressButton", {"name": "home"})
                except Exception as e:
                    log.warning("iOS home 失败: %s", e)
            elif key == "enter":
                self.input_text("\n")
            elif key == "delete":
                try:
                    from appium.webdriver.common.appiumby import AppiumBy
                    self._driver.find_element(AppiumBy.XPATH, '//*[@focused="true"]').send_keys("\b")
                except Exception as e:
                    log.debug("iOS delete 失败: %s", e)
            else:
                log.warning("iOS 不支持按键: %r", key)

    def open_target(self, target: str) -> None:
        """Android=包名启动，iOS=bundle_id 启动。"""
        if not target:
            return
        try:
            if self._os == "android":
                if not re.fullmatch(r"[\w.]+", target):
                    log.warning("非法包名，忽略: %r", target)
                    return
                self._driver.activate_app(target)
            else:
                self._driver.activate_app(target)
        except Exception as e:
            log.warning("open_target(%s) 失败: %s", target, e)

    def is_ime_visible(self) -> bool:
        if self._os != "android":
            return False
        try:
            return bool(self._driver.is_keyboard_shown())
        except Exception:
            return False

    def _handle_platform_action(self, action: dict) -> None:
        action_type = action["type"]
        if action_type == "long_press":
            w, h = self.screen_size
            x = max(0, min(int(action["x"]), w - 1))
            y = max(0, min(int(action["y"]), h - 1))
            self.long_press(x, y, action.get("duration", 1.5))
        elif action_type in ("back", "home", "recent"):
            self.press_key(action_type)
        else:
            raise ValueError(f"Unknown action type for Appium: {action_type}")

    # --- Platform identity ---

    @property
    def platform_name(self) -> str:
        return self._os  # brain/report 仍看到 "android" / "ios"

    def get_system_prompt_segment(self) -> str:
        return _IOS_PROMPT_SEGMENT if self._os == "ios" else _ANDROID_PROMPT_SEGMENT
