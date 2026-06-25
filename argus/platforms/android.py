"""Android platform — uses adb for screenshot + touch control (real device & emulator)."""

import io
import os
import re
import shutil
import subprocess
import tempfile
import time

from PIL import Image

import uiautomator2 as u2

from ..grid import draw_coordinate_grid, img_to_png_bytes
from ..logger import get_logger
from .base import Platform

log = get_logger("android")

# tap 吸附到 clickable 节点：执行 tap 前，若 UI tree 中存在包含该坐标的
# clickable 节点，则把落点修正到该节点 bounds 中心。对没有可用 UI tree 的
# app（如 Flutter，树里无 clickable 节点）天然 no-op —— 永远找不到包含节点，
# 原样 tap，行为与改动前逐字节相同。可用 TAP_SNAP_TO_CLICKABLE=0 关闭。
_TAP_SNAP_TO_CLICKABLE = os.environ.get(
    "TAP_SNAP_TO_CLICKABLE", "1").strip().lower() not in ("0", "false", "no", "off")
# 吸附安全阈值：包含坐标的最小 clickable 节点若面积大于屏幕的此比例，
# 视为全屏/大容器，放弃吸附（避免把 tap 拽到无意义的容器中心）。
_TAP_SNAP_MAX_AREA_RATIO = 0.55

ANDROID_PROMPT_SEGMENT = """你正在操作一个 Android 设备来执行测试用例。

可用操作类型：
- tap: {"x": int, "y": int}  — 点击屏幕坐标
- swipe: {"x1": int, "y1": int, "x2": int, "y2": int}  — 滑动
- swipe_up  — 向上滚动
- swipe_down  — 向下滚动
- input: {"text": "string"}  — 输入文字
- press_key: {"key": "enter|delete|tab|space|escape|back|home|recent"}  — 按键
- open_app: {"package": "string"}  — 打开 App（包名）
- long_press: {"x": int, "y": int, "duration": float}  — 长按
- wait: {"seconds": int}  — 等待（1-5秒）
- done: {"result": "pass|fail", "reason": "string"}  — 报告测试结果

常用 App 包名：
- Chrome: com.android.chrome
- 设置: com.android.settings
- 拨号: com.android.dialer
- 相机: com.android.camera2
- 文件: com.google.android.apps.nbu.files
- 日历: com.google.android.calendar
- 时钟: com.google.android.deskclock

注意：
- 对于原生 UI 元素，优先使用 UI 元素树中的坐标信息（bounds 属性），点击元素中心
- 对于网页等 UI 树无法描述的内容，通过视觉定位直接估算坐标
- Android 有三个导航键：back（返回）、home（主屏幕）、recent（最近任务）"""


class AndroidPlatform(Platform):
    """Android platform using adb for control. Works with both real devices and emulators."""

    def __init__(self):
        self._adb_path = self._find_adb()
        self._serial = None
        self._screen_width = 0
        self._screen_height = 0
        self._density = 1.0
        self._u2: u2.Device | None = None
        # 最近一次 dump 的原始 UI tree XML，供 tap 吸附复用（不额外 dump）
        self._last_raw_tree: str = ""

    def _find_adb(self) -> str:
        """Locate adb executable. Check PATH first, then common Android SDK locations."""
        adb = shutil.which("adb")
        if adb:
            return adb
        # Try common Android SDK paths (macOS)
        common_paths = [
            os.path.expanduser("~/Library/Android/sdk/platform-tools/adb"),
            "/opt/android-sdk/platform-tools/adb",
            "/usr/local/opt/android-platform-tools/bin/adb",
        ]
        for path in common_paths:
            if os.path.isfile(path):
                return path
        raise RuntimeError(
            "adb not found. Install Android SDK platform-tools:\n"
            "  brew install android-platform-tools  (macOS)\n"
            "  sudo apt install android-tools-adb   (Linux)"
        )

    def _adb(self, *args: str, input_data: str | None = None) -> str:
        """Run an adb command and return stdout."""
        cmd = [self._adb_path]
        if self._serial:
            cmd += ["-s", self._serial]
        cmd += list(args)
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            input=input_data, timeout=30,
        )
        if result.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    def _adb_bytes(self, *args: str) -> bytes:
        """Run an adb command and return raw stdout bytes."""
        cmd = [self._adb_path]
        if self._serial:
            cmd += ["-s", self._serial]
        cmd += list(args)
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode != 0:
            raise RuntimeError(f"adb {' '.join(args)} failed: {result.stderr.decode().strip()}")
        return result.stdout

    def setup(self, config: dict) -> None:
        # adb availability checked during __init__ via _find_adb()
        try:
            subprocess.run([self._adb_path, "version"], capture_output=True, check=True, timeout=5)
        except (FileNotFoundError, RuntimeError) as e:
            raise RuntimeError(f"adb check failed: {e}")

        android_cfg = config.get("android", {})
        self._serial = android_cfg.get("serial", "") or None

        # If no serial specified, use the first connected device
        if not self._serial:
            self._serial = self._find_device()

        # Get screen dimensions
        self._detect_screen_size()
        # Emulator serials start with "emulator-" or are localhost-based
        self._is_emulator: bool = bool(
            self._serial and (
                self._serial.startswith("emulator-")
                or self._serial.startswith("localhost:")
            )
        )
        log.info("Android 设备已连接: serial=%s, screen=%dx%d, emulator=%s",
                 self._serial, self._screen_width, self._screen_height, self._is_emulator)

        # Bring up uiautomator2 — first run auto-pushes uiautomator-server.apk
        # to the device. Text input goes through ACTION_SET_TEXT via the
        # accessibility/instrumentation path (not KeyEvents), so it isn't
        # subject to IME composing (Gboard pinyin etc).
        log.info("uiautomator2 连接中（首次会 push server apk）...")
        self._u2 = u2.connect(self._serial)
        log.info("uiautomator2 ready: %s", self._u2.info.get("productName"))

    def _find_device(self) -> str:
        """Find a connected device/emulator."""
        output = self._adb("devices")
        devices = []
        for line in output.strip().splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 2 and parts[1] == "device":
                devices.append(parts[0])

        if not devices:
            raise RuntimeError(
                "No Android device connected.\n"
                "  Real device: enable USB debugging and connect via USB/WiFi\n"
                "  Emulator: start one via Android Studio or `emulator -avd <name>`"
            )

        # Reset serial so _adb uses it
        serial = devices[0]
        if len(devices) > 1:
            log.warning("多个设备已连接，使用第一个: %s", serial)
        return serial

    def _detect_screen_size(self) -> None:
        """Detect physical screen size and density."""
        # Physical size: e.g. "Physical size: 1080x2340"
        output = self._adb("shell", "wm", "size")
        match = re.search(r"(\d+)x(\d+)", output)
        if match:
            self._screen_width = int(match.group(1))
            self._screen_height = int(match.group(2))
        else:
            self._screen_width, self._screen_height = 1080, 1920

        # Density for logical coordinate conversion
        output = self._adb("shell", "wm", "density")
        match = re.search(r"(\d+)", output)
        if match:
            self._density = int(match.group(1)) / 160.0  # 160 dpi = 1x

    def teardown(self) -> None:
        pass

    # --- Observation ---

    def screenshot_png(self) -> bytes:
        """Screenshot WITH coordinate grid overlay (legacy callers)."""
        raw_bytes = self._adb_bytes("exec-out", "screencap", "-p")
        img = Image.open(io.BytesIO(raw_bytes))
        scale = img.width / self._screen_width
        img = draw_coordinate_grid(img, scale, self._screen_width, self._screen_height)
        return img_to_png_bytes(img)

    def screenshot_raw(self) -> bytes:
        """Screenshot WITHOUT grid overlay. Matches web / iOS behavior.

        Used by agent.py for both the LLM input AND the report screenshots
        — keeping the LLM-seen image identical to what we render in the HTML
        report, so debugging matches what the model actually saw.
        """
        return self._adb_bytes("exec-out", "screencap", "-p")

    def get_ui_tree(self) -> str:
        """Dump UI hierarchy. Three-tier fallback:

          1. uiautomator2 ``dump_hierarchy()`` — bypasses adb shell + file IO
             by talking directly to the on-device uiautomator-server HTTP API
             we already connected to in setup(). Works on Android 12+ Pixel
             devices where /sdcard write perms are revoked.
          2. ``adb shell uiautomator dump /data/local/tmp/...`` — /data/local/tmp
             is the shell user's home with no scoped-storage restrictions.
          3. ``adb shell uiautomator dump /sdcard/...`` — original path, kept
             for older devices / emulators where it still works.
        """
        # Tier 1: uiautomator2
        if self._u2 is not None:
            try:
                xml = self._u2.dump_hierarchy()
                if xml:
                    self._last_raw_tree = xml
                    return xml
            except Exception as e:
                log.debug("u2 dump_hierarchy 失败，降级 adb dump: %s", e)

        # Tier 2 & 3: adb shell uiautomator dump to writable paths
        for path in ("/data/local/tmp/window_dump.xml", "/sdcard/window_dump.xml"):
            try:
                self._adb("shell", "uiautomator", "dump", path)
                xml = self._adb("shell", "cat", path)
                if xml:
                    self._last_raw_tree = xml
                    return xml
            except Exception as e:
                log.debug("adb uiautomator dump %s 失败: %s", path, e)

        log.warning("UI tree dump 三层降级全部失败")
        return "(UI tree unavailable)"

    @property
    def screen_size(self) -> tuple[int, int]:
        return (self._screen_width, self._screen_height)

    # --- Actions ---

    def _snap_to_clickable(self, x: int, y: int) -> tuple[int, int]:
        """把 tap 落点吸附到包含 (x,y) 的、面积最小的 clickable 节点中心。

        动机：原生 Android app 里 brain 常把坐标估在文字标签上、或无文字
        ImageView 旁边略偏 —— 这些落点其实落在「可点击父容器 / clickable
        ImageView」的 bounds 内。吸附到该节点中心可显著提升命中率。

        对 Flutter 等无 UI tree 的 app 天然 no-op：``self._last_raw_tree``
        里没有 clickable 节点 → 找不到包含节点 → 返回原坐标。
        """
        if not _TAP_SNAP_TO_CLICKABLE or not self._last_raw_tree:
            return x, y
        screen_area = max(1, self._screen_width * self._screen_height)
        best = None  # (area, cx, cy)
        for m in re.finditer(r'<node\s+([^>]*?)/?>', self._last_raw_tree):
            attrs = m.group(1)
            if 'clickable="true"' not in attrs:
                continue
            b = re.search(r'bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"', attrs)
            if not b:
                continue
            x1, y1, x2, y2 = map(int, b.groups())
            if x2 <= x1 or y2 <= y1:
                continue
            if not (x1 <= x <= x2 and y1 <= y <= y2):
                continue
            area = (x2 - x1) * (y2 - y1)
            # 跳过全屏/大容器，避免把 tap 拽到无意义的中心
            if area > screen_area * _TAP_SNAP_MAX_AREA_RATIO:
                continue
            if best is None or area < best[0]:
                best = (area, (x1 + x2) // 2, (y1 + y2) // 2)
        if best is None:
            return x, y
        _, cx, cy = best
        if (cx, cy) != (x, y):
            log.info("tap 吸附到 clickable 节点: (%d,%d) → (%d,%d)", x, y, cx, cy)
        return cx, cy

    def tap(self, x: int, y: int) -> None:
        x, y = self._snap_to_clickable(x, y)
        self._adb("shell", "input", "tap", str(x), str(y))

    def input_text(self, text: str) -> None:
        """Type text into the currently focused EditText.

        Uses uiautomator2's set_text, which goes through
        AccessibilityNodeInfo.ACTION_SET_TEXT — directly writing the field's
        text buffer via the instrumentation/accessibility path. This bypasses
        the IME entirely, so Gboard pinyin / Sogou / etc. cannot intercept
        characters, and CJK and ASCII go through the same code path.

        Two cases:
          - Regular EditText: one set_text call replaces the full text.
          - Flutter Pinput OTP (each slot is a separate EditText with
            maxLength=1): set_text 1 char at a time, the widget auto-advances
            focus to the next slot after each keystroke.
        """
        if not text:
            return
        if self._focused_is_otp_slot():
            for ch in text:
                self._u2(focused=True).set_text(ch)
                time.sleep(0.12)  # let Pinput advance focus to next slot
        else:
            self._u2(focused=True).set_text(text)

    def _focused_is_otp_slot(self) -> bool:
        """True if the focused EditText is a single-char OTP slot.

        Detection: scan UI tree for the EditText with focused="true". If its
        bounding box width is < 25% of screen width, treat as OTP slot —
        Flutter Pinput / similar PIN widgets render each digit in a narrow
        EditText (typically 60-120 px wide on 1080 px screens). Regular
        text fields are at least 50% screen width.
        """
        try:
            import re as _re
            ui = self.get_ui_tree() or ""
            sw, _sh = self.screen_size
            # Find the focused EditText block and its bounds
            for m in _re.finditer(
                r'class="(?:android\.widget\.)?EditText"[^>]*?focused="true"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"',
                ui,
            ):
                x1, _y1, x2, _y2 = (int(g) for g in m.groups())
                width = x2 - x1
                if width < sw * 0.25:
                    return True
            # Also match when attribute order is reversed (bounds before focused)
            for m in _re.finditer(
                r'class="(?:android\.widget\.)?EditText"[^>]*?bounds="\[(\d+),(\d+)\]\[(\d+),(\d+)\]"[^>]*?focused="true"',
                ui,
            ):
                x1, _y1, x2, _y2 = (int(g) for g in m.groups())
                if (x2 - x1) < sw * 0.25:
                    return True
        except Exception:
            pass
        return False

    def press_key(self, key: str) -> None:
        key_map = {
            "enter": "66",     # KEYCODE_ENTER
            "delete": "67",    # KEYCODE_DEL
            "tab": "61",       # KEYCODE_TAB
            "space": "62",     # KEYCODE_SPACE
            "escape": "111",   # KEYCODE_ESCAPE
            "back": "4",       # KEYCODE_BACK
            "home": "3",       # KEYCODE_HOME
            "recent": "187",   # KEYCODE_APP_SWITCH
        }
        code = key_map.get(key, key)
        self._adb("shell", "input", "keyevent", code)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._adb("shell", "input", "swipe",
                   str(x1), str(y1), str(x2), str(y2), "500")

    def scroll_up(self) -> None:
        cx = self._screen_width // 2
        h = self._screen_height
        self.swipe(cx, h * 3 // 4, cx, h // 4)

    def scroll_down(self) -> None:
        cx = self._screen_width // 2
        h = self._screen_height
        self.swipe(cx, h // 4, cx, h * 3 // 4)

    def open_target(self, target: str) -> None:
        """Open an app by package name."""
        # Use monkey to launch the app's main activity
        self._adb("shell", "monkey", "-p", target,
                   "-c", "android.intent.category.LAUNCHER", "1")

    def is_ime_visible(self) -> bool:
        # uiautomator dump can't see the IME — it's a separate system window.
        # `dumpsys input_method` reports the authoritative show state.
        try:
            out = subprocess.run(
                self._adb_cmd("shell", "dumpsys", "input_method"),
                capture_output=True, text=True, timeout=3,
            ).stdout
        except Exception:
            return False
        for line in out.splitlines():
            if "mInputShown=" in line:
                return "mInputShown=true" in line
        return False

    def _adb_cmd(self, *args: str) -> list[str]:
        cmd = [self._adb_path]
        if self._serial:
            cmd.extend(["-s", self._serial])
        cmd.extend(args)
        return cmd

    # --- Android-specific actions ---

    def long_press(self, x: int, y: int, duration: float = 1.5) -> None:
        """Long press via swipe with zero movement."""
        ms = int(duration * 1000)
        self._adb("shell", "input", "swipe", str(x), str(y), str(x), str(y), str(ms))

    def _handle_platform_action(self, action: dict) -> None:
        action_type = action["type"]
        if action_type == "long_press":
            w, h = self.screen_size
            x = max(0, min(int(action["x"]), w))
            y = max(0, min(int(action["y"]), h))
            self.long_press(x, y, action.get("duration", 1.5))
        elif action_type in ("back", "home", "recent"):
            self.press_key(action_type)
        else:
            raise ValueError(f"Unknown action type for Android: {action_type}")

    # --- Platform identity ---

    @property
    def platform_name(self) -> str:
        return "android"

    def get_system_prompt_segment(self) -> str:
        return ANDROID_PROMPT_SEGMENT
