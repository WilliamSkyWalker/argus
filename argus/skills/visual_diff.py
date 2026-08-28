"""Visual Diff Skill — compare consecutive screenshots to detect changes.

Helps the LLM verify whether an action took effect by highlighting
changed regions between the previous and current screenshot.
"""

from __future__ import annotations

import io

from PIL import Image, ImageChops, ImageDraw, ImageFilter

from .base import Skill, SkillContext, SkillResult
from ..grid import img_to_png_bytes
from ..logger import get_logger

log = get_logger("skills.diff")

# Minimum pixel difference to count as a change (0-255)
DIFF_THRESHOLD = 30

# Minimum percentage of changed pixels to report as "changed"
MIN_CHANGE_RATIO = 0.005  # 0.5%


def _compute_diff(prev: Image.Image, curr: Image.Image,
                  threshold: int = DIFF_THRESHOLD) -> tuple[Image.Image, float, list[tuple]]:
    """Compute visual diff between two images.

    Returns:
        - diff_mask: highlighted diff image
        - change_ratio: fraction of pixels that changed
        - regions: list of (x1, y1, x2, y2) bounding boxes of changed regions
    """
    # Resize to same dimensions if needed
    if prev.size != curr.size:
        prev = prev.resize(curr.size, Image.LANCZOS)

    prev_rgb = prev.convert("RGB")
    curr_rgb = curr.convert("RGB")

    # Pixel-wise difference
    diff = ImageChops.difference(prev_rgb, curr_rgb)

    # Convert to grayscale and threshold
    gray = diff.convert("L")
    binary = gray.point(lambda p: 255 if p > threshold else 0)

    # Calculate change ratio
    total_pixels = binary.size[0] * binary.size[1]
    changed_pixels = sum(1 for p in binary.getdata() if p > 0)
    change_ratio = changed_pixels / total_pixels if total_pixels > 0 else 0

    # Find bounding boxes of changed regions using connected components
    # Use a simple approach: dilate then find bounding box of non-zero regions
    dilated = binary.filter(ImageFilter.MaxFilter(15))

    regions = _find_change_regions(dilated)

    # Create highlighted diff visualization
    diff_vis = curr_rgb.copy()
    draw = ImageDraw.Draw(diff_vis, "RGBA")

    # Red overlay on changed areas
    for x1, y1, x2, y2 in regions:
        draw.rectangle([x1, y1, x2, y2],
                        fill=(255, 0, 0, 40),
                        outline=(255, 0, 0, 180),
                        width=2)

    return diff_vis, change_ratio, regions


def _find_change_regions(binary: Image.Image, min_size: int = 20) -> list[tuple]:
    """Find bounding boxes of changed regions in a binary mask.

    Uses a simple grid-based approach to cluster nearby changed pixels.
    """
    w, h = binary.size
    grid_size = 40  # Divide into cells
    cells_x = (w + grid_size - 1) // grid_size
    cells_y = (h + grid_size - 1) // grid_size

    # Find which grid cells have changes
    active_cells = set()
    pixels = binary.load()

    for cy in range(cells_y):
        for cx in range(cells_x):
            x_start = cx * grid_size
            y_start = cy * grid_size
            x_end = min(x_start + grid_size, w)
            y_end = min(y_start + grid_size, h)

            has_change = False
            for y in range(y_start, y_end, 4):
                for x in range(x_start, x_end, 4):
                    if pixels[x, y] > 0:
                        has_change = True
                        break
                if has_change:
                    break

            if has_change:
                active_cells.add((cx, cy))

    if not active_cells:
        return []

    # Merge adjacent cells into regions (simple flood fill)
    visited = set()
    regions = []

    for cell in active_cells:
        if cell in visited:
            continue

        # BFS to find connected component
        queue = [cell]
        component = []
        while queue:
            c = queue.pop(0)
            if c in visited or c not in active_cells:
                continue
            visited.add(c)
            component.append(c)
            cx, cy = c
            for dx, dy in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                neighbor = (cx + dx, cy + dy)
                if neighbor in active_cells and neighbor not in visited:
                    queue.append(neighbor)

        if component:
            min_cx = min(c[0] for c in component)
            min_cy = min(c[1] for c in component)
            max_cx = max(c[0] for c in component)
            max_cy = max(c[1] for c in component)

            x1 = min_cx * grid_size
            y1 = min_cy * grid_size
            x2 = min((max_cx + 1) * grid_size, w)
            y2 = min((max_cy + 1) * grid_size, h)

            if (x2 - x1) >= min_size and (y2 - y1) >= min_size:
                regions.append((x1, y1, x2, y2))

    return regions


class VisualDiffSkill(Skill):
    name = "visual_diff"
    default_enabled = True

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.threshold = self.config.get("threshold", DIFF_THRESHOLD)
        self.min_change = self.config.get("min_change_ratio", MIN_CHANGE_RATIO)

    def is_applicable(self, ctx: SkillContext) -> bool:
        """Only run when we have a previous screenshot to compare against."""
        return ctx.prev_image is not None

    def process(self, ctx: SkillContext) -> SkillResult:
        diff_vis, change_ratio, regions = _compute_diff(
            ctx.prev_image, ctx.raw_image, self.threshold
        )

        # Convert regions from pixel coords to logical coords
        scale = ctx.scale
        logical_regions = []
        for x1, y1, x2, y2 in regions:
            logical_regions.append({
                "x1": int(x1 / scale), "y1": int(y1 / scale),
                "x2": int(x2 / scale), "y2": int(y2 / scale),
            })

        # Determine change description
        if change_ratio < self.min_change:
            desc = "画面几乎无变化（操作可能未生效或正在加载）"
        elif change_ratio < 0.05:
            desc = f"画面有小范围变化（{len(regions)} 个区域），操作可能已生效"
        elif change_ratio < 0.3:
            desc = f"画面有明显变化（{len(regions)} 个区域），可能发生了页面切换或内容更新"
        else:
            desc = "画面发生大范围变化，可能切换了页面或弹出了新窗口"

        text = f"## 画面变化检测\n变化率: {change_ratio:.1%} — {desc}"

        # Include diff image as extra image for LLM (only if significant change)
        extra_images = []
        if change_ratio >= self.min_change and regions:
            extra_images.append(img_to_png_bytes(diff_vis))

        log.debug("Visual diff: %.1f%% changed, %d regions", change_ratio * 100, len(regions))

        return SkillResult(
            text=text,
            extra_images=extra_images,
            metadata={
                "change_ratio": round(change_ratio, 4),
                "regions": logical_regions,
                "changed": change_ratio >= self.min_change,
            },
        )
