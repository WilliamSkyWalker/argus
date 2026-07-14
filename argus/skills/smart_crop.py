"""Smart Crop Skill — emit multiple zoomed views of likely interest regions.

Each step always sends three strategic zooms (top bar / top-right / bottom),
plus an optional reactive zoom based on visual diff or the last action's target.
Each crop is upscaled and overlaid with a dense local grid so the LLM can read
precise coordinates for small interactive elements (icons, language toggles,
tab bars) that the main screenshot's coarse grid cannot resolve.
"""

from __future__ import annotations

from PIL import Image

from .base import Skill, SkillContext, SkillResult
from ..grid import draw_region_grid, img_to_png_bytes
from ..logger import get_logger

log = get_logger("skills.crop")

# Padding around a reactive region of interest (in logical points)
ROI_PADDING = 40

# Minimum size for any emitted crop (logical points)
MIN_REGION_SIZE = 60

# Default zoom factor for crops
DEFAULT_ZOOM = 2.0


def _zoom_region(ctx: SkillContext, lx1: int, ly1: int, lx2: int, ly2: int,
                 zoom: float = DEFAULT_ZOOM) -> Image.Image | None:
    """Crop the raw image to (lx1,ly1)-(lx2,ly2) logical, upscale, draw dense grid."""
    w, h = ctx.screen_size
    lx1 = max(0, lx1); ly1 = max(0, ly1)
    lx2 = min(w, lx2); ly2 = min(h, ly2)
    if lx2 - lx1 < MIN_REGION_SIZE or ly2 - ly1 < MIN_REGION_SIZE:
        return None

    scale = ctx.scale
    px1, py1 = int(lx1 * scale), int(ly1 * scale)
    px2, py2 = int(lx2 * scale), int(ly2 * scale)
    cropped = ctx.raw_image.crop((px1, py1, px2, py2)).copy()

    new_size = (int(cropped.width * zoom), int(cropped.height * zoom))
    cropped = cropped.resize(new_size, Image.LANCZOS)

    return draw_region_grid(cropped, scale * zoom, lx1, ly1, lx2, ly2)


def _strategic_regions(w: int, h: int) -> list[tuple[str, tuple[int, int, int, int]]]:
    """Fixed regions that almost always contain small interactive UI."""
    top_h = min(80, max(40, h // 10))
    bot_h = min(80, max(40, h // 10))
    right_w = min(320, max(160, w // 4))
    return [
        ("顶部导航栏", (0, 0, w, top_h)),
        ("右上角",     (w - right_w, 0, w, min(h, top_h + 40))),
        ("底部",       (0, h - bot_h, w, h)),
    ]


def _center(region: tuple[int, int, int, int]) -> tuple[int, int]:
    x1, y1, x2, y2 = region
    return (x1 + x2) // 2, (y1 + y2) // 2


def _contains_point(region: tuple[int, int, int, int], pt: tuple[int, int]) -> bool:
    x1, y1, x2, y2 = region
    px, py = pt
    return x1 <= px <= x2 and y1 <= py <= y2


class SmartCropSkill(Skill):
    name = "smart_crop"
    default_enabled = True

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.padding = self.config.get("padding", ROI_PADDING)
        self.zoom = self.config.get("zoom", DEFAULT_ZOOM)
        self.strategic_enabled = self.config.get("strategic", True)
        self.reactive_enabled = self.config.get("reactive", True)

    def process(self, ctx: SkillContext) -> SkillResult:
        w, h = ctx.screen_size
        extra_images: list[bytes] = []
        descriptions: list[str] = []

        # 1. Strategic zooms — always present, independent of history
        strategic_regions = _strategic_regions(w, h) if self.strategic_enabled else []
        for name, region in strategic_regions:
            zoomed = _zoom_region(ctx, *region, zoom=self.zoom)
            if zoomed is None:
                continue
            extra_images.append(img_to_png_bytes(zoomed))
            x1, y1, x2, y2 = region
            descriptions.append(
                f"- **{name}** 区域 ({x1},{y1})→({x2},{y2})，蓝色密网格"
            )

        # 2. Reactive zoom — based on visual_diff or last action
        if self.reactive_enabled:
            roi = self._determine_roi(ctx, w, h)
            if roi is not None:
                lx1, ly1, lx2, ly2 = roi
                lx1 = max(0, lx1 - self.padding)
                ly1 = max(0, ly1 - self.padding)
                lx2 = min(w, lx2 + self.padding)
                ly2 = min(h, ly2 + self.padding)

                # Skip if mostly covered by an existing strategic region
                center = _center((lx1, ly1, lx2, ly2))
                if not any(_contains_point(r, center) for _, r in strategic_regions):
                    zoomed = _zoom_region(ctx, lx1, ly1, lx2, ly2, zoom=self.zoom)
                    if zoomed is not None:
                        extra_images.append(img_to_png_bytes(zoomed))
                        descriptions.append(
                            f"- **历史关注区** ({lx1},{ly1})→({lx2},{ly2})"
                        )

        if not extra_images:
            return SkillResult()

        text = ("## 局部放大图\n"
                "以下放大图来自原始截图的局部裁剪，蓝色密网格标注的是绝对逻辑坐标，"
                "可直接用于 tap/swipe。\n" + "\n".join(descriptions))

        log.debug("Smart crop emitted %d zoom(s)", len(extra_images))
        return SkillResult(
            text=text,
            extra_images=extra_images,
            metadata={"zoom_count": len(extra_images)},
        )

    def _determine_roi(self, ctx: SkillContext,
                       w: int, h: int) -> tuple[int, int, int, int] | None:
        """Pick a single reactive ROI from visual_diff or last action."""
        diff_result = ctx.skill_results.get("visual_diff")
        if diff_result and diff_result.metadata.get("changed"):
            regions = diff_result.metadata.get("regions", [])
            if regions:
                x1 = min(r["x1"] for r in regions)
                y1 = min(r["y1"] for r in regions)
                x2 = max(r["x2"] for r in regions)
                y2 = max(r["y2"] for r in regions)
                return (x1, y1, x2, y2)

        if ctx.history:
            last_action = ctx.history[-1].get("action", {})
            action_type = last_action.get("type", "")

            if action_type == "tap":
                x = last_action.get("x", w // 2)
                y = last_action.get("y", h // 2)
                size = 120
                return (x - size, y - size, x + size, y + size)

            if action_type == "input":
                x = last_action.get("x", w // 2)
                y = last_action.get("y", h // 2)
                size = 100
                return (x - size, y - size // 2, x + size * 2, y + size)

        return None
