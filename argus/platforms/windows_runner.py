"""Windows desktop bridge for WSL — no Windows Python installation required."""

from __future__ import annotations

import base64
import json
import shutil
import subprocess
import threading
from pathlib import Path

from ..logger import get_logger
from .base import Platform
from .desktop_win import _PROMPT_SEGMENT

log = get_logger("desktop.winrunner")


class WindowsRunnerPlatform(Platform):
    """Drive the current Windows console via a bundled PowerShell/Win32 runner."""

    def __init__(self) -> None:
        self._process: subprocess.Popen[str] | None = None
        self._lock = threading.Lock()
        self._request_id = 0
        self._size = (0, 0)

    def setup(self, config: dict) -> None:
        powershell = shutil.which("powershell.exe")
        if not powershell:
            raise RuntimeError(
                "WSL Windows runner 需要启用 WSL interop，并能从 WSL 执行 powershell.exe。"
            )
        script = Path(__file__).with_name("windows_runner.ps1")
        windows_script = subprocess.run(
            ["wslpath", "-w", str(script)], capture_output=True, text=True,
            check=True, timeout=5,
        ).stdout.strip()
        self._process = subprocess.Popen(
            [
                powershell, "-NoLogo", "-NoProfile", "-NonInteractive", "-STA",
                "-ExecutionPolicy", "Bypass", "-File", windows_script,
            ],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", bufsize=1,
        )
        win = config.get("win", {}) or {}
        result = self._call(
            "setup", app=str(win.get("app") or ""), launch=str(win.get("launch") or ""),
        )
        self._size = int(result["width"]), int(result["height"])
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
            return response.get("data") or {}

    def screenshot_raw(self) -> bytes:
        result = self._call("screenshot")
        self._size = int(result["width"]), int(result["height"])
        return base64.b64decode(result["png"], validate=True)

    def screenshot_png(self) -> bytes:
        return self.screenshot_raw()

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._size

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
        return _PROMPT_SEGMENT
