"""Element Marker Skill — detect and number interactive UI elements on screenshots.

Parses the UI tree to find clickable/interactive elements, then draws numbered
markers on the screenshot. The LLM can reference elements by number (e.g. "tap [3]")
instead of guessing pixel coordinates.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET

from PIL import Image, ImageDraw, ImageFont

from .base import Skill, SkillContext, SkillResult
from ..grid import load_font
from ..logger import get_logger

log = get_logger("skills.marker")

# Colors for marker badges
MARKER_BG = (34, 139, 34, 200)       # green badge
MARKER_BORDER = (255, 255, 255, 220)  # white border
MARKER_TEXT = (255, 255, 255, 255)    # white text
HIGHLIGHT_COLOR = (34, 139, 34, 40)   # subtle green highlight


def _parse_ios_ui_tree(xml_str: str, scale: float) -> list[dict]:
    """Extract interactive elements from iOS accessibility XML."""
    elements = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return elements

    idx = 0
    for elem in root.iter():
        # iOS accessibility elements with tap target
        accessible = elem.get("accessible", "")
        enabled = elem.get("enabled", "true")
        label = elem.get("label", "") or elem.get("name", "")
        elem_type = elem.get("type", "") or elem.tag
        frame = elem.get("frame", "")

        if not frame:
            continue

        # Parse frame: {{x, y}, {w, h}}
        m = re.match(r"\{\{(\d+\.?\d*),\s*(\d+\.?\d*)\},\s*\{(\d+\.?\d*),\s*(\d+\.?\d*)\}\}", frame)
        if not m:
            continue

        x, y, w, h = float(m.group(1)), float(m.group(2)), float(m.group(3)), float(m.group(4))

        # Skip elements that are too small or too large (full screen)
        if w < 10 or h < 10 or (w > 390 and h > 800):
            continue

        # Only mark interactive elements
        interactive_types = {"Button", "TextField", "Switch", "Slider",
                             "Link", "SearchField", "Tab", "Cell",
                             "StaticText", "Icon", "Image"}
        is_interactive = (
            accessible.lower() == "true"
            or any(t in elem_type for t in interactive_types)
            or enabled.lower() == "true"
        )

        if not is_interactive:
            continue

        idx += 1
        cx = int(x + w / 2)
        cy = int(y + h / 2)

        elements.append({
            "index": idx,
            "label": label,
            "type": elem_type,
            "center": (cx, cy),
            "bounds": (int(x), int(y), int(x + w), int(y + h)),
            "pixel_bounds": (int(x * scale), int(y * scale),
                             int((x + w) * scale), int((y + h) * scale)),
            "pixel_center": (int(cx * scale), int(cy * scale)),
        })

    return elements


def _parse_android_ui_tree(xml_str: str, scale: float) -> list[dict]:
    """Extract clickable elements from Android uiautomator XML.

    Android 节点用 ``bounds="[x1,y1][x2,y2]"``（设备像素，与截图同一坐标系）。
    **关键：无 text / content-desc 的裸 clickable 节点也编号** —— Flutter App 的
    输入框、图标按钮常常没有 a11y 标签（text/desc 全空），LLM 无法靠文字匹配定位，
    只能靠「编号 + 精确中心」点击。不标的话就只能视觉估算 → 小元素必偏。
    """
    elements = []
    try:
        root = ET.fromstring(xml_str)
    except ET.ParseError:
        return elements

    bound_re = re.compile(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]")
    # 先求屏幕尺寸（用于过滤整屏背景容器）
    screen_w = screen_h = 0
    for elem in root.iter():
        m = bound_re.match(elem.get("bounds", "") or "")
        if m:
            screen_w = max(screen_w, int(m.group(3)))
            screen_h = max(screen_h, int(m.group(4)))

    idx = 0
    seen = set()
    for elem in root.iter():
        if elem.get("clickable") != "true":
            continue
        m = bound_re.match(elem.get("bounds", "") or "")
        if not m:
            continue
        x1, y1, x2, y2 = (int(m.group(i)) for i in range(1, 5))
        w, h = x2 - x1, y2 - y1
        if w < 12 or h < 12:
            continue
        # 跳过近整屏的背景/遮罩容器（>92% 屏宽且 >92% 屏高）
        if screen_w and screen_h and w >= 0.92 * screen_w and h >= 0.92 * screen_h:
            continue
        key = (x1, y1, x2, y2)
        if key in seen:
            continue
        seen.add(key)
        label = (elem.get("content-desc") or elem.get("text") or "").strip()
        cls = (elem.get("class") or "").rsplit(".", 1)[-1]
        idx += 1
        cx, cy = (x1 + x2) // 2, (y1 + y2) // 2
        elements.append({
            "index": idx,
            "label": label,
            "type": cls or "node",
            "center": (cx, cy),
            "bounds": (x1, y1, x2, y2),
            "pixel_bounds": (x1, y1, x2, y2),   # Android: tree px == screenshot px
            "pixel_center": (cx, cy),
        })
    return elements


def _parse_browser_dom(html_str: str, scale: float) -> list[dict]:
    """Extract interactive elements from browser DOM summary."""
    elements = []
    idx = 0

    # Pattern: tag [attrs] @ (x, y, w, h)  or  structured JSON entries
    # Simple heuristic: look for coordinate patterns
    for line in html_str.splitlines():
        line = line.strip()
        if not line:
            continue

        # Try to find elements with coordinates
        # Format varies — look for common patterns
        m = re.search(r'(\w+)\s*.*?@\s*\((\d+),\s*(\d+),\s*(\d+),\s*(\d+)\)', line)
        if m:
            tag = m.group(1)
            x, y, w, h = int(m.group(2)), int(m.group(3)), int(m.group(4)), int(m.group(5))
        else:
            continue

        interactive_tags = {"a", "button", "input", "select", "textarea", "label",
                            "summary", "details", "img", "A", "BUTTON", "INPUT"}
        if tag.lower() not in {t.lower() for t in interactive_tags}:
            continue

        if w < 5 or h < 5:
            continue

        idx += 1
        cx = x + w // 2
        cy = y + h // 2

        label_match = re.search(r'text="([^"]*)"', line) or re.search(r'>([^<]+)<', line)
        label = label_match.group(1) if label_match else ""

        elements.append({
            "index": idx,
            "label": label[:40],
            "type": tag,
            "center": (cx, cy),
            "bounds": (x, y, x + w, y + h),
            "pixel_bounds": (int(x * scale), int(y * scale),
                             int((x + w) * scale), int((y + h) * scale)),
            "pixel_center": (int(cx * scale), int(cy * scale)),
        })

    return elements


def _draw_markers(img: Image.Image, elements: list[dict]) -> Image.Image:
    """Draw numbered markers on the screenshot."""
    draw = ImageDraw.Draw(img, "RGBA")
    font = load_font(14)
    small_font = load_font(10)

    for elem in elements:
        idx = elem["index"]
        px, py = elem["pixel_center"]
        x1, y1, x2, y2 = elem["pixel_bounds"]

        # Subtle highlight on the element area
        draw.rectangle([x1, y1, x2, y2], fill=HIGHLIGHT_COLOR, outline=MARKER_BORDER, width=1)

        # Badge with number — positioned at top-left of element
        badge_text = str(idx)
        bbox = font.getbbox(badge_text)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        pad = 3
        bx, by = x1 - 2, y1 - th - pad * 2 - 2

        # Keep badge on screen
        if by < 0:
            by = y1 + 2
        if bx < 0:
            bx = 2

        draw.rounded_rectangle(
            [bx, by, bx + tw + pad * 2, by + th + pad * 2],
            radius=4, fill=MARKER_BG, outline=MARKER_BORDER, width=1,
        )
        draw.text((bx + pad, by + pad), badge_text, fill=MARKER_TEXT, font=font)

    return img


class ElementMarkerSkill(Skill):
    name = "element_marker"
    default_enabled = True

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.max_elements = self.config.get("max_elements", 50)

    def process(self, ctx: SkillContext) -> SkillResult:
        ui_tree = ctx.ui_tree
        scale = ctx.scale

        if not ui_tree or ui_tree.strip() in ("", "N/A", "none"):
            return SkillResult(text="[Element Marker: UI tree 为空]")

        # 按树类型分发：Android(bounds=) / iOS(frame=) / browser DOM
        if 'bounds="[' in ui_tree:
            elements = _parse_android_ui_tree(ui_tree, scale)
        elif ui_tree.strip().startswith("<"):
            elements = _parse_ios_ui_tree(ui_tree, scale)
        else:
            elements = _parse_browser_dom(ui_tree, scale)

        # Limit to top N elements
        elements = elements[:self.max_elements]

        if not elements:
            return SkillResult(
                text="[Element Marker: 未检测到可交互元素]",
                metadata={"elements": []},
            )

        # Draw markers on the working image
        annotated = _draw_markers(ctx.image.copy(), elements)

        # Build text index for the LLM
        lines = ["## 可交互元素列表（截图上已标注编号）",
                 "使用 tap 时可直接引用编号对应的坐标。\n"]
        for e in elements:
            cx, cy = e["center"]
            label = e["label"] or "(无标签)"
            lines.append(f"  [{e['index']}] {e['type']}: {label} — 坐标({cx}, {cy})")

        text = "\n".join(lines)

        log.debug("Marked %d interactive elements", len(elements))

        return SkillResult(
            image=annotated,
            text=text,
            metadata={"elements": elements},
        )
