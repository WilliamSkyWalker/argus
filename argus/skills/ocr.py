"""OCR Skill — extract text from screenshots to supplement UI tree info.

Uses easyocr (GPU-optional) for multilingual text recognition.
Falls back to pytesseract if easyocr is unavailable.
"""

from __future__ import annotations

import io

from PIL import Image

from .base import Skill, SkillContext, SkillResult
from ..logger import get_logger

log = get_logger("skills.ocr")

# Lazy-loaded reader instance (heavy init, reuse across steps)
_reader = None
_backend = None


def _get_reader(langs: list[str], use_gpu: bool):
    """Lazily initialize the OCR backend."""
    global _reader, _backend

    if _reader is not None:
        return _reader, _backend

    # Try easyocr first
    try:
        import easyocr
        _reader = easyocr.Reader(langs, gpu=use_gpu, verbose=False)
        _backend = "easyocr"
        log.info("OCR backend: easyocr (langs=%s, gpu=%s)", langs, use_gpu)
        return _reader, _backend
    except ImportError:
        pass

    # Fallback: pytesseract
    try:
        import pytesseract
        _reader = pytesseract
        _backend = "pytesseract"
        log.info("OCR backend: pytesseract")
        return _reader, _backend
    except ImportError:
        pass

    log.warning("No OCR backend available. Install easyocr or pytesseract.")
    return None, None


def _ocr_easyocr(reader, image: Image.Image, detail: bool = True) -> list[dict]:
    """Run easyocr and return structured results."""
    import numpy as np
    img_array = np.array(image.convert("RGB"))
    raw = reader.readtext(img_array, detail=1)

    results = []
    for bbox, text, confidence in raw:
        if confidence < 0.3:
            continue
        # bbox is [[x1,y1],[x2,y1],[x2,y2],[x1,y2]]
        xs = [p[0] for p in bbox]
        ys = [p[1] for p in bbox]
        results.append({
            "text": text,
            "confidence": round(confidence, 2),
            "bbox": {
                "x1": int(min(xs)), "y1": int(min(ys)),
                "x2": int(max(xs)), "y2": int(max(ys)),
            },
        })
    return results


def _ocr_pytesseract(reader, image: Image.Image) -> list[dict]:
    """Run pytesseract and return structured results."""
    data = reader.image_to_data(image, output_type=reader.Output.DICT)
    results = []
    for i, text in enumerate(data["text"]):
        text = text.strip()
        if not text:
            continue
        conf = int(data["conf"][i])
        if conf < 30:
            continue
        results.append({
            "text": text,
            "confidence": round(conf / 100, 2),
            "bbox": {
                "x1": data["left"][i],
                "y1": data["top"][i],
                "x2": data["left"][i] + data["width"][i],
                "y2": data["top"][i] + data["height"][i],
            },
        })
    return results


class OCRSkill(Skill):
    name = "ocr"
    default_enabled = False  # Requires extra dependency

    def __init__(self, config: dict | None = None):
        super().__init__(config)
        self.langs = self.config.get("langs", ["ch_sim", "en"])
        self.use_gpu = self.config.get("gpu", False)
        self.min_confidence = self.config.get("min_confidence", 0.4)

    def process(self, ctx: SkillContext) -> SkillResult:
        reader, backend = _get_reader(self.langs, self.use_gpu)
        if reader is None:
            return SkillResult(text="[OCR unavailable — install easyocr or pytesseract]")

        # Use raw image (without grid overlay) for cleaner OCR
        image = ctx.raw_image

        if backend == "easyocr":
            detections = _ocr_easyocr(reader, image)
        else:
            detections = _ocr_pytesseract(reader, image)

        # Filter by confidence
        detections = [d for d in detections if d["confidence"] >= self.min_confidence]

        if not detections:
            return SkillResult(text="[OCR: 未识别到文字]", metadata={"detections": []})

        # Build text summary for LLM prompt
        # Group by approximate vertical position (same line)
        detections.sort(key=lambda d: (d["bbox"]["y1"] // 20, d["bbox"]["x1"]))

        lines = []
        current_y = -100
        current_line = []
        for d in detections:
            y = d["bbox"]["y1"]
            if abs(y - current_y) > 20:
                if current_line:
                    lines.append(" ".join(current_line))
                current_line = [d["text"]]
                current_y = y
            else:
                current_line.append(d["text"])
        if current_line:
            lines.append(" ".join(current_line))

        # Convert pixel coords to logical coords
        scale = ctx.scale
        for d in detections:
            b = d["bbox"]
            d["logical_bbox"] = {
                "x1": int(b["x1"] / scale),
                "y1": int(b["y1"] / scale),
                "x2": int(b["x2"] / scale),
                "y2": int(b["y2"] / scale),
            }

        text_summary = "## OCR 识别结果\n" + "\n".join(lines)

        log.debug("OCR detected %d text regions", len(detections))

        return SkillResult(
            text=text_summary,
            metadata={"detections": detections, "line_count": len(lines)},
        )
