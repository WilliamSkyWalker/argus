"""Common window-relative desktop actions; native drivers own OS integration."""
from abc import abstractmethod
import time

from ..logger import get_logger
from .base import Platform

log = get_logger("desktop")

_KEY_MAP = {
    "enter": "enter", "return": "enter", "delete": "backspace", "backspace": "backspace",
    "tab": "tab", "space": "space", "escape": "esc", "esc": "esc",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
}


class DesktopPlatform(Platform):
    """Shared input flow with native coordinate and clipboard hooks."""

    key_map = _KEY_MAP
    paste_modifier = "ctrl"
    desktop_label = "Windows"

    @abstractmethod
    def _to_global(self, x: int, y: int) -> tuple[int, int]:
        """Convert window coordinates to the native input coordinate space."""

    @abstractmethod
    def _set_clipboard(self, text: str) -> None:
        """Write Unicode text to the host clipboard."""

    @abstractmethod
    def screenshot_raw(self) -> bytes:
        """Capture the native window without coordinate overlays."""

    def screenshot_png(self) -> bytes:
        return self.screenshot_raw()

    @property
    def screen_size(self) -> tuple[int, int]:
        """Window dimensions in the native driver's input coordinate space."""
        return (self._win_w, self._win_h)

    def tap(self, x: int, y: int) -> None:
        gx, gy = self._to_global(x, y)
        self._pg.click(gx, gy)

    def long_press(self, x: int, y: int, duration: float = 1.0) -> None:
        gx, gy = self._to_global(x, y)
        self._pg.mouseDown(gx, gy)
        time.sleep(max(0.1, duration))
        self._pg.mouseUp(gx, gy)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        gx1, gy1 = self._to_global(x1, y1)
        gx2, gy2 = self._to_global(x2, y2)
        self._pg.moveTo(gx1, gy1)
        self._pg.dragTo(gx2, gy2, duration=0.4, button="left")

    def scroll_up(self) -> None:
        # pyautogui.scroll 单位是滚轮 notch（正=上）；跨平台一致，与 mac 取同量级
        self._pg.scroll(5)

    def scroll_down(self) -> None:
        self._pg.scroll(-5)

    def input_text(self, text: str) -> None:
        """Paste through the native clipboard, falling back to ASCII typing."""
        if not text:
            return
        try:
            self._set_clipboard(text)
            self._pg.hotkey(self.paste_modifier, "v")
        except Exception as e:
            log.warning("剪贴板粘贴失败，退回 typewrite(仅 ASCII): %s", e)
            try:
                self._pg.typewrite(text, interval=0.02)
            except Exception as e2:
                log.warning("typewrite 也失败: %s", e2)

    def press_key(self, key: str) -> None:
        k = self.key_map.get(str(key).strip().lower())
        if k is None:
            log.warning("press_key 不识别: %r", key)
            return
        self._pg.press(k)

    def is_ime_visible(self) -> bool:
        return False

    def _handle_platform_action(self, action: dict) -> None:
        atype = action["type"]
        if atype == "long_press":
            w, h = self.screen_size
            x = max(0, min(int(action.get("x", 0)), w - 1))
            y = max(0, min(int(action.get("y", 0)), h - 1))
            self.long_press(x, y, float(action.get("duration", 1.0)))
        else:
            raise ValueError(f"Unknown action type for {self.desktop_label} desktop: {atype}")
