"""Image observations with explicit input mapping and bounded visual waits."""
import hashlib
import io
import math
from pathlib import Path
import time
import uuid

from PIL import Image, ImageChops

from saygo.platforms import device_session as ds


def metadata(platform, png, session=None):
    with Image.open(io.BytesIO(png)) as im:
        size = list(im.size)
    result = dict(session=session or "default", width=size[0], height=size[1],
                  screen_size=list(platform.screen_size), coordinate_space="screen",
                  image_sha256=hashlib.sha256(png).hexdigest(),
                  image_to_screen=[platform.screen_size[0] / size[0], platform.screen_size[1] / size[1]])
    if hasattr(platform, "observation_metadata"):
        result.update(platform.observation_metadata())
    if hasattr(platform, "page_id"):
        result["page_id"] = platform.page_id
    state = ds.load_state(session) or {}
    result["target"] = {key: state[key] for key in ("os", "kind", "app", "device_id", "session_id", "process_id") if key in state}
    return result


def capture(platform, session=None, out=None, crop=None):
    png = platform.screenshot_raw()
    result = metadata(platform, png, session)
    oid = uuid.uuid4().hex
    root = ds.STATE_DIR.parent / "observations"
    root.mkdir(parents=True, exist_ok=True)
    path = Path(out).expanduser().resolve() if out else root / f"{oid}.png"
    result.update(id=oid, observation_id=oid, at=time.time(), path=str(path), scale=getattr(platform, "scale", 1))
    source = root / f"{oid}.png"
    source.write_bytes(png)
    if path != source:
        path.write_bytes(png)
    result["source_path"] = str(source)
    if crop is not None:
        if len(crop) != 4 or any(isinstance(n, bool) or not isinstance(n, int) for n in crop):
            raise ValueError("crop must be [left, top, right, bottom] in image pixels")
        left, top, right, bottom = crop
        if not 0 <= left < right <= result["width"] or not 0 <= top < bottom <= result["height"]:
            raise ValueError("crop is outside the observation")
        with Image.open(io.BytesIO(png)) as im:
            cropped = im.crop(crop)
            cropped = cropped.resize((cropped.width * 2, cropped.height * 2))
            crop_path = root / f"{oid}-crop.png"
            cropped.save(crop_path)
        result["crop"] = dict(path=str(crop_path), source_rect=crop, zoom=2,
                              to_screen_scale=[n / 2 for n in result["image_to_screen"]],
                              to_screen_offset=[left * result["image_to_screen"][0], top * result["image_to_screen"][1]])
    (root / f"{oid}.json").write_text(__import__('json').dumps(result), encoding="utf-8")
    return result


def load(oid, session):
    if not isinstance(oid, str) or len(oid) != 32 or any(c not in "0123456789abcdef" for c in oid):
        raise ValueError("Invalid observation ID")
    import json
    result = json.loads((ds.STATE_DIR.parent / "observations" / f"{oid}.json").read_text())
    if result["session"] != (session or "default"):
        raise ValueError("Observation belongs to another session")
    return result


def change_fraction(before, after, region=None):
    """Fraction of pixels changing by >24 intensity; ignore compression and small noise."""
    with Image.open(io.BytesIO(before)) as first, Image.open(io.BytesIO(after)) as second:
        if first.size != second.size:
            return 1.0
        first, second = first.convert("RGB"), second.convert("RGB")
        if region:
            first, second = first.crop(region), second.crop(region)
        diff = ImageChops.difference(first, second)
        bands = diff.split()
        maximum = ImageChops.lighter(ImageChops.lighter(bands[0], bands[1]), bands[2])
        histogram = maximum.histogram()
        return sum(histogram[25:]) / max(1, maximum.width * maximum.height)


def wait(platform, mode="stable", timeout=5, threshold=.002, interval=.2):
    if mode not in {"stable", "change"} or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("wait requires stable/change and timeout in (0,60]")
    start = time.monotonic()
    previous = platform.screenshot_raw()
    baseline = previous
    stable_since = start
    while time.monotonic() - start < timeout:
        time.sleep(min(interval, max(0, timeout - (time.monotonic() - start))))
        current = platform.screenshot_raw()
        fraction = change_fraction(baseline if mode == "change" else previous, current)
        now = time.monotonic()
        if mode == "change" and fraction > threshold:
            return dict(condition_met=True, timed_out=False, mode=mode, change_fraction=fraction)
        if fraction > threshold:
            stable_since = now
        if mode == "stable" and now - stable_since >= .6:
            return dict(condition_met=True, timed_out=False, mode=mode, change_fraction=fraction)
        previous = current
    return dict(condition_met=False, timed_out=True, mode=mode)
