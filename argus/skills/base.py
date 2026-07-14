"""Base class for vision skills — preprocessing steps that enhance image recognition."""

from __future__ import annotations

import io
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from PIL import Image


@dataclass
class SkillResult:
    """Output produced by a skill's process() method."""

    # Annotated screenshot (may have markers, highlights, etc.)
    image: Image.Image | None = None

    # Extra images to send alongside the main screenshot (e.g. cropped regions, diff)
    extra_images: list[bytes] = field(default_factory=list)

    # Supplementary text to inject into the LLM prompt
    text: str = ""

    # Structured data for downstream skills or the agent
    metadata: dict = field(default_factory=dict)


@dataclass
class SkillContext:
    """Shared context passed through the skill pipeline."""

    # Raw screenshot (before any annotation)
    raw_image: Image.Image

    # Screenshot with grid overlay (current working image)
    image: Image.Image

    # Screen logical size (w, h)
    screen_size: tuple[int, int]

    # Pixel-to-logical scale factor
    scale: float

    # Previous screenshot for diff (None on first step)
    prev_image: Image.Image | None = None

    # History of past actions
    history: list[dict] = field(default_factory=list)

    # Results accumulated from earlier skills in the pipeline
    skill_results: dict[str, SkillResult] = field(default_factory=dict)

    # Authoritative IME visibility from the platform driver (e.g. Android
    # dumpsys input_method). Set by agent.py before pipeline run. UI tree
    # cannot reliably reveal this on Android — IME is a system window.
    ime_visible: bool = False


class Skill(ABC):
    """Abstract base class for a vision skill."""

    # Human-readable name (used as key in config and results)
    name: str = "base"

    # Whether this skill is enabled by default
    default_enabled: bool = True

    def __init__(self, config: dict | None = None):
        self.config = config or {}

    @abstractmethod
    def process(self, ctx: SkillContext) -> SkillResult:
        """Process the screenshot/context and return enriched results.

        Skills can:
        - Annotate the image (draw markers, highlights)
        - Extract text information (OCR, element labels)
        - Produce extra images (cropped regions, diff visualizations)
        - Provide metadata for other skills or the agent
        """
        ...

    def is_applicable(self, ctx: SkillContext) -> bool:
        """Return False to skip this skill for the current step."""
        return True
