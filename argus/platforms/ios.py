"""iOS platform — wraps hands.py (idb). Supports both simulator and real device."""

from ..logger import get_logger
from .base import Platform

log = get_logger("ios")

IOS_PROMPT_SEGMENT = """你正在操作一个 iOS 设备。

可用操作类型：
- tap: {"x": int, "y": int}  — 点击屏幕坐标
- swipe: {"x1": int, "y1": int, "x2": int, "y2": int}  — 滑动
- swipe_up  — 向上滚动
- swipe_down  — 向下滚动
- input: {"text": "string"}  — 输入文字
- press_key: {"key": "enter|delete|tab|space|escape"}  — 按键
- open_app: {"bundle_id": "string"}  — 打开App
- long_press: {"x": int, "y": int, "duration": float}  — 长按
- wait: {"seconds": int}  — 等待（1-5秒）
- done: {"result": "pass|fail", "reason": "string"}  — 报告测试结果

常用 App bundle ID：
- Safari: com.apple.mobilesafari
- 设置: com.apple.Preferences
- 地图: com.apple.Maps
- 照片: com.apple.Photos
- 日历: com.apple.mobilecal
- 通讯录: com.apple.MobileAddressBook
- 备忘录: com.apple.mobilenotes
- 时钟: com.apple.mobiletimer

注意：
- 对于原生 UI 元素，优先使用 UI 元素树中的坐标信息（x, y, width, height），点击元素中心 (x + width/2, y + height/2)
- 对于网页内容（UI 树中看不到的元素），通过视觉定位直接估算坐标"""


class IOSPlatform(Platform):
    """iOS platform using idb for control. Works with both simulator and real device."""

    def __init__(self):
        self._hands = None
        self._udid = None
        self._is_real_device = False

    def setup(self, config: dict) -> None:
        from ..hands import Hands

        sim_cfg = config.get("simulator", {})
        udid = sim_cfg.get("udid", "")
        device_mode = sim_cfg.get("device_mode", "auto")  # auto | simulator | device

        if not udid:
            udid = self._discover_device(device_mode)

        self._udid = udid
        self._hands = Hands(udid)

        # Detect if it's a real device
        if device_mode == "device":
            self._is_real_device = True
        elif device_mode == "auto":
            self._is_real_device = self._detect_real_device(udid)

        device_type = "真机" if self._is_real_device else "模拟器"
        log.info("iOS %s 已连接: udid=%s", device_type, udid)

    def _discover_device(self, mode: str) -> str:
        """Find a device UDID based on mode."""
        if mode in ("auto", "simulator"):
            # Try simulator first
            try:
                from ..simulator import list_devices
                booted = [d for d in list_devices() if d.state == "Booted"]
                if booted:
                    return booted[0].udid
            except Exception:
                pass

        if mode in ("auto", "device"):
            # Try idb to find connected real devices
            try:
                import json
                import subprocess
                result = subprocess.run(
                    ["idb", "list-targets", "--json"],
                    capture_output=True, text=True, timeout=10,
                )
                if result.returncode == 0:
                    for line in result.stdout.strip().splitlines():
                        target = json.loads(line)
                        # Real devices have target_type "device" (not "simulator")
                        if target.get("type") == "device" and target.get("state") == "Booted":
                            return target["udid"]
                        # If mode is auto, also accept simulators from idb
                        if mode == "auto" and target.get("state") == "Booted":
                            return target["udid"]
            except Exception:
                pass

        raise RuntimeError(
            "No iOS device found.\n"
            "  Simulator: run `argus setup` first\n"
            "  Real device: connect via USB and ensure idb_companion is running"
        )

    def _detect_real_device(self, udid: str) -> bool:
        """Check if UDID belongs to a real device (not simulator)."""
        try:
            from ..simulator import list_devices
            sim_udids = {d.udid for d in list_devices()}
            return udid not in sim_udids
        except Exception:
            return False

    def teardown(self) -> None:
        pass

    # --- Observation ---

    def screenshot_png(self) -> bytes:
        return self._hands.screenshot_png()

    def screenshot_raw(self) -> bytes:
        from PIL import Image

        from ..grid import img_to_png_bytes
        img = self._hands._take_raw_screenshot()
        w, h = self.screen_size
        # 旋转检测：截图横竖与缓存的窗口尺寸不一致时交换缓存（否则下面 resize 会拉伸变形）
        if img.width != img.height and w != h and (img.width > img.height) != (w > h):
            log.info("检测到屏幕旋转: 窗口尺寸 %dx%d → %dx%d", w, h, h, w)
            self._hands._window_size = (h, w)
            w, h = h, w
        # Retina 截图（如 3x）缩到逻辑点尺寸，让 LLM 看到的图与 tap 坐标同一空间
        if img.size != (w, h):
            img = img.resize((w, h), Image.LANCZOS)
        return img_to_png_bytes(img)

    def get_ui_tree(self) -> str:
        return self._hands.get_ui_tree()

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._hands.window_size

    @property
    def scale(self) -> float:
        # screenshot_raw 已把 Retina 截图缩放到逻辑点尺寸，
        # 返回图像 px == tap 坐标空间，比例恒为 1.0（skills 用它换算 bounds）
        return 1.0

    # --- Actions ---

    def tap(self, x: int, y: int) -> None:
        self._hands.tap(x, y)

    def input_text(self, text: str) -> None:
        if not text:
            return
        # Clear the focused field first so input replaces (not appends to)
        # the existing value. Mirrors Android logic — without this, test
        # cases that input multiple values in sequence (e.g. retrying with
        # 5 different invalid email formats) concatenate into a single
        # garbled string in the field.
        #
        # Skip clear when the focused field looks like a single-char OTP
        # slot (Flutter Pinput): each box is its own narrow TextField with
        # length=1, so clearing wipes the slot the user is currently
        # filling and breaks multi-step OTP entry.
        if not self._focused_is_otp_slot():
            self._clear_focused_field()
        if text.isascii() and " " not in text:
            # `idb ui text` can drop characters when typing fast bursts
            # (especially into Flutter Pinput widgets — observed on
            # short OTP digits never landing in the focused TextField).
            # Mitigation: char-by-char with verify-and-repair for ANY
            # input >1 char. Single-char goes through the batch path.
            if len(text) > 1:
                self._input_with_verify(text)
            else:
                self._hands._idb("ui", "text", text)
        else:
            # CJK or contains space — fall back to existing paste path
            # in hands.py (clipboard-based, handles non-ASCII reliably).
            self._hands.input_text(text)

    def _focused_is_otp_slot(self) -> bool:
        """True if the focused TextField is a narrow OTP slot (Flutter Pinput).

        Detection: parse UI tree JSON, find the focused TextField /
        SecureTextField. If its frame.width is < 25% of screen width,
        treat as OTP slot. Pinput renders each digit in its own narrow
        TextField (typically 30-60 pt wide on phones); regular text
        fields are at least 50% screen width.
        """
        try:
            import json as _json
            raw = self._hands.get_ui_tree() or ""
            nodes = _json.loads(raw)
            sw, _sh = self.screen_size
            for node in nodes:
                role = node.get("type", "") or node.get("AXRole", "")
                if "TextField" not in role and "SecureTextField" not in role:
                    continue
                focused = (
                    node.get("has_focus")
                    or node.get("focused")
                    or node.get("selected")
                )
                if not focused:
                    continue
                frame = node.get("frame", {})
                width = frame.get("width", 0)
                if width and width < sw * 0.25:
                    return True
        except Exception:
            pass
        return False

    def _clear_focused_field(self) -> None:
        """Best-effort clear of the focused TextField before typing.

        iOS has no native Ctrl+A across all apps. Strategy: read the
        current value length from UI tree, send that many backspaces
        (+5 cushion). Falls back to 30 backspaces if value isn't
        readable. Silently no-ops if no field is focused.
        """
        try:
            import json as _json
            raw = self._hands.get_ui_tree() or ""
            nodes = _json.loads(raw)
            n = 0
            for node in nodes:
                role = node.get("type", "") or node.get("AXRole", "")
                if "TextField" not in role and "SecureTextField" not in role:
                    continue
                focused = (
                    node.get("has_focus")
                    or node.get("focused")
                    or node.get("selected")
                )
                if focused:
                    value = (
                        node.get("AXValue", "")
                        or node.get("value", "")
                        or ""
                    )
                    n = len(str(value)) if value else 0
                    break
            if n == 0:
                # Couldn't read value — send a fixed safe number of
                # backspaces. 30 covers most fields and finishes in ~1.5s.
                n = 30
            else:
                n = n + 5  # cushion against off-by-one
            for _ in range(n):
                self._hands._idb("ui", "key", "42")  # HID keycode for delete/backspace
        except Exception as e:
            log.debug("clear_focused_field failed: %s", e)

    def _input_with_verify(self, text: str) -> None:
        """Char-by-char input + verify-and-repair, up to 3 attempts.

        `idb ui text` empirically drops chars on rapid bursts into
        Flutter Pinput widgets. Char-by-char with verification reads the
        focused field's current value from the UI tree after each batch
        and resumes / restarts as needed.
        """
        import json as _json
        import time as _t

        def _read_focused() -> tuple[str, str | None]:
            """返回聚焦输入框的 (role, value)；无聚焦字段时 ("", None)。"""
            try:
                raw = self._hands.get_ui_tree() or ""
                nodes = _json.loads(raw)
            except Exception:
                return "", None
            for node in nodes:
                role = node.get("type", "") or node.get("AXRole", "")
                if "TextField" not in role and "SecureTextField" not in role:
                    continue
                focused = (
                    node.get("has_focus")
                    or node.get("focused")
                    or node.get("selected")
                )
                if focused:
                    value = (
                        node.get("AXValue", "")
                        or node.get("value", "")
                        or ""
                    )
                    return role, str(value)
            return "", None

        def _read_focused_value() -> str | None:
            return _read_focused()[1]

        # 密码框（SecureTextField）回显掩码（•）/空值，verify 永远「分歧」，
        # 3 轮重试每轮都 backspace 清空 → 把已正确输入的密码擦掉。
        # 跳过 verify-and-repair，整串一次性发送即可。
        role, _ = _read_focused()
        if "SecureTextField" in role:
            self._hands._idb("ui", "text", text)
            return

        for _attempt in range(3):
            for ch in text:
                self._hands._idb("ui", "text", ch)
                _t.sleep(0.05)
            _t.sleep(0.3)
            role, actual = _read_focused()
            if actual is None:
                # Field gone (page transitioned, OTP auto-submitted, etc.).
                return
            if "SecureTextField" in role:
                # 焦点中途落到密码框：掩码值无法核对，别 backspace 擦字段
                return
            if actual == text:
                return
            if text.startswith(actual):
                missing = text[len(actual):]
                for ch in missing:
                    self._hands._idb("ui", "text", ch)
                    _t.sleep(0.05)
                _t.sleep(0.3)
                actual = _read_focused_value()
                if actual is not None and actual == text:
                    return
            # Diverged from prefix — clear and retry from scratch.
            for _ in range(len(actual or "") + 5):
                self._hands._idb("ui", "key", "42")
            _t.sleep(0.2)
        log.warning("input_text 多次 verify 失败, 字段可能不完整: target=%r", text)

    def press_key(self, key: str) -> None:
        self._hands.press_key(key)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._hands.swipe(x1, y1, x2, y2)

    def scroll_up(self) -> None:
        self._hands.swipe_up()

    def scroll_down(self) -> None:
        self._hands.swipe_down()

    def open_target(self, target: str) -> None:
        self._hands.open_app(target)

    # --- iOS-specific actions ---

    def long_press(self, x: int, y: int, duration: float = 1.5) -> None:
        self._hands._idb("ui", "tap", str(x), str(y), "--duration", str(duration))

    def _handle_platform_action(self, action: dict) -> None:
        action_type = action["type"]
        if action_type == "long_press":
            w, h = self.screen_size
            # 与 base.execute_action 的 clamp 一致：坐标 == 尺寸已在屏幕外 1px
            x = max(0, min(int(action["x"]), w - 1))
            y = max(0, min(int(action["y"]), h - 1))
            self.long_press(x, y, action.get("duration", 1.5))
        else:
            raise ValueError(f"Unknown action type for iOS: {action_type}")

    # --- Platform identity ---

    @property
    def platform_name(self) -> str:
        return "ios"

    def get_system_prompt_segment(self) -> str:
        return IOS_PROMPT_SEGMENT
