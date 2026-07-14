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


class KeyboardDetectorSkill(Skill):
    name = "keyboard_detector"
    default_enabled = True

    def process(self, ctx: SkillContext) -> SkillResult:
        w, h = ctx.screen_size

        # 键盘检测只靠 platform driver 的权威 IME 标志（Android `dumpsys
        # input_method` / iOS driver）——纯视觉，不再解析 UI 树。
        kb_info: dict | None = None
        if ctx.ime_visible:
            kb_info = {
                "y_start": int(h * 0.55),
                "height": int(h * 0.45),
                "source": "platform_driver",
            }

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
