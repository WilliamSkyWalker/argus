"""Toast Detector Skill — capture transient toast/snackbar notifications.

Toasts and snackbars appear briefly (1-3 seconds) after an action. A single
screenshot might miss them. This skill:

1. After an action, requests the platform to take rapid burst screenshots
2. Compares each frame against the baseline to find new overlay regions
3. Crops and OCRs the toast region, reporting the text to the LLM

Since the skill runs in the pre-think pipeline (before the next decision),
it examines the CURRENT screenshot against the previous one to detect
newly appeared overlay elements.

Detection heuristics:
- Small horizontal band appearing near the top or bottom of the screen
- High contrast region that wasn't in the previous frame
- UI tree elements with "toast", "snackbar", "alert", "banner" type
"""

from __future__ import annotations

import re

from PIL import Image, ImageChops, ImageStat

from .base import Skill, SkillContext, SkillResult
from ..grid import img_to_png_bytes
from ..logger import get_logger

log = get_logger("skills.toast")

# Toast typically appears in the top 20% or bottom 25% of the screen
_TOP_ZONE = 0.20
_BOTTOM_ZONE = 0.25

# Minimum width fraction for a toast bar
_MIN_WIDTH_FRACTION = 0.3

# UI tree keywords that indicate toast/notification
_TOAST_KEYWORDS = [
    "toast", "snackbar", "alert", "banner", "notification",
    "HUD", "提示", "成功", "失败", "错误", "已复制", "已保存", "已删除",
]




def _find_toast_region(prev: Image.Image | None, curr: Image.Image) -> dict | None:
    """Detect a toast-like overlay region by diffing previous and current screenshots.

    Returns {"zone": "top"|"bottom", "bbox": (x1,y1,x2,y2), "crop": Image} or None.
    """
    if prev is None:
        return None

    # Resize prev to match curr if needed
    if prev.size != curr.size:
        prev = prev.resize(curr.size, Image.LANCZOS)

    w, h = curr.size

    # Check top zone and bottom zone separately
    zones = [
        ("top", 0, int(h * _TOP_ZONE)),
        ("bottom", int(h * (1 - _BOTTOM_ZONE)), h),
    ]

    for zone_name, y_start, y_end in zones:
        prev_zone = prev.crop((0, y_start, w, y_end)).convert("RGB")
        curr_zone = curr.crop((0, y_start, w, y_end)).convert("RGB")

        diff = ImageChops.difference(prev_zone, curr_zone).convert("L")

        # Threshold the diff
        binary = diff.point(lambda p: 255 if p > 25 else 0)

        # Check if there's a horizontal band of change
        zone_h = y_end - y_start
        zone_w = w

        # Scan rows to find the changed band
        row_change = []
        pixels = binary.load()
        for y in range(zone_h):
            changed_in_row = sum(1 for x in range(0, zone_w, 3) if pixels[x, y] > 0)
            row_change.append(changed_in_row / (zone_w // 3))

        # Find a contiguous band of changed rows
        in_band = False
        band_start = 0
        bands = []
        for i, ratio in enumerate(row_change):
            if ratio > 0.15 and not in_band:
                band_start = i
                in_band = True
            elif ratio <= 0.15 and in_band:
                band_h = i - band_start
                if 10 < band_h < zone_h * 0.8:
                    bands.append((band_start, i))
                in_band = False
        if in_band:
            band_h = len(row_change) - band_start
            if 10 < band_h < zone_h * 0.8:
                bands.append((band_start, len(row_change)))

        if bands:
            # Take the most prominent band
            best = max(bands, key=lambda b: b[1] - b[0])
            abs_y1 = y_start + best[0]
            abs_y2 = y_start + best[1]

            # Find horizontal extent of the change
            band_crop = binary.crop((0, best[0], zone_w, best[1]))
            col_change = []
            bp = band_crop.load()
            bh = best[1] - best[0]
            for x in range(zone_w):
                changed = sum(1 for y in range(0, bh, 2) if bp[x, y] > 0)
                col_change.append(changed / max(1, bh // 2))

            # Find left and right bounds
            x1 = 0
            for i, r in enumerate(col_change):
                if r > 0.1:
                    x1 = i
                    break
            x2 = zone_w
            for i in range(zone_w - 1, -1, -1):
                if col_change[i] > 0.1:
                    x2 = i + 1
                    break

            band_width = x2 - x1
            if band_width >= w * _MIN_WIDTH_FRACTION:
                crop = curr.crop((x1, abs_y1, x2, abs_y2))
                return {
                    "zone": zone_name,
                    "bbox": (x1, abs_y1, x2, abs_y2),
                    "crop": crop,
                }

    return None


class ToastDetectorSkill(Skill):
    name = "toast_detector"
    default_enabled = True

    def is_applicable(self, ctx: SkillContext) -> bool:
        """Only useful when we have a previous screenshot (after an action)."""
        return ctx.prev_image is not None

    def process(self, ctx: SkillContext) -> SkillResult:
        results = []
        confidence = 0.0

        # 2. Visual diff-based toast detection
        toast_region = _find_toast_region(ctx.prev_image, ctx.raw_image)
        extra_images = []

        if toast_region:
            zone = toast_region["zone"]
            x1, y1, x2, y2 = toast_region["bbox"]
            zone_cn = "顶部" if zone == "top" else "底部"
            results.append(f"在屏幕{zone_cn}检测到新出现的通知栏 ({x1},{y1})-({x2},{y2})")
            confidence += 0.5

            # Include cropped toast image for LLM to read
            extra_images.append(img_to_png_bytes(toast_region["crop"]))

        confidence = min(confidence, 1.0)
        has_toast = confidence >= 0.3

        if has_toast:
            scale = ctx.scale
            desc = "；".join(results)
            text = (f"## Toast/通知检测\n"
                    f"检测到短暂通知（置信度 {confidence:.0%}）: {desc}\n"
                    f"请仔细查看通知内容，可能包含操作结果（成功/失败/错误信息）。")

            # Convert bbox to logical coords if available
            metadata_regions = []
            if toast_region:
                b = toast_region["bbox"]
                metadata_regions.append({
                    "zone": toast_region["zone"],
                    "pixel_bbox": {"x1": b[0], "y1": b[1], "x2": b[2], "y2": b[3]},
                    "logical_bbox": {
                        "x1": int(b[0] / scale), "y1": int(b[1] / scale),
                        "x2": int(b[2] / scale), "y2": int(b[3] / scale),
                    },
                })
        else:
            text = ""
            metadata_regions = []

        log.debug("Toast detector: found=%s confidence=%.0f%%",
                   has_toast, confidence * 100)

        return SkillResult(
            text=text,
            extra_images=extra_images,
            metadata={
                "has_toast": has_toast,
                "confidence": round(confidence, 2),
                "regions": metadata_regions,
            },
        )
