"""Meme formats for the /memegen commands. Most artwork is drawn in code; the
formats built on a well-known picture open it from assets/images. The frame
functions take one RGBA frame; the poster builders return a finished image."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageOps

from utils.image_filters import sepia
from utils import richtext

INK = (30, 24, 20, 255)
TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "assets" / "images"


@lru_cache(maxsize=None)
def _load_template(name: str) -> Image.Image:
    return Image.open(TEMPLATES_DIR / name).convert("RGBA")


def _template(name: str) -> Image.Image:
    """A fresh copy of a template picture from assets/images."""
    return _load_template(name).copy()


def _cover(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    """The picture scaled and center-cropped to fill `size` exactly."""
    return ImageOps.fit(image, size, Image.LANCZOS, centering=(0.5, 0.5))


def _contain(image: Image.Image, size: tuple[int, int]) -> Image.Image:
    scale = min(size[0] / image.width, size[1] / image.height)
    return image.resize((max(1, int(image.width * scale)), max(1, int(image.height * scale))), Image.LANCZOS)


def _centered_text(image: Image.Image, box: tuple[int, int, int, int], text: "richtext.RichText | str", font_name: str, *, fill,
                   max_size: int = 200, stroke_width: int = 0, stroke_fill=(0, 0, 0, 255), spacing: int = 6) -> None:
    """Fit text to a box on the image and draw it centered there."""
    richtext.draw_in_box(image, box, text, font_name, fill=fill, max_size=max_size, stroke=stroke_width, stroke_fill=stroke_fill, gap=spacing)


# ---------- speech bubble ----------

def speech_bubble(frame: Image.Image, tail: str = "left") -> Image.Image:
    """Cut a speech bubble out of the top of the picture, so whatever is
    behind it in chat shows through. `tail` is where the point aims."""
    scale = 3
    width, height = frame.width * scale, frame.height * scale
    band = height * 0.20
    keep = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(keep)
    draw.ellipse((-width * 0.18, -band, width * 1.18, band), fill=0)
    tip_x = {"left": 0.30, "center": 0.50, "right": 0.70}.get(tail, 0.30)
    root_x = {"left": 0.55, "center": 0.50, "right": 0.45}.get(tail, 0.55)
    draw.polygon([(width * (root_x - 0.07), band * 0.90), (width * (root_x + 0.07), band * 0.90), (width * tip_x, band * 1.85)], fill=0)
    keep = keep.resize(frame.size, Image.LANCZOS)
    result = frame.copy()
    result.putalpha(Image.fromarray(np.minimum(np.asarray(frame.getchannel("A")), np.asarray(keep)), "L"))
    return result


# ---------- jail ----------

def jail(frame: Image.Image) -> Image.Image:
    """Dim the picture and put cell bars in front of it."""
    width, height = frame.size
    rgb = ImageEnhance.Brightness(ImageEnhance.Color(frame.convert("RGB")).enhance(0.55)).enhance(0.8)
    result = rgb.convert("RGBA")
    result.putalpha(frame.getchannel("A"))
    bars = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(bars)
    count = 7
    thickness = max(4, width // 28)
    for index in range(count):
        center = int(width * (index + 0.5) / count)
        left = center - thickness // 2
        draw.rectangle((left, 0, left + thickness, height), fill=(52, 54, 60, 255))
        draw.rectangle((left + thickness // 5, 0, left + thickness // 5 + max(1, thickness // 4), height), fill=(128, 132, 142, 255))
        draw.rectangle((left + thickness - max(1, thickness // 5), 0, left + thickness, height), fill=(24, 25, 28, 255))
    rail = max(4, height // 22)
    for top in (int(height * 0.06), int(height * 0.94) - rail):
        draw.rectangle((0, top, width, top + rail), fill=(52, 54, 60, 255))
        draw.rectangle((0, top + rail // 5, width, top + rail // 5 + max(1, rail // 4)), fill=(128, 132, 142, 255))
    result.alpha_composite(bars)
    return result


# ---------- wasted ----------

def wasted(frame: Image.Image) -> Image.Image:
    """Drain the color and stamp WASTED across a dark band."""
    width, height = frame.size
    rgb = ImageEnhance.Brightness(ImageEnhance.Color(frame.convert("RGB")).enhance(0.12)).enhance(0.85)
    result = rgb.convert("RGBA")
    result.putalpha(frame.getchannel("A"))
    band_height = max(20, int(height * 0.24))
    top = (height - band_height) // 2
    overlay = Image.new("RGBA", frame.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    draw.rectangle((0, top, width, top + band_height), fill=(0, 0, 0, 150))
    _centered_text(overlay, (int(width * 0.08), top + band_height // 8, int(width * 0.92), top + band_height - band_height // 8), "WASTED",
                   "impact.ttf", fill=(200, 32, 44, 255), max_size=400, stroke_width=max(1, band_height // 28), stroke_fill=(20, 0, 0, 255))
    result.alpha_composite(overlay)
    return result


# ---------- wanted poster ----------

WANTED_SIZE = (600, 820)


def _parchment(size: tuple[int, int]) -> Image.Image:
    width, height = size
    rng = np.random.default_rng(7)  # the same paper every time
    grain = rng.normal(0, 7, (height, width, 1))
    blotches = np.asarray(Image.fromarray(rng.integers(0, 255, (height // 40, width // 40), dtype=np.uint8), "L")
                          .resize(size, Image.BICUBIC), dtype=np.float32)[..., None] - 127
    ys, xs = np.mgrid[0:height, 0:width]
    edge = np.minimum(np.minimum(xs, width - 1 - xs), np.minimum(ys, height - 1 - ys))[..., None]
    burn = np.clip(1.0 - edge / 70.0, 0, 1) ** 2 * 60  # darker toward the edges
    paper = np.array([226, 200, 152], dtype=np.float32) + grain + blotches * 0.12 - burn
    return Image.fromarray(np.clip(paper, 0, 255).astype(np.uint8), "RGB").convert("RGBA")


def wanted(picture: Image.Image, name: "richtext.RichText | str" = "", reward: "richtext.RichText | str" = "$5,000") -> Image.Image:
    """An old-west wanted poster around the picture."""
    name, reward = richtext.as_rich(name).strip(), richtext.as_rich(reward).strip()
    width, height = WANTED_SIZE
    poster = _parchment(WANTED_SIZE)
    draw = ImageDraw.Draw(poster)
    draw.rectangle((18, 18, width - 19, height - 19), outline=INK, width=4)
    draw.rectangle((28, 28, width - 29, height - 29), outline=INK, width=1)
    _centered_text(poster, (50, 40, width - 50, 170), "WANTED", "impact.ttf", fill=INK, max_size=170)
    _centered_text(poster, (60, 172, width - 60, 214), "DEAD OR ALIVE", "impact.ttf", fill=INK, max_size=44)
    photo_box = (110, 232, width - 110, 612)
    photo = sepia(_cover(picture, (photo_box[2] - photo_box[0], photo_box[3] - photo_box[1])))
    photo = ImageEnhance.Contrast(photo.convert("RGB")).enhance(1.15).convert("RGBA")
    poster.paste(photo, photo_box[:2])
    draw.rectangle((photo_box[0] - 5, photo_box[1] - 5, photo_box[2] + 4, photo_box[3] + 4), outline=INK, width=5)
    text_top = 628
    if name:
        _centered_text(poster, (50, text_top, width - 50, text_top + 70), name.upper(), "impact.ttf", fill=INK, max_size=64)
        text_top += 76
    _centered_text(poster, (50, text_top, width - 50, height - 44), ("REWARD " + reward).upper(), "impact.ttf", fill=(120, 24, 20, 255), max_size=78)
    return poster


# ---------- drake ----------

DRAKE_PANEL = 400


def _content_panel(text: "richtext.RichText | str", picture: Image.Image | None) -> Image.Image:
    panel = Image.new("RGBA", (DRAKE_PANEL, DRAKE_PANEL), (255, 255, 255, 255))
    if picture is not None:
        panel.alpha_composite(_cover(picture, (DRAKE_PANEL, DRAKE_PANEL)))
    elif richtext.as_rich(text):
        _centered_text(panel, (24, 24, DRAKE_PANEL - 24, DRAKE_PANEL - 24), richtext.as_rich(text).strip(), "arial.ttf", fill=(20, 20, 20, 255), max_size=64)
    return panel


def drake(reject_text: "richtext.RichText | str" = "", approve_text: "richtext.RichText | str" = "", reject_picture: Image.Image | None = None,
          approve_picture: Image.Image | None = None) -> Image.Image:
    """Two rows: the thing turned down, then the thing preferred. Each right-hand
    panel shows its picture if one was given, otherwise its text."""
    canvas = Image.new("RGBA", (DRAKE_PANEL * 2, DRAKE_PANEL * 2), (255, 255, 255, 255))
    canvas.paste(_template("drake.jpg"), (0, 0))  # the left column: turning away on top, approving below
    canvas.paste(_content_panel(reject_text, reject_picture), (DRAKE_PANEL, 0))
    canvas.paste(_content_panel(approve_text, approve_picture), (DRAKE_PANEL, DRAKE_PANEL))
    return canvas


# ---------- change my mind ----------

CMM_SIZE = (800, 636)
CMM_SHEET = (720, 250)  # the statement is drawn flat at this size, then laid onto the sign
# The corners of the blank part of the sign in the photo, above the printed
# CHANGE MY MIND: top left, top right, bottom right, bottom left.
CMM_SIGN = ((290, 364), (617, 240), (646, 357), (334, 471))


def change_my_mind(text: "richtext.RichText | str") -> Image.Image:
    """The change-my-mind photo with the statement on the sign, tilted to
    follow the paper."""
    scene = _template("changemymind.jpg")
    sheet = Image.new("RGBA", CMM_SHEET, (255, 255, 255, 0))
    _centered_text(sheet, (0, 0, *CMM_SHEET), richtext.as_rich(text).strip(), "arial.ttf", fill=(24, 24, 28, 255), max_size=110)
    width, height = CMM_SHEET
    flat = np.float32([(0, 0), (width, 0), (width, height), (0, height)])
    warp = cv2.getPerspectiveTransform(flat, np.float32(CMM_SIGN))
    laid = cv2.warpPerspective(np.asarray(sheet), warp, CMM_SIZE, flags=cv2.INTER_AREA, borderMode=cv2.BORDER_CONSTANT, borderValue=(255, 255, 255, 0))
    scene.alpha_composite(Image.fromarray(laid, "RGBA"))
    return scene
