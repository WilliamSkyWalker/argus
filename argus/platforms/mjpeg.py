"""MJPEG 帧流读取器 —— 从 Appium driver 的 mjpegServer 常驻取最新帧。

Appium 的 uiautomator2 / xcuitest driver 都支持 `mjpegServerPort` capability：
起 session 后 driver 在该端口持续推 multipart/x-mixed-replace 的 JPEG 流。
argus 连上这条流、后台线程只保留「最新一帧」，截图时直接取内存里的帧，省掉
`get_screenshot_as_png` 每次的 HTTP 往返 + 设备端现场截图编码（agent 主循环每
step 都要看屏，累计差距被放大几十倍）。

设计：
- 纯 stdlib（urllib + 线程），不引第三方依赖。
- graceful：连不上 / 断流 → latest() 返回 None，调用方 fallback 到原截图路径。
- 断流自动重连（带退避），线程为 daemon，不阻塞退出。
"""

from __future__ import annotations

import threading
import time
import urllib.request

from ..logger import get_logger

log = get_logger("appium.mjpeg")

# JPEG 帧边界标记
_SOI = b"\xff\xd8"   # Start Of Image
_EOI = b"\xff\xd9"   # End Of Image

_READ_CHUNK = 65536
_CONNECT_TIMEOUT = 5


class MjpegFrameReader:
    """后台读 mjpeg 流，只保留最新一帧 JPEG。"""

    def __init__(self, url: str):
        self._url = url
        self._latest: bytes | None = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._first_frame = threading.Event()

    def start(self, wait_first_frame: float = 3.0) -> bool:
        """起后台读帧线程；wait_first_frame 秒内等到第一帧则返回 True。"""
        self._thread = threading.Thread(target=self._run, name="argus-mjpeg", daemon=True)
        self._thread.start()
        got = self._first_frame.wait(timeout=wait_first_frame)
        if got:
            log.info("mjpeg 帧流就绪: %s", self._url)
        else:
            log.warning("mjpeg %ss 内无首帧，本次先走普通截图（后台仍在重试）", wait_first_frame)
        return got

    def latest(self) -> bytes | None:
        """当前最新 JPEG 帧字节，无则 None。"""
        with self._lock:
            return self._latest

    def stop(self) -> None:
        self._stop.set()
        # 线程是 daemon，不 join 太久，避免 teardown 卡住
        t = self._thread
        if t is not None:
            t.join(timeout=1.0)
        self._thread = None

    # --- internal ---

    def _run(self) -> None:
        backoff = 0.5
        while not self._stop.is_set():
            try:
                self._read_stream()
                backoff = 0.5  # 正常读到过就重置退避
            except Exception as e:
                if self._stop.is_set():
                    break
                log.debug("mjpeg 流中断，%.1fs 后重连: %s", backoff, e)
                self._stop.wait(backoff)
                backoff = min(backoff * 2, 5.0)

    def _read_stream(self) -> None:
        req = urllib.request.Request(self._url)
        with urllib.request.urlopen(req, timeout=_CONNECT_TIMEOUT) as resp:
            buf = b""
            while not self._stop.is_set():
                chunk = resp.read(_READ_CHUNK)
                if not chunk:
                    raise IOError("mjpeg 流 EOF")
                buf += chunk
                # 从 buffer 里切出完整 JPEG 帧（可能一次 read 含多帧，只留最后一帧）
                while True:
                    soi = buf.find(_SOI)
                    if soi < 0:
                        # 未见帧头，防 buffer 无限增长
                        if len(buf) > 4 * 1024 * 1024:
                            buf = buf[-_READ_CHUNK:]
                        break
                    eoi = buf.find(_EOI, soi + 2)
                    if eoi < 0:
                        # 帧未收完，保留从 SOI 起的部分等后续 chunk
                        buf = buf[soi:]
                        break
                    frame = buf[soi:eoi + 2]
                    buf = buf[eoi + 2:]
                    with self._lock:
                        self._latest = frame
                    if not self._first_frame.is_set():
                        self._first_frame.set()
