"""Windows 桌面平台 —— 窗口级纯视觉驱动（前台方案，平移 desktop_mac.py）。

语义与 mac/移动端同构：截图 = **被测 App 窗口**画面（不是整屏），brain 出窗口内百分比坐标，
框架换算成全屏物理坐标后用 pyautogui 点击/输入。跑 case 时把被测窗口保持在最前（每 turn
截图前 SetForegroundWindow 一次），因此点击/键盘走系统前台注入即可生效——**允许占用鼠标/
键盘焦点**（同 mac 已确认的前台方案；后台不打扰的方案是 RDP 独立会话，另做）。不走 UIA。

参考 midscene 的 `@midscene/computer`：也是纯截图 + 坐标注入、不碰 UIA/DOM；local 模式同样
接管真实鼠标键盘。差别只在这里按**窗口**截、它按**display** 截。

依赖：`pip install pyautogui pywin32 pillow`。pillow 已是 argus 依赖。
无系统级权限门槛（不像 mac 要屏幕录制/辅助功能授权）。

⚠️ DPI 缩放是 Windows 头号坑（等同安卓分辨率标定）：setup 时把进程设为 **Per-Monitor
DPI aware**，此后 GetWindowRect / ImageGrab / pyautogui 三者**全部落在物理像素**同一坐标系，
无需再乘 scale。不设的话进程被系统虚拟化（低分截图 + 坐标偏移），点不中。
"""

import subprocess
import time

from ..logger import get_logger
from .base import Platform

log = get_logger("desktop.win")

_WINDOW_WAIT_S = 12          # setup 等被测窗口出现的超时
_ACTIVATE_SETTLE_S = 0.25    # SetForegroundWindow 后让前台切换落定

_PROMPT_SEGMENT = """你正在操作一台 Windows 桌面上的**某个被测 App**来执行测试用例。

截图就是**该 App 的窗口画面**（不是整个桌面），坐标相对这张窗口截图，左上角为 (0,0)。

可用操作类型：
- tap: {"x_pct": 0-100, "y_pct": 0-100, "target": "被点元素的简短描述"}  — 鼠标左键单击(百分比坐标,相对窗口截图宽/高)
- swipe: {"x1_pct": .., "y1_pct": .., "x2_pct": .., "y2_pct": ..}  — 按住拖拽(百分比)
- scroll_up / scroll_down  — 滚轮上/下滚动
- input: {"text": "string"}  — 输入文字(在当前焦点;走剪贴板粘贴,支持中文)
- press_key: {"key": "enter|delete|tab|space|escape|up|down|left|right"}  — 按键
- wait: {"seconds": int}  — 等待(1-5 秒)
- done: {"result": "pass|fail", "reason": "string"}  — 报告测试结果

注意：
- 纯视觉模式，截图无辅助标记，直接用视觉给百分比坐标
- 桌面元素小，点按钮/菜单项/关闭钮时看清中心再给点
- 输入前先 tap 目标输入框使其聚焦，再 input"""

# pyautogui 的按键名（跨平台基本一致）
_KEY_MAP = {
    "enter": "enter", "return": "enter", "delete": "backspace", "backspace": "backspace",
    "tab": "tab", "space": "space", "escape": "esc", "esc": "esc",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
}


class DesktopWinPlatform(Platform):
    """Windows 桌面驱动：窗口级截图 + 前台保持 + pyautogui 物理像素坐标注入。"""

    def __init__(self):
        self._pg = None          # pyautogui
        self._w32 = None         # win32gui
        self._w32con = None      # win32con
        self._grab = None        # PIL.ImageGrab
        self._app = ""           # 被测窗口标题子串（WIN_APP）
        self._launch = ""        # 可选启动名/路径（WIN_LAUNCH）
        self._hwnd = 0
        # 窗口全屏物理坐标 bounds；tap 坐标 = 窗口内坐标 + (win_x, win_y)
        self._win_x = 0
        self._win_y = 0
        self._win_w = 0
        self._win_h = 0

    # --- Lifecycle ---

    def setup(self, config: dict) -> None:
        try:
            import pyautogui
            import win32gui
            import win32con
            from PIL import ImageGrab
        except Exception as e:
            raise RuntimeError(
                "Windows 桌面驱动依赖 pyautogui + pywin32 + pillow。装法：\n"
                "  pip install pyautogui pywin32 pillow\n"
                f"（原始错误：{e}）"
            )
        pyautogui.FAILSAFE = False
        self._pg, self._w32, self._w32con, self._grab = (
            pyautogui, win32gui, win32con, ImageGrab)

        self._set_dpi_aware()

        wcfg = config.get("win", {}) or {}
        self._app = (wcfg.get("app", "") or "").strip()
        self._launch = (wcfg.get("launch", "") or "").strip()
        if not self._app:
            raise RuntimeError(
                "跑 windows 平台必须指定被测窗口 —— 在 .env 配 WIN_APP=<窗口标题子串>，"
                "或 `WIN_APP=计算器 python -m argus.cli run … --platform windows`。")

        # 有 WIN_LAUNCH 就先启动，再等窗口出现
        if self._launch:
            self._launch_app(self._launch)
        deadline = time.time() + _WINDOW_WAIT_S
        found = 0
        while time.time() < deadline:
            found = self._find_window()
            if found:
                break
            time.sleep(0.5)
        if not found:
            raise RuntimeError(
                f"{_WINDOW_WAIT_S}s 内未找到标题含「{self._app}」的可见窗口。请确认 "
                f"WIN_APP 是标题栏文字的子串，且该 App 已开着一个窗口"
                + ("，或配 WIN_LAUNCH 指定它的启动路径。" if not self._launch else "。"))
        self._apply_window(found)
        log.info("Windows 桌面就绪: app=%r hwnd=%d win=%dx%d @(%d,%d)",
                 self._app, self._hwnd, self._win_w, self._win_h, self._win_x, self._win_y)

    def teardown(self) -> None:
        self._pg = self._w32 = self._w32con = self._grab = None

    def _set_dpi_aware(self) -> None:
        """把进程设为 Per-Monitor DPI aware，让截图/坐标/点击都落物理像素同一坐标系。

        优先 PER_MONITOR_AWARE_V2（Win10 1703+），回落旧 API。已被 manifest 设过会返回
        失败，忽略即可（说明已经 aware）。必须在任何窗口交互前调用。
        """
        import ctypes
        try:
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
            return
        except Exception:
            pass
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
            return
        except Exception:
            pass
        try:
            ctypes.windll.user32.SetProcessDPIAware()        # 旧系统兜底（system-aware）
        except Exception as e:
            log.debug("SetProcessDPIAware 失败（可能已 aware）: %s", e)

    # --- 窗口发现 / 前台保持 ---

    def _launch_app(self, launch: str) -> None:
        try:
            # cmd start 能吃 exe 路径、注册的应用名、甚至 shell 动词；空标题占位是 start 语法要求
            subprocess.run(["cmd", "/c", "start", "", launch], check=False, timeout=15)
            time.sleep(1.5)
        except Exception as e:
            log.warning("启动 %r 失败: %s", launch, e)

    def _find_window(self) -> int:
        """按标题子串找被测 App 的主窗口（可见、非最小化、面积最大）。返回 hwnd 或 0。"""
        w32 = self._w32
        needle = self._app.lower()
        best, best_area = 0, -1

        def _cb(hwnd, _):
            nonlocal best, best_area
            if not w32.IsWindowVisible(hwnd):
                return
            title = w32.GetWindowText(hwnd) or ""
            if needle not in title.lower():
                return
            try:
                l, t, r, b = w32.GetWindowRect(hwnd)
            except Exception:
                return
            area = (r - l) * (b - t)
            if area <= 0:          # 最小化窗口 rect 会是负/零
                return
            if area > best_area:
                best, best_area = hwnd, area

        w32.EnumWindows(_cb, None)
        return best

    def _apply_window(self, hwnd: int) -> None:
        self._hwnd = hwnd
        rect = self._visible_bounds(hwnd)
        l, t, r, b = rect
        self._win_x, self._win_y = l, t
        self._win_w, self._win_h = r - l, b - t

    def _visible_bounds(self, hwnd) -> tuple[int, int, int, int]:
        """窗口真实可见 bounds。优先 DWM 扩展帧（去掉 Win10+ 的隐形缩放边框），
        回落 GetWindowRect。返回 (left, top, right, bottom) 物理像素。"""
        import ctypes
        from ctypes import wintypes
        try:
            rect = wintypes.RECT()
            # DWMWA_EXTENDED_FRAME_BOUNDS = 9
            hr = ctypes.windll.dwmapi.DwmGetWindowAttribute(
                wintypes.HWND(hwnd), 9, ctypes.byref(rect), ctypes.sizeof(rect))
            if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return (rect.left, rect.top, rect.right, rect.bottom)
        except Exception:
            pass
        return self._w32.GetWindowRect(hwnd)

    def _ensure_frontmost(self) -> None:
        """把被测窗口切到最前。前台方案核心——每 turn 截图前调用。

        SetForegroundWindow 从后台进程调用常被系统拦（前台锁）；先 restore + 用
        AttachThreadInput 挂到当前前台线程再抬，是标准绕法。失败不致命（可能已在前台）。
        """
        w32, con = self._w32, self._w32con
        hwnd = self._hwnd
        try:
            if w32.GetForegroundWindow() == hwnd:
                return
            if w32.IsIconic(hwnd):                 # 最小化了先还原
                w32.ShowWindow(hwnd, con.SW_RESTORE)
            try:
                w32.SetForegroundWindow(hwnd)
            except Exception:
                # 前台锁：把本线程 attach 到当前前台窗口线程再抬
                import win32process
                import win32api
                fg = w32.GetForegroundWindow()
                cur_tid = win32api.GetCurrentThreadId()
                fg_tid, _ = win32process.GetWindowThreadProcessId(fg) if fg else (0, 0)
                tgt_tid, _ = win32process.GetWindowThreadProcessId(hwnd)
                if fg_tid and fg_tid != tgt_tid:
                    w32.AttachThreadInput(cur_tid, fg_tid, True)
                    try:
                        w32.SetForegroundWindow(hwnd)
                    finally:
                        w32.AttachThreadInput(cur_tid, fg_tid, False)
            time.sleep(_ACTIVATE_SETTLE_S)
        except Exception as e:
            log.debug("SetForegroundWindow 失败: %s", e)

    # --- Observation ---

    def screenshot_raw(self) -> bytes:
        """截被测窗口（先保前台 + 刷新窗口 bounds）。返回 PNG bytes。

        窗口在前台后，用 ImageGrab 全屏抓再按 bounds 裁——比 PrintWindow 可靠（后者对
        GPU/Electron/自绘窗口常出黑图）。all_screens=True 覆盖多显示器/负坐标。
        """
        import io
        self._ensure_frontmost()
        hwnd = self._find_window()
        if not hwnd:
            raise RuntimeError(f"被测窗口「{self._app}」消失（可能已退出/最小化）。")
        self._hwnd = hwnd
        self._apply_window(hwnd)

        l, t = self._win_x, self._win_y
        r, b = l + self._win_w, t + self._win_h
        img = self._grab.grab(bbox=(l, t, r, b), all_screens=True)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()

    def screenshot_png(self) -> bytes:
        return self.screenshot_raw()

    @property
    def screen_size(self) -> tuple[int, int]:
        """被测窗口的物理像素尺寸——brain 的 x_pct/y_pct 相对它换算。"""
        return (self._win_w, self._win_h)

    @property
    def scale(self) -> float:
        """DPI-aware 下截图与坐标同在物理像素，无额外缩放。仅供报告一致性。"""
        return 1.0

    # --- Actions（入参为窗口内坐标，加窗口 origin 换算成全屏物理坐标）---

    def _to_global(self, x: int, y: int) -> tuple[int, int]:
        return (int(self._win_x + x), int(self._win_y + y))

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
        """剪贴板 + Ctrl+V（支持中文）。前台方案下焦点在被测窗口，粘贴落到聚焦控件。"""
        if not text:
            return
        try:
            self._set_clipboard(text)
            self._pg.hotkey("ctrl", "v")
        except Exception as e:
            log.warning("剪贴板粘贴失败，退回 typewrite(仅 ASCII): %s", e)
            try:
                self._pg.typewrite(text, interval=0.02)
            except Exception as e2:
                log.warning("typewrite 也失败: %s", e2)

    def _set_clipboard(self, text: str) -> None:
        """写剪贴板。优先 win32clipboard（pywin32 自带），回落 pyperclip。"""
        try:
            import win32clipboard
            win32clipboard.OpenClipboard()
            try:
                win32clipboard.EmptyClipboard()
                win32clipboard.SetClipboardText(text, win32clipboard.CF_UNICODETEXT)
            finally:
                win32clipboard.CloseClipboard()
            return
        except Exception:
            import pyperclip
            pyperclip.copy(text)

    def press_key(self, key: str) -> None:
        k = _KEY_MAP.get(str(key).strip().lower())
        if k is None:
            log.warning("press_key 不识别: %r", key)
            return
        self._pg.press(k)

    def open_target(self, target: str) -> None:
        self._launch_app(target)

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
            raise ValueError(f"Unknown action type for Windows desktop: {atype}")

    # --- Platform identity ---

    @property
    def platform_name(self) -> str:
        return "windows"

    def get_system_prompt_segment(self) -> str:
        return _PROMPT_SEGMENT
