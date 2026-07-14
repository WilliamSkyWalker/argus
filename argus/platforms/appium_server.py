"""Appium server 生命周期管理 —— 让 argus 自己拉起/复用 Appium server，
不依赖人工在终端手动 `appium` 起服务。

职责：
  - 定位 appium 二进制（优先 config/env，其次 PATH，最后扫 nvm 各 node 版本挑最高版）
  - 起服务时把「appium 所在 node 的 bin 目录」放到 PATH 最前，保证 appium 的
    `#!/usr/bin/env node` shebang 用的是装了 appium 的那个 node（避开机器上多 node
    版本共存时 shebang 命中错误 node 的坑）
  - 自动注入 ANDROID_HOME / ANDROID_SDK_ROOT（uiautomator2 driver 必需）
  - 已有 server 在跑 → 直接复用（不重复起、退出也不关）；自己起的 → 退出时关

用法：
    mgr = AppiumServerManager(server_url, config_appium)
    url = mgr.ensure_running()      # 返回可用的 server url
    ...
    mgr.stop()                      # 若是自己起的才真正关
"""

import glob
import os
import re
import shutil
import subprocess
import time
import urllib.request
from urllib.parse import urlparse

from ..logger import get_logger

log = get_logger("appium.server")

_READY_TIMEOUT = 60         # 等 server /status 就绪的秒数
_SPAWN_LOG = "/tmp/argus-appium-server.log"


def _parse_node_version(path: str) -> tuple:
    """从 ~/.nvm/versions/node/vX.Y.Z/bin/appium 解析出 (X,Y,Z) 供排序，失败返回 (0,)。"""
    m = re.search(r"/node/v(\d+)\.(\d+)\.(\d+)/", path)
    return tuple(int(g) for g in m.groups()) if m else (0,)


def find_appium_binary(explicit: str | None = None) -> str | None:
    """定位 appium 可执行文件。

    顺序：显式配置 → APPIUM_BIN 环境变量 → PATH（which）→ 扫 nvm 各 node 版本，
    挑版本号最高、且真装了 appium 的那个（多 node 共存时 which 常命中没装 appium 的默认 node）。
    """
    for cand in (explicit, os.environ.get("APPIUM_BIN")):
        if cand and os.path.isfile(cand):
            return cand
    which = shutil.which("appium")
    if which:
        return which
    nvm_bins = glob.glob(os.path.expanduser("~/.nvm/versions/node/*/bin/appium"))
    if nvm_bins:
        return sorted(nvm_bins, key=_parse_node_version)[-1]
    return None


def _default_android_home() -> str | None:
    for cand in (
        os.environ.get("ANDROID_HOME"),
        os.environ.get("ANDROID_SDK_ROOT"),
        os.path.expanduser("~/Library/Android/sdk"),   # macOS
        os.path.expanduser("~/Android/Sdk"),           # Linux
    ):
        if cand and os.path.isdir(cand):
            return cand
    return None


class AppiumServerManager:
    def __init__(self, server_url: str, cfg: dict | None = None):
        self._url = server_url.rstrip("/")
        self._cfg = cfg or {}
        self._proc: subprocess.Popen | None = None
        self._owned = False   # True 仅当本进程亲手起的 server

    def _status_ok(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self._url}/status", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def ensure_running(self) -> str:
        """确保 server 可用，返回 server url。已在跑则复用，否则自己拉起。"""
        if self._cfg.get("auto_start", True) is False:
            if not self._status_ok():
                raise RuntimeError(
                    f"Appium server 未运行且 auto_start=false：{self._url}\n"
                    f"  请先手动启动 appium，或把 appium.auto_start 设为 true")
            log.info("复用已运行的 Appium server: %s", self._url)
            return self._url

        if self._status_ok():
            log.info("复用已运行的 Appium server: %s", self._url)
            return self._url

        appium_bin = find_appium_binary(self._cfg.get("binary"))
        if not appium_bin:
            raise RuntimeError(
                "找不到 appium 可执行文件。装法：\n"
                "  npm install -g appium && appium driver install uiautomator2 xcuitest\n"
                "  或在 config appium.binary / 环境变量 APPIUM_BIN 指定路径")

        parsed = urlparse(self._url)
        port = str(parsed.port or 4723)
        host = parsed.hostname or "127.0.0.1"

        env = os.environ.copy()
        # 关键：把 appium 所在 node 的 bin 放 PATH 最前，令 shebang 命中对的 node
        node_bin_dir = os.path.dirname(appium_bin)
        env["PATH"] = node_bin_dir + os.pathsep + env.get("PATH", "")
        # uiautomator2 driver 必需 ANDROID_HOME
        android_home = self._cfg.get("android_home") or _default_android_home()
        if android_home:
            env["ANDROID_HOME"] = android_home
            env["ANDROID_SDK_ROOT"] = android_home
        else:
            log.warning("未找到 Android SDK（ANDROID_HOME）——安卓 session 会失败；iOS 不受影响")

        log.info("启动 Appium server: %s (node bin=%s, ANDROID_HOME=%s)",
                 appium_bin, node_bin_dir, android_home or "<none>")
        logf = open(self._cfg.get("log_path", _SPAWN_LOG), "ab")
        self._proc = subprocess.Popen(
            [appium_bin, "--address", host, "--port", port, "--log-level", "info:info"],
            stdout=logf, stderr=logf, env=env,
            start_new_session=True,   # 脱离 argus 进程组，避免信号误杀/被杀
        )
        self._owned = True

        deadline = time.time() + _READY_TIMEOUT
        while time.time() < deadline:
            if self._proc.poll() is not None:
                raise RuntimeError(
                    f"Appium server 启动即退出（code={self._proc.returncode}），见日志 "
                    f"{self._cfg.get('log_path', _SPAWN_LOG)}")
            if self._status_ok():
                log.info("Appium server 就绪: %s", self._url)
                return self._url
            time.sleep(1)
        self.stop()
        raise RuntimeError(f"Appium server {self._READY_TIMEOUT}s 内未就绪: {self._url}")

    def stop(self) -> None:
        """仅关闭本进程亲手起的 server；复用的外部 server 不动。"""
        if not self._owned or self._proc is None:
            return
        log.info("关闭 argus 起的 Appium server (pid=%s)", self._proc.pid)
        try:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        except Exception as e:
            log.debug("关闭 Appium server 失败: %s", e)
        finally:
            self._proc = None
            self._owned = False
