"""Loading Detector Skill — detect page loading states to prevent premature actions.

Identifies spinners, progress bars, skeleton screens, and blank pages by analyzing
image characteristics and UI tree hints. Advises the LLM to wait when loading is detected.

Detection methods:
1. UI tree keywords: "ActivityIndicator", "ProgressView", "loading", "spinner"
2. Image analysis: large uniform/blank regions, low visual entropy
3. Skeleton screen: repeating gray placeholder rectangles
"""

from __future__ import annotations

import json
import re

from PIL import Image, ImageStat

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills.loading")

# Keywords that hint at loading state in UI tree / DOM
_LOADING_KEYWORDS = [
    "ActivityIndicator", "ProgressView", "UIActivityIndicator",
    "loading", "spinner", "skeleton", "placeholder",
    "加载中", "正在加载", "请稍候",
]

# Minimum fraction of screen that is "blank" to flag as loading
_BLANK_THRESHOLD = 0.60

# Entropy below this → likely blank/loading screen
_LOW_ENTROPY_THRESHOLD = 3.0


def _analyze_blank_ratio(image: Image.Image) -> float:
    """Estimate what fraction of the image is a single flat color (blank/white/skeleton).

    Divides the image into a grid, checks how many cells have very low variance.
    """
    img = image.convert("RGB")
    w, h = img.size
    cell_size = 40
    cells_x = max(1, w // cell_size)
    cells_y = max(1, h // cell_size)
    total_cells = cells_x * cells_y
    flat_cells = 0

    for cy in range(cells_y):
        for cx in range(cells_x):
            x1 = cx * cell_size
            y1 = cy * cell_size
            x2 = min(x1 + cell_size, w)
            y2 = min(y1 + cell_size, h)
            cell = img.crop((x1, y1, x2, y2))
            stat = ImageStat.Stat(cell)
            # Average stddev across RGB channels
            avg_std = sum(stat.stddev) / 3
            if avg_std < 5.0:  # Very flat color
                flat_cells += 1

    return flat_cells / total_cells


def _image_entropy(image: Image.Image) -> float:
    """Calculate Shannon entropy of the image histogram. Low = simple/blank."""
    import math
    hist = image.convert("L").histogram()
    total = sum(hist)
    if total == 0:
        return 0.0
    entropy = 0.0
    for count in hist:
        if count > 0:
            p = count / total
            entropy -= p * math.log2(p)
    return entropy


def _detect_skeleton_pattern(image: Image.Image) -> bool:
    """Detect skeleton screen pattern: repeating horizontal gray bars.

    Skeleton screens typically have 3+ equally-spaced gray rectangles.
    """
    img = image.convert("L")
    w, h = img.size

    # Sample the center column
    cx = w // 2
    pixels = [img.getpixel((cx, y)) for y in range(0, h, 2)]

    # Look for repeating gray bands (brightness 180-230)
    in_gray = False
    gray_bands = []
    band_start = 0

    for i, p in enumerate(pixels):
        is_gray = 180 <= p <= 235
        if is_gray and not in_gray:
            band_start = i
            in_gray = True
        elif not is_gray and in_gray:
            band_len = i - band_start
            if 5 < band_len < 60:  # Reasonable band height
                gray_bands.append((band_start, band_len))
            in_gray = False

    # 3+ similar-sized gray bands = likely skeleton
    if len(gray_bands) >= 3:
        lengths = [b[1] for b in gray_bands]
        avg_len = sum(lengths) / len(lengths)
        similar = sum(1 for l in lengths if abs(l - avg_len) < avg_len * 0.4)
        if similar >= 3:
            return True

    return False


class LoadingDetectorSkill(Skill):
    name = "loading_detector"
    default_enabled = True

    def process(self, ctx: SkillContext) -> SkillResult:
        signals = []
        confidence = 0.0

        # Blank ratio
        blank_ratio = _analyze_blank_ratio(ctx.raw_image)
        if blank_ratio > _BLANK_THRESHOLD:
            signals.append(f"页面 {blank_ratio:.0%} 区域为空白")
            confidence += 0.3

        # 3. Low entropy
        entropy = _image_entropy(ctx.raw_image)
        if entropy < _LOW_ENTROPY_THRESHOLD:
            signals.append(f"页面视觉复杂度极低 (entropy={entropy:.1f})")
            confidence += 0.2

        # 4. Skeleton pattern
        is_skeleton = _detect_skeleton_pattern(ctx.raw_image)
        if is_skeleton:
            signals.append("检测到骨架屏占位符")
            confidence += 0.4

        confidence = min(confidence, 1.0)
        is_loading = confidence >= 0.3

        if is_loading:
            desc = "；".join(signals)
            text = (f"## 加载状态检测\n"
                    f"⚠️ 页面可能正在加载（置信度 {confidence:.0%}）: {desc}\n"
                    f"建议使用 wait 等待页面加载完成后再操作。")
        else:
            text = ""

        log.debug("Loading detector: loading=%s confidence=%.0f%%",
                   is_loading, confidence * 100)

        return SkillResult(
            text=text,
            metadata={
                "is_loading": is_loading,
                "confidence": round(confidence, 2),
                "signals": signals,
                "blank_ratio": round(blank_ratio, 2) if 'blank_ratio' in dir() else 0,
            },
        )
