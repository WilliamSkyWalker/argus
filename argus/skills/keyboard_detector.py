"""Keyboard Detector Skill — detect on-screen keyboard and its impact area.

When a soft keyboard is visible (iOS/Android), it covers the bottom ~40% of the
screen. Elements behind the keyboard cannot be tapped. This skill:

1. Detects keyboard presence from UI tree or visual analysis
2. Reports the occluded region so the LLM avoids tapping behind the keyboard
3. Advises scrolling or dismissing the keyboard if the target is hidden

Detection: only structured IME signals from the UI tree
- Android: uiautomator dump exposes input method package (com.*.inputmethod.*)
- iOS: a11y tree exposes XCUIElementTypeKeyboard / UIKeyboard* nodes

Visual row-pattern detection was removed — it triggered on any screen with
evenly-spaced list rows (Settings menus, tab bars, bottom sheets), creating
false positives that drove the LLM to press BACK or wait instead of acting.
"""

from __future__ import annotations

import json
import re

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills.keyboard")

# Keyboard typically occupies bottom 35-50% of the screen
_KB_MIN_HEIGHT_FRACTION = 0.25


# 仅匹配真实 IME 信号，避免「Keyboard settings」「Keyboard shortcuts」文本误判：
# - Android：uiautomator dump 的 IME package（com.*.inputmethod.*）
# - iOS：a11y 节点 type（XCUIElementTypeKeyboard / UIKeyboard*）
# 不要 lower-case + substring 匹配 "keyboard"，那会让 Settings 列表里随便一行带
# "Keyboard" 文字的菜单被识别为「键盘已弹起」。
_IME_SIGNAL_PATTERNS = (
    re.compile(r'package="[^"]*\.inputmethod[^"]*"'),
    re.compile(r'package="com\.android\.inputmethodservice'),
    re.compile(r'\bXCUIElementTypeKeyboard\b'),
    re.compile(r'\bUIKeyboard(?:Impl|Window|InputViewSet)\b'),
)


def _detect_from_ui_tree(ui_tree: str, screen_h: int) -> dict | None:
    """Find keyboard element in iOS / Android UI tree.

    Only matches concrete IME signals (package names, accessibility node types).
    Plain text containing the word "Keyboard" (e.g. Android Settings menu items)
    is NOT a signal — that produced false positives that made the LLM press BACK
    whenever it was on a settings page.
    """
    try:
        nodes = json.loads(ui_tree)
    except (json.JSONDecodeError, TypeError):
        if any(p.search(ui_tree) for p in _IME_SIGNAL_PATTERNS):
            return {
                "y_start": int(screen_h * 0.55),
                "height": int(screen_h * 0.45),
                "source": "ui_tree_ime_signal",
            }
        return None

    for node in nodes if isinstance(nodes, list) else [nodes]:
        node_type = str(node.get("type", ""))
        if "Keyboard" in node_type:
            frame = node.get("frame", {})
            y = int(frame.get("y", 0))
            h = int(frame.get("height", 0))
            if h > screen_h * _KB_MIN_HEIGHT_FRACTION:
                return {
                    "y_start": y,
                    "height": h,
                    "source": "ui_tree",
                }

    return None


class KeyboardDetectorSkill(Skill):
    name = "keyboard_detector"
    default_enabled = True

    def process(self, ctx: SkillContext) -> SkillResult:
        w, h = ctx.screen_size

        # Priority order:
        # 1. Authoritative IME flag from platform driver (Android `dumpsys
        #    input_method` — the only reliable source: uiautomator dump
        #    cannot see the IME window, and the visual row detector triggers
        #    on any list screen).
        # 2. Structured UI tree match (iOS XCUI / Android IME package node),
        #    in case the platform driver couldn't report.
        kb_info: dict | None = None
        if ctx.ime_visible:
            kb_info = {
                "y_start": int(h * 0.55),
                "height": int(h * 0.45),
                "source": "platform_driver",
            }
        if kb_info is None:
            kb_info = _detect_from_ui_tree(ctx.ui_tree, h)

        if kb_info is None:
            return SkillResult(
                text="",
                metadata={"keyboard_visible": False},
            )

        y_start = kb_info["y_start"]
        kb_height = kb_info["height"]
        visible_area_h = y_start  # Usable area above keyboard

        text = (
            f"## 键盘状态\n"
            f"软键盘已弹出，占据屏幕底部 y={y_start} 以下区域（高度 {kb_height}pt）。\n"
            f"可用区域: y=0 到 y={y_start}。\n"
            f"注意：键盘下方的元素无法点击。如果目标在键盘后面，"
            f"可先按 escape 收起键盘，或向上滚动。"
        )

        log.debug("Keyboard detected: y_start=%d height=%d source=%s",
                   y_start, kb_height, kb_info.get("source", "?"))

        return SkillResult(
            text=text,
            metadata={
                "keyboard_visible": True,
                "y_start": y_start,
                "height": kb_height,
                "visible_area_height": visible_area_h,
                "source": kb_info.get("source", ""),
            },
        )
