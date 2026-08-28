"""Color Validator Skill — analyze color scheme and visual style of the screen.

Useful for verifying UI style requirements like "dark background", "gold accent",
"safety status colored". Provides structured color information to the LLM:

1. Dominant colors and their coverage
2. Whether the screen uses a dark or light theme
3. Accent color detection
4. Color contrast analysis for key regions
"""

from __future__ import annotations

from collections import Counter

from PIL import Image

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills.color")


def _rgb_to_hsl(r: int, g: int, b: int) -> tuple[float, float, float]:
    """Convert RGB (0-255) to HSL (h: 0-360, s: 0-1, l: 0-1)."""
    r, g, b = r / 255, g / 255, b / 255
    mx, mn = max(r, g, b), min(r, g, b)
    l = (mx + mn) / 2

    if mx == mn:
        h = s = 0.0
    else:
        d = mx - mn
        s = d / (2 - mx - mn) if l > 0.5 else d / (mx + mn)
        if mx == r:
            h = (g - b) / d + (6 if g < b else 0)
        elif mx == g:
            h = (b - r) / d + 2
        else:
            h = (r - g) / d + 4
        h /= 6

    return h * 360, s, l


def _color_name(r: int, g: int, b: int) -> str:
    """Approximate human-readable color name in Chinese."""
    h, s, l = _rgb_to_hsl(r, g, b)

    if l < 0.12:
        return "黑色"
    if l > 0.92:
        return "白色"
    if s < 0.10:
        if l < 0.4:
            return "深灰"
        if l < 0.7:
            return "灰色"
        return "浅灰"

    # Chromatic
    if h < 15 or h >= 345:
        return "红色"
    if h < 40:
        return "橙色"
    if h < 65:
        return "黄色/金色"
    if h < 150:
        return "绿色"
    if h < 200:
        return "青色"
    if h < 260:
        return "蓝色"
    if h < 310:
        return "紫色"
    return "粉色"


def _extract_dominant_colors(image: Image.Image, n_colors: int = 8) -> list[dict]:
    """Extract dominant colors by quantizing the image."""
    # Resize for speed
    thumb = image.copy()
    thumb.thumbnail((100, 100))
    thumb = thumb.convert("RGB")

    # Quantize to n_colors
    quantized = thumb.quantize(colors=n_colors, method=Image.Quantize.MEDIANCUT)
    palette = quantized.getpalette()
    if palette is None:
        return []

    # Count pixels per color index
    pixel_counts = Counter(quantized.getdata())
    total_pixels = thumb.size[0] * thumb.size[1]

    colors = []
    for idx, count in pixel_counts.most_common(n_colors):
        if idx * 3 + 2 >= len(palette):
            continue
        r, g, b = palette[idx * 3], palette[idx * 3 + 1], palette[idx * 3 + 2]
        fraction = count / total_pixels
        if fraction < 0.02:
            continue
        colors.append({
            "rgb": (r, g, b),
            "hex": f"#{r:02x}{g:02x}{b:02x}",
            "name": _color_name(r, g, b),
            "fraction": round(fraction, 3),
        })

    return colors


def _detect_theme(colors: list[dict]) -> str:
    """Determine if the overall theme is dark or light."""
    if not colors:
        return "unknown"

    # Weight by coverage fraction
    weighted_lightness = 0.0
    total_weight = 0.0
    for c in colors:
        r, g, b = c["rgb"]
        _, _, l = _rgb_to_hsl(r, g, b)
        weighted_lightness += l * c["fraction"]
        total_weight += c["fraction"]

    avg_l = weighted_lightness / total_weight if total_weight > 0 else 0.5

    if avg_l < 0.3:
        return "深色主题"
    if avg_l > 0.7:
        return "浅色主题"
    return "混合主题"


def _find_accent_colors(colors: list[dict]) -> list[dict]:
    """Find accent colors (high saturation, relatively low coverage)."""
    accents = []
    for c in colors:
        r, g, b = c["rgb"]
        h, s, l = _rgb_to_hsl(r, g, b)
        # Accent: saturated, not too dark/light, not dominant
        if s > 0.4 and 0.15 < l < 0.85 and c["fraction"] < 0.3:
            accents.append({**c, "saturation": round(s, 2)})
    return accents


def _sample_region_color(image: Image.Image, region: str,
                          w: int, h: int) -> dict:
    """Sample average color from a named screen region."""
    regions = {
        "top_bar": (0, 0, w, int(h * 0.08)),
        "center": (int(w * 0.2), int(h * 0.3), int(w * 0.8), int(h * 0.7)),
        "bottom_bar": (0, int(h * 0.92), w, h),
        "background": (int(w * 0.1), int(h * 0.1), int(w * 0.9), int(h * 0.9)),
    }

    box = regions.get(region)
    if not box:
        return {}

    crop = image.crop(box).convert("RGB")
    thumb = crop.copy()
    thumb.thumbnail((20, 20))

    pixels = list(thumb.getdata())
    if not pixels:
        return {}

    avg_r = sum(p[0] for p in pixels) // len(pixels)
    avg_g = sum(p[1] for p in pixels) // len(pixels)
    avg_b = sum(p[2] for p in pixels) // len(pixels)

    return {
        "region": region,
        "rgb": (avg_r, avg_g, avg_b),
        "hex": f"#{avg_r:02x}{avg_g:02x}{avg_b:02x}",
        "name": _color_name(avg_r, avg_g, avg_b),
    }


class ColorValidatorSkill(Skill):
    name = "color_validator"
    default_enabled = False  # Only useful for style-verification test cases

    def process(self, ctx: SkillContext) -> SkillResult:
        image = ctx.raw_image.convert("RGB")
        w, h = ctx.screen_size
        scale = ctx.scale

        # 1. Extract dominant colors
        colors = _extract_dominant_colors(image)

        # 2. Detect theme
        theme = _detect_theme(colors)

        # 3. Find accent colors
        accents = _find_accent_colors(colors)

        # 4. Sample key regions
        regions = {}
        for region_name in ["top_bar", "center", "bottom_bar"]:
            info = _sample_region_color(image, region_name,
                                         image.size[0], image.size[1])
            if info:
                regions[region_name] = info

        # Build report text
        lines = [f"## 色彩分析", f"整体主题: {theme}\n"]

        lines.append("主要颜色:")
        for c in colors[:5]:
            lines.append(f"  - {c['name']} ({c['hex']}) 占比 {c['fraction']:.0%}")

        if accents:
            lines.append("\n强调色:")
            for a in accents[:3]:
                lines.append(f"  - {a['name']} ({a['hex']}) 饱和度 {a['saturation']:.0%}")

        if regions:
            lines.append("\n区域颜色:")
            region_names_cn = {"top_bar": "顶栏", "center": "中央", "bottom_bar": "底栏"}
            for rn, info in regions.items():
                cn = region_names_cn.get(rn, rn)
                lines.append(f"  - {cn}: {info['name']} ({info['hex']})")

        text = "\n".join(lines)

        log.debug("Color analysis: theme=%s, %d colors, %d accents",
                   theme, len(colors), len(accents))

        return SkillResult(
            text=text,
            metadata={
                "theme": theme,
                "dominant_colors": colors,
                "accent_colors": accents,
                "regions": regions,
            },
        )
