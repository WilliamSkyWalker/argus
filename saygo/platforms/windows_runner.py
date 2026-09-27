"""Windows desktop bridge for WSL — no Windows Python installation required."""

from __future__ import annotations

import base64
import io
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

from PIL import Image

from ..logger import get_logger
from .base import Platform
from .desktop_win import _PROMPT_SEGMENT

log = get_logger("desktop.winrunner")

# 桌面截图动辄 2560x1440（PNG ~400KB），直发视觉模型又慢又贵；缩到这个宽度上限。
# 只影响发给 LLM 的图，screen_size 仍报真实窗口尺寸 → x_pct 映射与 tap 坐标空间不变。
_MAX_IMAGE_WIDTH = 1280


class WindowsRunnerPlatform(Platform):
    """Drive the current Windows console via a bundled PowerShell/Win32 runner."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._size = (0, 0)
        self._desktop_path = ""
        self.connection = {}
        self.input_binding = None
        self._binding_callback = None
        self._window_metadata = {}
        self._expected_window = None

    def setup(self, config: dict) -> None:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError(
                "WSL Windows runner 需要启用 WSL interop，并能从 WSL 执行 powershell.exe。"
            )
        script = Path(__file__).with_name("windows_runner.ps1")
        if os.name == "nt":
            windows_script = str(script)
        else:
            windows_script = subprocess.run(
                ["wslpath", "-w", str(script)], capture_output=True, text=True,
                check=True, timeout=5,
            ).stdout.strip()
        from saygo.devices.toolchain import background_options
        self._process = subprocess.Popen(
            [
                powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                "-WindowStyle", "Hidden",
                "-ExecutionPolicy", "Bypass", "-File", windows_script,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1, **background_options(),
        )
        win = config.get("win", {}) or {}
        self._binding_callback = win.get("_binding_callback")
        result = self._call(
            "setup", app=str(win.get("app") or ""), launch=str(win.get("launch") or ""),
            foreground=bool(win.get("foreground")),
            input_binding=win.get("input_binding"),
            process_name=win.get("process_name"), process_id=win.get("process_id"),
            window_id=win.get("window_id"),
            new_window=win.get("new_window", False), new_window_args=win.get("new_window_args", []),
        )
        self.connection = result.get("connection", {})
        if self.connection.get("status") == "waiting_for_human":
            from .desktop import DesktopHandoffRequired
            raise DesktopHandoffRequired(self.connection)
        self._size = int(result["width"]), int(result["height"])
        self._desktop_path = str(result.get("desktop_path") or "")
        log.info(
            "Windows runner ready: title=%r size=%dx%d",
            result.get("title"), self._size[0], self._size[1],
        )

    def teardown(self) -> None:
        process = self._process
        if not process:
            return
        if process.poll() is None:
            try:
                self._call("teardown")
                process.wait(timeout=3)
            except Exception:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream:
                stream.close()
        self._process = None

    def _call(self, command: str, **payload: object) -> dict:
        process = self._process
        if not process or process.poll() is not None or not process.stdin or not process.stdout:
            detail = ""
            if process and process.stderr:
                detail = process.stderr.read().strip()
            raise RuntimeError(f"Windows runner 未运行。{detail}")
        with self._lock:
            self._request_id += 1
            request = {"id": self._request_id, "command": command, **payload}
            if command in {"tap", "swipe", "scroll", "input", "key", "open"} and self._expected_window:
                request["expected_window"] = self._expected_window
            process.stdin.write(json.dumps(request, ensure_ascii=False) + "\n")
            process.stdin.flush()
            line = process.stdout.readline()
            if not line:
                detail = process.stderr.read().strip() if process.stderr else ""
                raise RuntimeError(f"Windows runner 意外退出。{detail}")
            response = json.loads(line)
            if response.get("id") != self._request_id:
                raise RuntimeError("Windows runner 响应序号不匹配。")
            if not response.get("ok"):
                raise RuntimeError(f"Windows runner 操作失败：{response.get('error', '未知错误')}")
            data = response.get("data") or {}
            if "input_binding" in data:
                binding = data["input_binding"]
                if self._binding_callback and (binding != self.input_binding or command == "setup"):
                    self._binding_callback(binding)
                self.input_binding = binding
            return data

    def screenshot_raw(self) -> bytes:
        result = self._call("screenshot")
        self._size = int(result["width"]), int(result["height"])
        self._window_metadata = {key: result[key] for key in ("window_id", "process_id", "window_bounds") if key in result}
        return self._shrink(base64.b64decode(result["png"], validate=True))

    def observation_metadata(self):
        return dict(self._window_metadata)

    def diagnose(self):
        """Read window evidence without capture, activation, or input."""
        return self._call("diagnose")

    def expect_window(self, observation):
        self._expected_window = {key: observation[key] for key in
                                 ("window_id", "process_id", "window_bounds") if key in observation}

    def _shrink(self, png: bytes) -> bytes:
        img = Image.open(io.BytesIO(png))
        if img.width <= _MAX_IMAGE_WIDTH:
            return png
        height = max(1, round(img.height * _MAX_IMAGE_WIDTH / img.width))
        img = img.resize((_MAX_IMAGE_WIDTH, height), Image.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()

    def screenshot_png(self) -> bytes:
        return self.screenshot_raw()

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._size

    @property
    def scale(self) -> float:
        return min(1.0, _MAX_IMAGE_WIDTH / self._size[0]) if self._size[0] else 1.0

    def tap(self, x: int, y: int) -> None:
        self._call("tap", x=x, y=y)

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._call("swipe", x1=x1, y1=y1, x2=x2, y2=y2)

    def scroll_up(self) -> None:
        self._call("scroll", direction="up")

    def scroll_down(self) -> None:
        self._call("scroll", direction="down")

    def input_text(self, text: str) -> None:
        self._call("input", text=text)

    def press_key(self, key: str) -> None:
        self._call("key", key=key)

    def open_target(self, target: str) -> None:
        self._call("open", target=target)

    @property
    def platform_name(self) -> str:
        return "windows"

    def get_system_prompt_segment(self) -> str:
        extra = (
            "\n\nWindows 后台 runner：\n"
            "- 坐标始终相对于目标窗口；先 tap 输入框，再 input。\n"
            "- 不使用全局鼠标、键盘或剪贴板；控件自身仍可能响应消息而激活窗口。\n"
            "- Ctrl+A 仅支持原生 Edit/RichEdit 输入框，其他组合键暂不支持。\n"
            "- 截图来自目标窗口；截取失败或窗口关闭时明确报错，不回退到桌面截图。\n"
            "- 后台输入可能没有可见光标，必须验证文字或按钮结果；派发成功不代表应用已处理。\n"
            "- GPU/自绘控件可能不支持后台操作；无效果时停止重试并请求人工接管。"
        )
        return _PROMPT_SEGMENT + extra
