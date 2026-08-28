"""Layout Checker Skill — detect common layout issues in the UI.

Identifies problems that affect usability and visual quality:
1. Overlapping elements (two interactive elements sharing the same area)
2. Elements clipped by screen bounds (content overflow / cut-off)
3. Alignment issues (elements that should be aligned but aren't)
4. Touch target too small (interactive element < 44pt on mobile)

Uses UI tree data when available; falls back to visual edge detection.
"""

from __future__ import annotations

import json
import re

from PIL import Image

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills.layout")

# Minimum touch target size (Apple HIG / Material Design)
MIN_TOUCH_TARGET = 44  # logical points


def _check_overlaps(elements: list[dict]) -> list[dict]:
    """Find pairs of interactive elements that significantly overlap."""
    issues = []
    interactive = [
        e for e in elements
        if e.get("enabled") and e["w"] > 5 and e["h"] > 5
        and e["type"] not in ("Window", "Application", "Other", "ScrollView", "View")
    ]

    for i in range(len(interactive)):
        a = interactive[i]
        ax1, ay1 = a["x"], a["y"]
        ax2, ay2 = ax1 + a["w"], ay1 + a["h"]

        for j in range(i + 1, len(interactive)):
            b = interactive[j]
            bx1, by1 = b["x"], b["y"]
            bx2, by2 = bx1 + b["w"], by1 + b["h"]

            # Calculate intersection
            ix1 = max(ax1, bx1)
            iy1 = max(ay1, by1)
            ix2 = min(ax2, bx2)
            iy2 = min(ay2, by2)

            if ix1 < ix2 and iy1 < iy2:
                inter_area = (ix2 - ix1) * (iy2 - iy1)
                min_area = min(a["w"] * a["h"], b["w"] * b["h"])
                overlap_ratio = inter_area / min_area if min_area > 0 else 0

                if overlap_ratio > 0.3:
                    a_label = a["label"] or a["type"]
                    b_label = b["label"] or b["type"]
                    issues.append({
                        "type": "overlap",
                        "severity": "high" if overlap_ratio > 0.7 else "medium",
                        "description": f"元素重叠: \"{a_label}\" 与 \"{b_label}\" 重叠 {overlap_ratio:.0%}",
                        "elements": [a_label, b_label],
                        "overlap_ratio": round(overlap_ratio, 2),
                    })

    return issues


def _check_clipped(elements: list[dict], screen_w: int, screen_h: int) -> list[dict]:
    """Find elements that extend beyond screen bounds."""
    issues = []
    for e in elements:
        if e["w"] < 5 or e["h"] < 5:
            continue

        x2 = e["x"] + e["w"]
        y2 = e["y"] + e["h"]
        label = e["label"] or e["type"]

        clipped_parts = []
        if e["x"] < -2:
            clipped_parts.append("左侧")
        if e["y"] < -2:
            clipped_parts.append("顶部")
        if x2 > screen_w + 2:
            clipped_parts.append("右侧")
        if y2 > screen_h + 2:
            clipped_parts.append("底部")

        if clipped_parts:
            issues.append({
                "type": "clipped",
                "severity": "medium",
                "description": f"元素 \"{label}\" 被{'/'.join(clipped_parts)}裁切",
                "element": label,
                "bounds": {"x": e["x"], "y": e["y"], "w": e["w"], "h": e["h"]},
            })

    return issues


def _check_small_targets(elements: list[dict]) -> list[dict]:
    """Find interactive elements smaller than minimum touch target."""
    issues = []
    interactive_types = {
        "Button", "TextField", "Switch", "Slider", "Link",
        "SearchField", "Tab", "Cell",
    }

    for e in elements:
        if not e.get("enabled"):
            continue
        if not any(t in e["type"] for t in interactive_types):
            continue

        too_small = []
        if 0 < e["w"] < MIN_TOUCH_TARGET:
            too_small.append(f"宽度 {e['w']:.0f}pt")
        if 0 < e["h"] < MIN_TOUCH_TARGET:
            too_small.append(f"高度 {e['h']:.0f}pt")

        if too_small:
            label = e["label"] or e["type"]
            issues.append({
                "type": "small_target",
                "severity": "low",
                "description": f"触控目标过小: \"{label}\" {', '.join(too_small)} (最小 {MIN_TOUCH_TARGET}pt)",
                "element": label,
                "size": {"w": e["w"], "h": e["h"]},
            })

    return issues


def _check_alignment(elements: list[dict]) -> list[dict]:
    """Check if elements that appear to be in a group have consistent alignment.

    Groups elements by similar y-position (same row) or x-position (same column),
    then checks if their edges are properly aligned.
    """
    issues = []
    if len(elements) < 3:
        return issues

    # Find elements in the same horizontal row (similar y, tolerance 5pt)
    by_row: dict[int, list] = {}
    for e in elements:
        if e["w"] < 10 or e["h"] < 10:
            continue
        row_key = round(e["y"] / 8) * 8
        by_row.setdefault(row_key, []).append(e)

    for row_key, row_elems in by_row.items():
        if len(row_elems) < 2:
            continue

        # Check vertical alignment within the row
        tops = [e["y"] for e in row_elems]
        avg_top = sum(tops) / len(tops)
        misaligned = [
            e for e in row_elems
            if abs(e["y"] - avg_top) > 4 and abs(e["y"] - avg_top) < 20
        ]

        if misaligned:
            labels = [e["label"] or e["type"] for e in misaligned[:3]]
            issues.append({
                "type": "misalignment",
                "severity": "low",
                "description": f"水平排列的元素垂直未对齐: {', '.join(labels)} (偏移 > 4pt)",
                "elements": labels,
            })

    return issues


def _detect_visual_overflow(image: Image.Image) -> list[dict]:
    """Detect visual clipping: content that appears cut off at screen edges.

    Checks if there's high-contrast content touching the screen edges,
    suggesting it extends beyond the visible area.
    """
    issues = []
    img = image.convert("RGB")
    w, h = img.size
    pixels = img.load()

    # Check right edge: if there's non-uniform content touching the rightmost column
    right_strip = [pixels[w - 1, y] for y in range(0, h, 4)]
    right_variance = _pixel_variance(right_strip)

    if right_variance > 1500:
        issues.append({
            "type": "visual_overflow",
            "severity": "medium",
            "description": "右侧边缘检测到内容截断，可能存在水平溢出",
        })

    return issues


def _pixel_variance(pixels: list) -> float:
    """Calculate variance of a list of RGB pixel tuples."""
    if len(pixels) < 2:
        return 0.0
    r_vals = [p[0] for p in pixels]
    g_vals = [p[1] for p in pixels]
    b_vals = [p[2] for p in pixels]

    def var(vals):
        avg = sum(vals) / len(vals)
        return sum((v - avg) ** 2 for v in vals) / len(vals)

    return var(r_vals) + var(g_vals) + var(b_vals)


class LayoutCheckerSkill(Skill):
    name = "layout_checker"
    default_enabled = False  # Primarily for design review / Figma comparison

    def process(self, ctx: SkillContext) -> SkillResult:
        w, h = ctx.screen_size
        all_issues = []

        # 纯视觉：只做像素级 overflow 检测（不再从 UI 树取元素 bounds 做
        # overlap/clip/alignment 检查——那些依赖树，已随全局去树移除）。
        all_issues.extend(_detect_visual_overflow(ctx.raw_image))

        if not all_issues:
            return SkillResult(
                text="",
                metadata={"issues": [], "issue_count": 0},
            )

        # Sort by severity
        severity_order = {"high": 0, "medium": 1, "low": 2}
        all_issues.sort(key=lambda i: severity_order.get(i.get("severity", "low"), 3))

        # Build report
        severity_icons = {"high": "!!!", "medium": " !!", "low": "  !"}
        lines = [f"## 布局检查（发现 {len(all_issues)} 个问题）\n"]
        for issue in all_issues[:10]:
            icon = severity_icons.get(issue["severity"], "  ?")
            lines.append(f"  {icon} [{issue['type']}] {issue['description']}")

        text = "\n".join(lines)

        log.debug("Layout checker: %d issues found", len(all_issues))

        return SkillResult(
            text=text,
            metadata={
                "issues": all_issues,
                "issue_count": len(all_issues),
            },
        )
