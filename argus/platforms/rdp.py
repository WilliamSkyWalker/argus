"""Experimental RDP platform — operate a remote Windows desktop via FreeRDP.

The RDP client renders into a private Xvfb display.  Argus captures that display
and sends X input to it, which FreeRDP forwards as native RDP input events.  The
Windows host therefore needs only Remote Desktop enabled, not Python or an agent.
"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import time
from pathlib import Path

from PIL import ImageGrab

from ..logger import get_logger
from .base import Platform

log = get_logger("platform.rdp")

_CONNECT_WAIT_S = 8
_PROMPT_SEGMENT = """You are operating a remote Windows desktop over RDP.
The screenshot is the entire remote desktop. Use only visible evidence. The
session is dedicated to this test, so normal desktop mouse and keyboard actions
are available. Do not assume a local machine window or a mobile soft keyboard."""

_KEY_MAP = {
    "enter": "Return", "return": "Return", "tab": "Tab", "escape": "Escape",
    "esc": "Escape", "backspace": "BackSpace", "delete": "Delete",
    "space": "space", "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Prior", "pagedown": "Next",
}


class RDPPlatform(Platform):
    """Dedicated RDP desktop backed by xfreerdp running in a private X server."""

    def __init__(self) -> None:
        self._display = ""
        self._xvfb: subprocess.Popen[bytes] | None = None
        self._rdp: subprocess.Popen[bytes] | None = None
        self._xfreerdp = ""
        self._width = 0
        self._height = 0
        self._start_program = ""

    def setup(self, config: dict) -> None:
        log.warning(
            "RDPPlatform 仍在开发中，仅供实验验证；远程会话、应用启动和输入行为尚未承诺稳定。"
        )
        rdp = config.get("rdp", {}) or {}
        host = str(rdp.get("host") or "").strip()
        username = str(rdp.get("username") or "").strip()
        password = str(rdp.get("password") or "")
        if not host or not username or not password:
            raise RuntimeError(
                "RDP 平台需要 RDP_HOST、RDP_USERNAME 和 RDP_PASSWORD。"
                "它们只应写入本地 .env 或环境变量，不能提交到仓库。"
            )

        self._width = self._positive_int(rdp.get("width"), "RDP_WIDTH")
        self._height = self._positive_int(rdp.get("height"), "RDP_HEIGHT")
        port = self._positive_int(rdp.get("port"), "RDP_PORT")
        certificate = str(rdp.get("certificate") or "tofu").strip().lower()
        if certificate not in {"tofu", "ignore"}:
            raise RuntimeError("RDP_CERTIFICATE 仅支持 tofu 或 ignore。生产环境请使用 tofu。")
        self._start_program = str(rdp.get("start_program") or "").strip()

        self._xfreerdp = shutil.which("xfreerdp3") or shutil.which("xfreerdp") or ""
        missing = [
            name for name, value in {
                "xfreerdp": self._xfreerdp,
                "Xvfb": shutil.which("Xvfb"),
                "xdotool": shutil.which("xdotool"),
            }.items() if not value
        ]
        if missing:
            raise RuntimeError(
                "RDP 平台缺少 " + ", ".join(missing) + "。Ubuntu/WSL 安装：\n"
                "  sudo apt install freerdp2-x11 xvfb xdotool\n"
                "新发行版若提供 freerdp3-x11，请安装该包。"
            )

        self._display = self._start_xvfb()
        command = [
            self._xfreerdp, f"/v:{host}:{port}", f"/u:{username}",
            f"/size:{self._width}x{self._height}", "/bpp:32",
            f"/cert:{certificate}", "/network:auto", "/clipboard", "/from-stdin:force",
        ]

        env = os.environ.copy()
        env["DISPLAY"] = self._display
        # Keep the secret out of argv and the process environment.
        try:
            self._rdp = subprocess.Popen(
                command, env=env, stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            assert self._rdp.stdin is not None
            self._rdp.stdin.write((password + "\n").encode())
            self._rdp.stdin.close()
        except Exception:
            self.teardown()
            raise
        try:
            self._wait_for_first_frame(host, password)
        except Exception:
            self.teardown()
            raise
        if self._start_program:
            self._launch_remote_target(self._start_program)
        log.info("RDP session connected: host=%s size=%dx%d", host, self._width, self._height)

    def teardown(self) -> None:
        for proc in (self._rdp, self._xvfb):
            if proc and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=3)
        self._rdp = self._xvfb = None
        self._display = ""

    @staticmethod
    def _positive_int(value: object, name: str) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{name} 必须是正整数。") from exc
        if result <= 0:
            raise RuntimeError(f"{name} 必须是正整数。")
        return result

    def _start_xvfb(self) -> str:
        for number in range(90, 111):
            display = f":{number}"
            if Path(f"/tmp/.X{number}-lock").exists():
                continue
            proc = subprocess.Popen(
                ["Xvfb", display, "-screen", "0", f"{self._width}x{self._height}x24", "-nolisten", "tcp"],
                stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            )
            time.sleep(0.15)
            if proc.poll() is None:
                self._xvfb = proc
                return display
        raise RuntimeError("无法启动隔离 Xvfb 显示器（:90 至 :110 均不可用）。")

    def _wait_for_first_frame(self, host: str, password: str) -> None:
        assert self._rdp is not None
        deadline = time.monotonic() + _CONNECT_WAIT_S
        while time.monotonic() < deadline:
            code = self._rdp.poll()
            if code is not None:
                stderr = self._rdp.stderr.read().decode("utf-8", "replace") if self._rdp.stderr else ""
                raise RuntimeError(
                    f"RDP 连接 {host} 失败（xfreerdp 退出码 {code}）："
                    f"{self._redact(stderr, password).strip() or '无诊断输出'}"
                )
            try:
                frame = ImageGrab.grab(xdisplay=self._display)
                if frame.size == (self._width, self._height) and frame.getbbox() is not None:
                    return
            except OSError:
                pass
            time.sleep(0.25)
        raise RuntimeError(f"RDP 已连接但 {_CONNECT_WAIT_S}s 内未收到桌面画面：{host}。")

    @staticmethod
    def _redact(message: str, secret: str = "") -> str:
        if secret:
            message = message.replace(secret, "<redacted>")
        return message.replace("ARGUS_RDP_PASSWORD", "<redacted>")

    def _run_xdotool(self, *args: str) -> None:
        if not self._rdp or self._rdp.poll() is not None:
            raise RuntimeError("RDP 会话已断开，无法发送输入。")
        result = subprocess.run(
            ["xdotool", *args], env={**os.environ, "DISPLAY": self._display},
            capture_output=True, text=True, timeout=10, check=False,
        )
        if result.returncode:
            raise RuntimeError(f"RDP 输入失败：{result.stderr.strip() or result.stdout.strip()}")

    def screenshot_raw(self) -> bytes:
        if not self._rdp or self._rdp.poll() is not None:
            raise RuntimeError("RDP 会话已断开，无法截图。")
        image = ImageGrab.grab(xdisplay=self._display)
        if image.size != (self._width, self._height):
            raise RuntimeError(f"RDP 截图尺寸异常：{image.size}，期望 {(self._width, self._height)}。")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    def screenshot_png(self) -> bytes:
        return self.screenshot_raw()

    @property
    def screen_size(self) -> tuple[int, int]:
        return self._width, self._height

    def tap(self, x: int, y: int) -> None:
        self._run_xdotool("mousemove", "--sync", str(x), str(y), "click", "1")

    def swipe(self, x1: int, y1: int, x2: int, y2: int) -> None:
        self._run_xdotool(
            "mousemove", "--sync", str(x1), str(y1), "mousedown", "1",
            "mousemove", "--sync", "--duration", "400", str(x2), str(y2), "mouseup", "1",
        )

    def scroll_up(self) -> None:
        self._run_xdotool("click", "--repeat", "5", "4")

    def scroll_down(self) -> None:
        self._run_xdotool("click", "--repeat", "5", "5")

    def input_text(self, text: str) -> None:
        if not text:
            return
        if not text.isascii():
            raise RuntimeError(
                "RDP 文本输入当前仅支持 ASCII。请使用 ASCII demo 文本；"
                "Unicode 剪贴板桥接将在后续版本提供。"
            )
        self._run_xdotool("type", "--clearmodifiers", "--delay", "10", text)

    def press_key(self, key: str) -> None:
        mapped = _KEY_MAP.get(str(key).strip().lower())
        if mapped is None:
            raise ValueError(f"RDP 不支持的按键：{key!r}")
        self._run_xdotool("key", "--clearmodifiers", mapped)

    def open_target(self, target: str) -> None:
        if target:
            self._launch_remote_target(target)

    def _launch_remote_target(self, target: str) -> None:
        if not target.isascii() or "\n" in target or "\r" in target:
            raise RuntimeError("RDP_START_PROGRAM 必须是单行 ASCII 程序名或命令。")
        # Windows shortcuts can be intercepted by the local X11 client. Clicking Start
        # and typing invokes Windows search without relying on Super/Meta forwarding.
        self._run_xdotool(
            "mousemove", "--sync", "16", str(max(0, self._height - 16)), "click", "1",
        )
        time.sleep(0.5)
        self._run_xdotool("type", "--clearmodifiers", "--delay", "10", target)
        self._run_xdotool("key", "--clearmodifiers", "Return")
        time.sleep(1.0)

    @property
    def platform_name(self) -> str:
        return "rdp"

    def get_system_prompt_segment(self) -> str:
        return _PROMPT_SEGMENT
