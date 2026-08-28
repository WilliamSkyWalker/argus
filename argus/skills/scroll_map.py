"""Scroll Map Skill — determine viewport position within a scrollable page.

Tells the LLM where in the page the current viewport is, whether there's
content above/below, and approximately how far scrolled. This prevents the
agent from getting stuck when the target element is off-screen.

Detection methods:
1. UI tree: look for scroll view indicators (contentOffset, scrollable)
2. Browser: inject JS to read scrollTop / scrollHeight (via platform)
3. Visual: detect scrollbar position from the screenshot
4. Edge analysis: check if top/bottom edges have content continuity
"""

from __future__ import annotations

import json
import re

from PIL import Image

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills.scroll")


def _detect_scrollbar_visual(image: Image.Image) -> dict | None:
    """Detect scrollbar indicator from the right edge of the screenshot.

    iOS/Android scrollbars are thin dark/gray strips on the right edge.
    Returns {"position": 0.0-1.0, "visible_fraction": 0.0-1.0} or None.
    """
    img = image.convert("L")
    w, h = img.size

    # Sample the rightmost 8px strip
    strip_width = 8
    x_start = w - strip_width

    # For each row, check if there's a dark pixel in the right strip
    # that contrasts with the background
    pixels = img.load()
    bg_samples = []
    strip_values = []

    for y in range(0, h, 2):
        # Background: sample 20px to the left of the strip
        bg_x = max(0, x_start - 20)
        bg_val = pixels[bg_x, y]
        bg_samples.append(bg_val)

        # Strip: darkest pixel in the strip
        min_val = 255
        for x in range(x_start, w):
            min_val = min(min_val, pixels[x, y])
        strip_values.append(min_val)

    if not bg_samples:
        return None

    avg_bg = sum(bg_samples) / len(bg_samples)

    # Find rows where strip is significantly darker than background
    threshold = max(30, avg_bg * 0.5)
    scrollbar_rows = []
    for i, (strip_val, bg_val) in enumerate(zip(strip_values, bg_samples)):
        if strip_val < threshold and (bg_val - strip_val) > 30:
            scrollbar_rows.append(i)

    if len(scrollbar_rows) < 5:
        return None

    # Find contiguous scrollbar region
    first = scrollbar_rows[0]
    last = scrollbar_rows[-1]
    bar_center = (first + last) / 2
    bar_length = last - first + 1

    total_rows = len(strip_values)
    position = bar_center / total_rows
    visible_fraction = bar_length / total_rows

    return {
        "position": round(position, 2),
        "visible_fraction": round(visible_fraction, 2),
    }


def _check_edge_content(image: Image.Image) -> dict:
    """Check if top and bottom edges suggest content continuity.

    If the bottom edge is mid-content (not a footer/blank), there's likely more below.
    """
    img = image.convert("RGB")
    w, h = img.size

    # Sample bottom 5 rows
    bottom_strip = img.crop((0, h - 10, w, h))
    from PIL import ImageStat
    bottom_stat = ImageStat.Stat(bottom_strip)
    bottom_std = sum(bottom_stat.stddev) / 3

    # Sample top 5 rows
    top_strip = img.crop((0, 0, w, 10))
    top_stat = ImageStat.Stat(top_strip)
    top_std = sum(top_stat.stddev) / 3

    # High variance at edge = content is cut off = more content in that direction
    return {
        "likely_more_above": top_std > 20,
        "likely_more_below": bottom_std > 20,
    }


class ScrollMapSkill(Skill):
    name = "scroll_map"
    default_enabled = True

    def process(self, ctx: SkillContext) -> SkillResult:
        # 纯视觉滚动位置估计（不再解析 UI 树）
        scroll_info = None

        # Visual scrollbar detection
        if scroll_info is None:
            scrollbar = _detect_scrollbar_visual(ctx.raw_image)
            if scrollbar:
                scroll_info = {**scrollbar, "source": "visual_scrollbar"}

        # 3. Edge analysis (always available as fallback)
        edges = _check_edge_content(ctx.raw_image)

        if scroll_info is None and (edges["likely_more_above"] or edges["likely_more_below"]):
            scroll_info = {
                "position": 0.5 if edges["likely_more_above"] and edges["likely_more_below"]
                else (0.8 if edges["likely_more_above"] else 0.2),
                "visible_fraction": None,
                "source": "edge_analysis",
            }

        if scroll_info is None:
            # Likely a non-scrollable or fully visible page
            return SkillResult(
                text="",
                metadata={"scrollable": False},
            )

        # Build description
        pos = scroll_info.get("position", 0.5)
        vis = scroll_info.get("visible_fraction")

        if pos < 0.1:
            pos_desc = "顶部（页面开头）"
        elif pos > 0.9:
            pos_desc = "底部（页面末尾）"
        else:
            pos_desc = f"约 {pos:.0%} 位置"

        parts = [f"当前视口在页面{pos_desc}"]

        if vis is not None and vis < 0.95:
            parts.append(f"可见区域占全页 {vis:.0%}")

        if edges["likely_more_above"]:
            parts.append("上方有更多内容")
        if edges["likely_more_below"]:
            parts.append("下方有更多内容")

        # Advice
        if edges["likely_more_below"]:
            parts.append("如果目标元素不在当前视口，可尝试 scroll_down")
        elif edges["likely_more_above"]:
            parts.append("如果目标元素不在当前视口，可尝试 scroll_up")

        text = "## 滚动位置\n" + "；".join(parts) + "。"

        log.debug("Scroll map: position=%.0f%% visible=%.0f%%",
                   pos * 100, (vis or 1.0) * 100)

        return SkillResult(
            text=text,
            metadata={
                "scrollable": True,
                **scroll_info,
                **edges,
            },
        )
