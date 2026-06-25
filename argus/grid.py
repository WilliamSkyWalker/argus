"""Shared coordinate grid drawing utilities for screenshot overlays."""

import io

from PIL import Image, ImageDraw, ImageFont


def load_font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype("/System/Library/Fonts/Helvetica.ttc", size)
    except Exception:
        return ImageFont.load_default()


def draw_coordinate_grid(img: Image.Image, scale: float, lw: int, lh: int) -> Image.Image:
    """Draw coordinate grid on a screenshot image.

    Three tiers: fine lines every 25pt, medium + small labels every 50pt,
    strong lines + big labels every 100pt.
    """
    draw = ImageDraw.Draw(img, "RGBA")
    s = scale

    fine_step = 25
    mid_step = 50
    label_step = 100

    fine_color = (255, 0, 0, 30)
    mid_color = (255, 0, 0, 60)
    major_color = (255, 0, 0, 100)
    text_color = (255, 0, 0, 210)
    mid_text_color = (255, 0, 0, 150)

    label_font = load_font(int(12 * s))
    mid_font = load_font(int(9 * s))

    # Vertical lines
    for lx in range(0, lw + 1, fine_step):
        px = int(lx * s)
        if lx % label_step == 0:
            draw.line([(px, 0), (px, img.height)], fill=major_color, width=1)
            draw.text((px + 2, 2), str(lx), fill=text_color, font=label_font)
        elif lx % mid_step == 0:
            draw.line([(px, 0), (px, img.height)], fill=mid_color, width=1)
            draw.text((px + 2, 2), str(lx), fill=mid_text_color, font=mid_font)
        else:
            draw.line([(px, 0), (px, img.height)], fill=fine_color, width=1)

    # Horizontal lines
    for ly in range(0, lh + 1, fine_step):
        py = int(ly * s)
        if ly % label_step == 0:
            draw.line([(0, py), (img.width, py)], fill=major_color, width=1)
            draw.text((2, py + 2), str(ly), fill=text_color, font=label_font)
        elif ly % mid_step == 0:
            draw.line([(0, py), (img.width, py)], fill=mid_color, width=1)
            draw.text((2, py + 2), str(ly), fill=mid_text_color, font=mid_font)
        else:
            draw.line([(0, py), (img.width, py)], fill=fine_color, width=1)

    return img


def draw_region_grid(img: Image.Image, scale: float,
                     lx_start: int, ly_start: int,
                     lx_end: int, ly_end: int) -> Image.Image:
    """Draw a dense grid on a cropped region image with absolute logical coords.

    Grid every 5pt, labels every 10pt for maximum precision.
    """
    draw = ImageDraw.Draw(img, "RGBA")
    s = scale

    fine_step = 5
    label_step = 25

    fine_color = (0, 120, 255, 40)
    label_color = (0, 120, 255, 100)
    text_color = (0, 120, 255, 200)
    font = load_font(int(11 * s))

    mid_step = 10
    mid_color = (0, 120, 255, 70)

    # Vertical lines
    first_lx = (lx_start // fine_step) * fine_step
    for lx in range(first_lx, lx_end + 1, fine_step):
        px = int((lx - lx_start) * s)
        if 0 <= px <= img.width:
            if lx % label_step == 0:
                draw.line([(px, 0), (px, img.height)], fill=label_color, width=1)
                draw.text((px + 2, 2), str(lx), fill=text_color, font=font)
            elif lx % mid_step == 0:
                draw.line([(px, 0), (px, img.height)], fill=mid_color, width=1)
            else:
                draw.line([(px, 0), (px, img.height)], fill=fine_color, width=1)

    # Horizontal lines
    first_ly = (ly_start // fine_step) * fine_step
    for ly in range(first_ly, ly_end + 1, fine_step):
        py = int((ly - ly_start) * s)
        if 0 <= py <= img.height:
            if ly % label_step == 0:
                draw.line([(0, py), (img.width, py)], fill=label_color, width=1)
                draw.text((2, py + 2), str(ly), fill=text_color, font=font)
            elif ly % mid_step == 0:
                draw.line([(0, py), (img.width, py)], fill=mid_color, width=1)
            else:
                draw.line([(0, py), (img.width, py)], fill=fine_color, width=1)

    return img


def img_to_png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()
