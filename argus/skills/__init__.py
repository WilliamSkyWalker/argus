"""Vision skills registry — load and run image preprocessing skills."""

from __future__ import annotations

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills")

# Registry: name → class
_BUILTIN_SKILLS: dict[str, type[Skill]] = {}


def _register_builtins():
    """Lazily import and register all built-in skills."""
    if _BUILTIN_SKILLS:
        return

    from .ocr import OCRSkill
    from .element_marker import ElementMarkerSkill
    from .visual_diff import VisualDiffSkill
    from .smart_crop import SmartCropSkill
    from .loading_detector import LoadingDetectorSkill
    from .toast_detector import ToastDetectorSkill
    from .scroll_map import ScrollMapSkill
    from .keyboard_detector import KeyboardDetectorSkill
    from .color_validator import ColorValidatorSkill
    from .layout_checker import LayoutCheckerSkill

    for cls in [
        # Phase 1: detection & analysis (order matters)
        LoadingDetectorSkill,
        KeyboardDetectorSkill,
        ScrollMapSkill,
        ElementMarkerSkill,
        # Phase 2: diff & change detection
        VisualDiffSkill,
        ToastDetectorSkill,
        # Phase 3: enhancement
        SmartCropSkill,
        OCRSkill,
        # Phase 4: validation (typically disabled by default)
        ColorValidatorSkill,
        LayoutCheckerSkill,
    ]:
        _BUILTIN_SKILLS[cls.name] = cls


def create_pipeline(skills_config: dict | None = None) -> list[Skill]:
    """Create an ordered list of skill instances based on config.

    Config format:
        {
            "enabled": ["ocr", "element_marker", "visual_diff", "smart_crop"],
            "ocr": {"lang": ["ch_sim", "en"]},
            "element_marker": {"style": "numbered"},
            ...
        }
    """
    _register_builtins()
    cfg = skills_config or {}

    enabled_names = cfg.get("enabled", None)

    # If not specified, use all skills that are default_enabled
    if enabled_names is None:
        enabled_names = [
            name for name, cls in _BUILTIN_SKILLS.items()
            if cls.default_enabled
        ]

    pipeline = []
    for name in enabled_names:
        cls = _BUILTIN_SKILLS.get(name)
        if cls is None:
            log.warning("Unknown skill: %s (skipping)", name)
            continue
        skill_cfg = cfg.get(name, {})
        pipeline.append(cls(skill_cfg))
        log.debug("Loaded skill: %s", name)

    return pipeline


def run_pipeline(pipeline: list[Skill], ctx: SkillContext) -> SkillContext:
    """Run all skills in order, accumulating results into the context."""
    for skill in pipeline:
        if not skill.is_applicable(ctx):
            log.debug("Skipping skill %s (not applicable)", skill.name)
            continue

        try:
            result = skill.process(ctx)
            ctx.skill_results[skill.name] = result

            # If the skill produced an annotated image, update the working image
            if result.image is not None:
                ctx.image = result.image

        except Exception as e:
            log.warning("Skill %s failed: %s", skill.name, e)

    return ctx


__all__ = [
    "Skill", "SkillContext", "SkillResult",
    "create_pipeline", "run_pipeline",
]
