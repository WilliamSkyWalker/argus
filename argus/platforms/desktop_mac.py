"""macOS desktop platform —— 窗口级纯视觉驱动（前台方案）。

语义与移动端同构：截图 = **被测 App 窗口**画面（不是整屏），brain 出窗口内百分比坐标，
框架换算成全局坐标后用 pyautogui 点击/输入。跑 case 时把被测 App 保持在最前（每 turn
截图前 activate 一次），因此点击/键盘走系统前台注入即可生效——**允许占用鼠标/键盘焦点**
（已与用户确认的前台方案；后台不打扰的方案是独立图形会话/VNC，另做）。不走 AX。

依赖：`pip3 install pyautogui pyobjc-framework-Quartz`。
权限：系统设置 → 隐私与安全性 → **屏幕录制**（按窗口截图）+ **辅助功能**（鼠标键盘）。

坐标：CGWindowBounds 是**逻辑点**全局坐标（多显示器下第二屏可能为负）；pyautogui 也用
逻辑点全局坐标，一一对应。截图 CGWindowListCreateImage 出的是**物理像素**（Retina 2x），
scale = 截图像素宽 / 窗口逻辑宽，仅供报告/调试。
"""

import io
import shutil
import subprocess
import time

from ..logger import get_logger
from .base import Platform

log = get_logger("desktop.mac")

_WINDOW_WAIT_S = 12          # setup 等被测 App 窗口出现的超时
_ACTIVATE_SETTLE_S = 0.25    # activate 后让前台切换落定

_PROMPT_SEGMENT = """你正在操作一台 macOS 桌面上的**某个被测 App**来执行测试用例。

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

_KEY_MAP = {
    "enter": "enter", "return": "enter", "delete": "backspace", "backspace": "backspace",
    "tab": "tab", "space": "space", "escape": "esc", "esc": "esc",
    "up": "up", "down": "down", "left": "left", "right": "right",
    "home": "home", "end": "end", "pageup": "pageup", "pagedown": "pagedown",
}


class DesktopMacPlatform(Platform):
    """macOS 桌面驱动：窗口级截图 + 前台保持 + pyautogui 全局坐标注入。"""

    def __init__(self):
        self._pg = None          # pyautogui
        self._Q = None           # Quartz
        self._AK = None          # AppKit
        self._app_name = ""      # 被测 App（CGWindowOwnerName / open -a 的名字）
        self._pid = 0
        self._win_id = 0
        # 窗口全局 bounds（逻辑点）；tap 坐标 = 窗口内坐标 + (win_x, win_y)
        self._win_x = 0.0
        self._win_y = 0.0
        self._win_w = 0
        self._win_h = 0
        self._last_shot_size: tuple[int, int] | None = None

    # --- Lifecycle ---

    def setup(self, config: dict) -> None:
        try:
            import pyautogui
            import Quartz
            import AppKit
        except Exception as e:
            raise RuntimeError(
                "macOS 桌面驱动依赖 pyautogui + pyobjc。装法：\n"
                "  pip3 install pyautogui pyobjc-framework-Quartz\n"
                f"（原始错误：{e}）"
            )
        pyautogui.FAILSAFE = False
        self._pg, self._Q, self._AK = pyautogui, Quartz, AppKit

        app = (config.get("mac", {}) or {}).get("app", "").strip()
        if not app:
            raise RuntimeError(
                "跑 mac 平台必须指定被测 App —— 在 .env 配 MAC_APP=<应用名>，"
                "或 `MAC_APP=Calculator python3 -m argus.cli run … --platform mac`。")
        self._app_name = app

        # 启动/前置 App，再等它的窗口出现
        self._open_app(app)
        deadline = time.time() + _WINDOW_WAIT_S
        found = None
        while time.time() < deadline:
            found = self._find_window()
            if found:
                break
            time.sleep(0.5)
        if not found:
            raise RuntimeError(
                f"{_WINDOW_WAIT_S}s 内未找到 App「{app}」的窗口。请确认 App 名正确"
                f"（= 菜单栏显示名 / CGWindowOwnerName）且已能正常打开一个窗口。")
        self._apply_window(found)
        log.info("macOS 桌面就绪: app=%s pid=%d win=%dx%d @(%.0f,%.0f)",
                 app, self._pid, self._win_w, self._win_h, self._win_x, self._win_y)

    def teardown(self) -> None:
        self._pg = self._Q = self._AK = None

    # --- 窗口发现 / 前台保持 ---

    def _open_app(self, app: str) -> None:
        try:
            flag = "-b" if app.count(".") >= 2 and " " not in app else "-a"
            subprocess.run(["open", flag, app], check=False, timeout=15)
            time.sleep(1.0)
        except Exception as e:
            log.warning("open %s 失败: %s", app, e)

    def _find_window(self):
        """按 ownerName 找被测 App 的主窗口（layer==0，面积最大）。返回 window info dict 或 None。"""
        Q = self._Q
        wins = Q.CGWindowListCopyWindowInfo(Q.kCGWindowListOptionAll, Q.kCGNullWindowID)
        best, best_area = None, -1
        for w in wins or []:
            if w.get("kCGWindowOwnerName") != self._app_name:
                continue
            if int(w.get("kCGWindowLayer", 0)) != 0:   # 排除菜单/dock/浮层
                continue
            b = w.get("kCGWindowBounds") or {}
            area = float(b.get("Width", 0)) * float(b.get("Height", 0))
            if area > best_area:
                best, best_area = w, area
        return best

    def _apply_window(self, w) -> None:
        b = w["kCGWindowBounds"]
        self._win_id = int(w["kCGWindowNumber"])
        self._pid = int(w["kCGWindowOwnerPID"])
        self._win_x = float(b["X"])
        self._win_y = float(b["Y"])
        self._win_w = int(round(float(b["Width"])))
        self._win_h = int(round(float(b["Height"])))

    def _ensure_frontmost(self) -> None:
        """把被测 App 切到最前（不是最前才 activate）。前台方案的核心——每 turn 截图前调用。"""
        try:
            ws = self._AK.NSWorkspace.sharedWorkspace()
            front = ws.frontmostApplication()
            if front is not None and int(front.processIdentifier()) == self._pid:
                return
            app = self._AK.NSRunningApplication.runningApplicationWithProcessIdentifier_(self._pid)
            if app is not None:
                # NSApplicationActivateIgnoringOtherApps = 1 << 1
                app.activateWithOptions_(1 << 1)
                time.sleep(_ACTIVATE_SETTLE_S)
        except Exception as e:
            log.debug("activate 前台失败: %s", e)

    # --- Observation ---

    def screenshot_raw(self) -> bytes:
        """截被测 App 窗口（先保前台 + 刷新窗口 bounds）。返回 PNG bytes。"""
        self._ensure_frontmost()
        w = self._find_window()
        if w is None:
            raise RuntimeError(f"被测 App「{self._app_name}」窗口消失（可能已退出/最小化）。")
        self._apply_window(w)

        Q, AK = self._Q, self._AK
        img = Q.CGWindowListCreateImage(
            Q.CGRectNull, Q.kCGWindowListOptionIncludingWindow, self._win_id,
            Q.kCGWindowImageBoundsIgnoreFraming)
        if img is None:
            raise RuntimeError(
                "CGWindowListCreateImage 返回空——检查『屏幕录制』权限是否已授给运行 argus 的进程。")
        px_w, px_h = int(Q.CGImageGetWidth(img)), int(Q.CGImageGetHeight(img))
        self._last_shot_size = (px_w, px_h)
        rep = AK.NSBitmapImageRep.alloc().initWithCGImage_(img)
        png = rep.representationUsingType_properties_(AK.NSBitmapImageFileTypePNG, None)
        return bytes(png)

    def screenshot_png(self) -> bytes:
        return self.screenshot_raw()

    @property
    def screen_size(self) -> tuple[int, int]:
        """被测窗口的逻辑尺寸——brain 的 x_pct/y_pct 相对它换算。"""
        return (self._win_w, self._win_h)

    @property
    def scale(self) -> float:
        """截图物理像素 / 窗口逻辑宽。Retina 屏 2.0，普通屏 1.0。"""
        if self._last_shot_size and self._win_w:
            return self._last_shot_size[0] / self._win_w
        return 1.0

    # --- Actions（入参为窗口内逻辑坐标，加窗口 origin 换算成全局逻辑坐标）---

    def _to_global(self, x: int, y: int) -> tuple[int, int]:
        return (int(round(self._win_x + x)), int(round(self._win_y + y)))

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
        self._pg.scroll(5)

    def scroll_down(self) -> None:
        self._pg.scroll(-5)

    def input_text(self, text: str) -> None:
        """剪贴板 pbcopy + ⌘V（支持中文）。前台方案下焦点在被测 App，粘贴落到聚焦控件。"""
        if not text:
            return
        try:
            pb = shutil.which("pbcopy") or "/usr/bin/pbcopy"
            subprocess.run([pb], input=text.encode("utf-8"), check=True, timeout=5)
            self._pg.hotkey("command", "v")
        except Exception as e:
            log.warning("剪贴板粘贴失败，退回 typewrite(仅 ASCII): %s", e)
            try:
                self._pg.typewrite(text, interval=0.02)
            except Exception as e2:
                log.warning("typewrite 也失败: %s", e2)

    def press_key(self, key: str) -> None:
        k = _KEY_MAP.get(str(key).strip().lower())
        if k is None:
            log.warning("press_key 不识别: %r", key)
            return
        self._pg.press(k)

    def open_target(self, target: str) -> None:
        self._open_app(target)

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
            raise ValueError(f"Unknown action type for macOS desktop: {atype}")

    # --- Platform identity ---

    @property
    def platform_name(self) -> str:
        return "mac"

    def get_system_prompt_segment(self) -> str:
        return _PROMPT_SEGMENT
