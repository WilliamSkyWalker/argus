"""Touch/input control via idb (iOS Development Bridge) — no WDA needed."""

import base64
import json
import os
import subprocess
import tempfile
import time

from PIL import Image

from .grid import (
    draw_coordinate_grid as _draw_grid,
    draw_region_grid as _draw_region_grid,
    img_to_png_bytes as _img_to_png_bytes,
    load_font as _load_font,
)


def _parse_json_from_llm(raw: str) -> dict:
    """Extract JSON from LLM response, handling markdown fences."""
    text = raw.strip()
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0]
    text = text.strip()
    if not text.startswith("{"):
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1:
            text = text[start:end + 1]
    return json.loads(text)


class Hands:
    """Controls the simulator via idb — tap, swipe, type, read UI."""

    def __init__(self, udid: str = "booted"):
        self.udid = udid
        self._window_size: tuple[int, int] | None = None
        self._scale: float | None = None

    def _idb(self, *args: str, timeout: int = 30) -> str:
        # timeout 防 idb_companion 卡死拖挂整个 run；截图类调用传更长的 timeout=60
        result = subprocess.run(
            ["idb", *args, "--udid", self.udid],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"idb {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    @property
    def window_size(self) -> tuple[int, int]:
        if self._window_size is None:
            raw = self._idb("ui", "describe-all", "--json")
            nodes = json.loads(raw)
            if nodes:
                frame = nodes[0].get("frame", {})
                w = int(frame.get("width", 402))
                h = int(frame.get("height", 874))
                self._window_size = (w, h)
            else:
                self._window_size = (402, 874)
        return self._window_size

    @property
    def scale(self) -> float:
        """Detect Retina scale factor (raw pixels / logical points)."""
        if self._scale is None:
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
                path = f.name
            try:
                self._idb("screenshot", path, timeout=60)
                img = Image.open(path)
                img.load()  # 立即读入像素，之后可安全删除底层文件
                w, _ = self.window_size
                self._scale = img.width / w
            finally:
                try:
                    os.remove(path)
                except OSError:
                    pass
        return self._scale

    # ------------------------------------------------------------------
    # Basic actions
    # ------------------------------------------------------------------

    def open_app(self, bundle_id: str) -> None:
        self._idb("launch", bundle_id)

    def tap(self, x: int, y: int) -> None:
        self._idb("ui", "tap", str(x), str(y))

    def swipe(self, x1: int, y1: int, x2: int, y2: int, duration: float = 0.5) -> None:
        self._idb("ui", "swipe", str(x1), str(y1), str(x2), str(y2),
                   "--duration", str(duration))

    def swipe_up(self) -> None:
        w, h = self.window_size
        cx = w // 2
        self.swipe(cx, h * 3 // 4, cx, h // 4)

    def swipe_down(self) -> None:
        w, h = self.window_size
        cx = w // 2
        self.swipe(cx, h // 4, cx, h * 3 // 4)

    def _simctl(self, *args: str, timeout: int = 30) -> str:
        result = subprocess.run(
            ["xcrun", "simctl", *args, self.udid],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            raise RuntimeError(f"simctl {' '.join(args)} failed: {result.stderr.strip()}")
        return result.stdout

    # ------------------------------------------------------------------
    # Text input
    # ------------------------------------------------------------------

    def _paste_text(self, text: str) -> None:
        """Set simulator pasteboard, then long-press the focused field to trigger iOS paste menu."""
        subprocess.run(
            ["xcrun", "simctl", "pbcopy", self.udid],
            input=text, text=True, check=True,
        )
        raw = self._idb("ui", "describe-all", "--json")
        nodes = json.loads(raw)
        target = None
        for node in nodes:
            if node.get("type") in ("TextField", "SearchField", "TextView") and node.get("enabled"):
                target = node
                break
        if target:
            frame = target["frame"]
            cx = int(frame["x"] + frame["width"] / 2)
            cy = int(frame["y"] + frame["height"] / 2)
        else:
            w, h = self.window_size
            cx, cy = w // 2, h // 2
        self._idb("ui", "tap", str(cx), str(cy), "--duration", "1.5")
        time.sleep(0.8)
        raw = self._idb("ui", "describe-all", "--json")
        nodes = json.loads(raw)
        for node in nodes:
            label = node.get("AXLabel") or ""
            if label in ("Paste", "粘贴", "Paste and Go", "粘贴并前往"):
                frame = node["frame"]
                px = int(frame["x"] + frame["width"] / 2)
                py = int(frame["y"] + frame["height"] / 2)
                self._idb("ui", "tap", str(px), str(py))
                return
        subprocess.run(
            ["osascript", "-e", 'tell application "Simulator" to activate'],
            check=True,
        )
        time.sleep(0.3)
        subprocess.run(
            ["osascript", "-e",
             'tell application "System Events" to keystroke "v" using command down'],
            check=True,
        )

    def input_text(self, text: str) -> None:
        """Type text into the currently focused field. Supports CJK via clipboard paste."""
        if text.isascii():
            self._idb("ui", "text", text)
        else:
            self._paste_text(text)

    def press_key(self, key: str) -> None:
        key_map = {
            "enter": "40",
            "delete": "42",
            "tab": "43",
            "space": "44",
            "escape": "41",
        }
        code = key_map.get(key, key)
        self._idb("ui", "key", code)

    # ------------------------------------------------------------------
    # UI tree helpers
    # ------------------------------------------------------------------

    def tap_element(self, name: str) -> bool:
        """Find an element by accessibility label and tap it."""
        raw = self._idb("ui", "describe-all", "--json")
        nodes = json.loads(raw)
        for node in nodes:
            if node.get("AXLabel") == name:
                frame = node["frame"]
                cx = int(frame["x"] + frame["width"] / 2)
                cy = int(frame["y"] + frame["height"] / 2)
                self.tap(cx, cy)
                return True
        return False

    def get_ui_tree(self) -> str:
        """Get the current UI hierarchy as JSON string."""
        return self._idb("ui", "describe-all", "--json")

    # ------------------------------------------------------------------
    # Screenshots
    # ------------------------------------------------------------------

    def _take_raw_screenshot(self) -> Image.Image:
        """Take a raw screenshot and return as PIL Image (no grid)."""
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            path = f.name
        try:
            self._idb("screenshot", path, timeout=60)
            img = Image.open(path)
            img.load()  # 立即读入像素，之后可安全删除底层文件（不删会每 turn 泄漏一个 PNG）
            return img
        finally:
            try:
                os.remove(path)
            except OSError:
                pass

    def screenshot_png(self) -> bytes:
        """Take a high-res screenshot with coordinate grid overlay."""
        img = self._take_raw_screenshot()
        lw, lh = self.window_size
        img = _draw_grid(img, self.scale, lw, lh)
        return _img_to_png_bytes(img)

    def screenshot_region(self, lx: int, ly: int, lw: int, lh: int) -> bytes:
        """Take a screenshot and crop to a logical region with dense grid.

        Args:
            lx, ly: top-left corner in logical coords
            lw, lh: width and height in logical coords

        Returns PNG bytes with a 5pt/10pt grid labeled in absolute logical coords.
        """
        img = self._take_raw_screenshot()
        s = self.scale
        # Crop in raw pixels
        x1 = max(0, int(lx * s))
        y1 = max(0, int(ly * s))
        x2 = min(img.width, int((lx + lw) * s))
        y2 = min(img.height, int((ly + lh) * s))
        cropped = img.crop((x1, y1, x2, y2))
        cropped = _draw_region_grid(cropped, s, lx, ly, lx + lw, ly + lh)
        return _img_to_png_bytes(cropped)

    # ------------------------------------------------------------------
    # Smart tap — two-phase LLM-guided click
    # ------------------------------------------------------------------

    def tap_screen(self, x: int, y: int) -> bytes:
        """Take a fresh screenshot, then immediately tap.

        Returns the screenshot bytes taken right before the tap.
        """
        screenshot = self.screenshot_png()
        self.tap(x, y)
        return screenshot

    def find_and_tap(self, description: str) -> dict:
        """Two-phase LLM-guided tap for maximum accuracy.

        Phase 1: Send full screenshot (with grid) to LLM for coarse location.
        Phase 2: Crop a 120x120 logical-pt region around the coarse location,
                 draw a dense 5pt grid with labels, send to LLM for precise coords.
        Then tap at the precise coords.

        Returns dict with: before_screenshot, after_screenshot, x, y
        """
        from openai import OpenAI
        from .config import load_config

        cfg = load_config()["llm"]
        client = OpenAI(api_key=cfg["api_key"], base_url=cfg["base_url"])
        model = cfg["model"]
        lw, lh = self.window_size
        s = self.scale

        # --- Phase 1: coarse location from full screenshot ---
        raw_img = self._take_raw_screenshot()
        full_grid = _draw_grid(raw_img.copy(), s, lw, lh)
        full_b64 = base64.standard_b64encode(_img_to_png_bytes(full_grid)).decode()

        coarse_resp = client.chat.completions.create(
            model=model, max_tokens=256,
            messages=[
                {"role": "system", "content": (
                    "你是视觉定位助手。截图上有红色坐标网格（细线每10点，中线每25点，粗线+标签每50点）。"
                    "返回目标元素中心的大致逻辑坐标。只返回 JSON: {\"x\": int, \"y\": int}"
                )},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{full_b64}"}},
                    {"type": "text", "text": f"屏幕逻辑尺寸 {lw}x{lh}。找到：{description}"},
                ]},
            ],
        )
        coarse = _parse_json_from_llm(coarse_resp.choices[0].message.content)
        cx, cy = int(coarse["x"]), int(coarse["y"])

        # --- Phase 2: precise location from zoomed crop ---
        margin = 60  # logical pts around coarse center
        crop_lx = max(0, cx - margin)
        crop_ly = max(0, cy - margin)
        crop_lx2 = min(lw, cx + margin)
        crop_ly2 = min(lh, cy + margin)

        # Crop from the SAME raw image (no re-screenshot, no scroll drift)
        rx1 = int(crop_lx * s)
        ry1 = int(crop_ly * s)
        rx2 = int(crop_lx2 * s)
        ry2 = int(crop_ly2 * s)
        cropped = raw_img.crop((rx1, ry1, rx2, ry2))
        cropped = _draw_region_grid(cropped, s, crop_lx, crop_ly, crop_lx2, crop_ly2)
        crop_b64 = base64.standard_b64encode(_img_to_png_bytes(cropped)).decode()

        precise_resp = client.chat.completions.create(
            model=model, max_tokens=256,
            messages=[
                {"role": "system", "content": (
                    "你是视觉定位助手。这是一张放大的局部截图，蓝色网格每5点一条线，标签每10点。"
                    "网格上的数字是绝对逻辑坐标。返回目标元素中心的精确坐标。"
                    "只返回 JSON: {\"x\": int, \"y\": int}"
                )},
                {"role": "user", "content": [
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{crop_b64}"}},
                    {"type": "text", "text": (
                        f"这是屏幕 ({crop_lx},{crop_ly}) 到 ({crop_lx2},{crop_ly2}) 区域的放大图。"
                        f"找到：{description}。返回精确的绝对逻辑坐标。"
                    )},
                ]},
            ],
        )
        precise = _parse_json_from_llm(precise_resp.choices[0].message.content)
        tx, ty = int(precise["x"]), int(precise["y"])

        # Clamp to screen bounds
        tx = max(0, min(tx, lw))
        ty = max(0, min(ty, lh))

        # Tap
        self.tap(tx, ty)

        # Verification screenshot
        time.sleep(0.5)
        after_png = self.screenshot_png()

        return {
            "before_screenshot": _img_to_png_bytes(full_grid),
            "after_screenshot": after_png,
            "coarse": (cx, cy),
            "precise": (tx, ty),
            "x": tx,
            "y": ty,
        }
