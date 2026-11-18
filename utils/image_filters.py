"""Color filters for the /filter commands. Every function takes one RGBA
frame and returns a new one, leaving transparency as it was."""

from __future__ import annotations

import cv2
import numpy as np
from PIL import Image, ImageChops, ImageEnhance, ImageFilter, ImageOps


def _with_alpha(rgb: Image.Image, source: Image.Image) -> Image.Image:
    result = rgb.convert("RGBA")
    result.putalpha(source.getchannel("A"))
    return result


def invert(frame: Image.Image) -> Image.Image:
    return _with_alpha(ImageOps.invert(frame.convert("RGB")), frame)


def grayscale(frame: Image.Image) -> Image.Image:
    return _with_alpha(ImageOps.grayscale(frame), frame)


def sepia(frame: Image.Image) -> Image.Image:
    rgb = np.asarray(frame.convert("RGB"), dtype=np.float32)
    matrix = np.array([[0.393, 0.769, 0.189], [0.349, 0.686, 0.168], [0.272, 0.534, 0.131]], dtype=np.float32)
    toned = np.clip(rgb @ matrix.T, 0, 255).astype(np.uint8)
    return _with_alpha(Image.fromarray(toned, "RGB"), frame)


def hue_shift(frame: Image.Image, degrees: float) -> Image.Image:
    """Turn every color around the color wheel by `degrees`."""
    hsv = cv2.cvtColor(np.asarray(frame.convert("RGB")), cv2.COLOR_RGB2HSV)
    # OpenCV keeps 8-bit hue as 0..179 (half degrees).
    hsv[..., 0] = ((hsv[..., 0].astype(np.int32) + int(round(degrees / 2))) % 180).astype(np.uint8)
    return _with_alpha(Image.fromarray(cv2.cvtColor(hsv, cv2.COLOR_HSV2RGB), "RGB"), frame)


def adjust(frame: Image.Image, brightness: int = 100, contrast: int = 100, saturation: int = 100) -> Image.Image:
    """Each value is a percent: 100 leaves that property alone."""
    rgb = frame.convert("RGB")
    for enhancer, percent in ((ImageEnhance.Brightness, brightness), (ImageEnhance.Contrast, contrast), (ImageEnhance.Color, saturation)):
        if percent != 100:
            rgb = enhancer(rgb).enhance(percent / 100.0)
    return _with_alpha(rgb, frame)


def posterize(frame: Image.Image, levels: int = 4) -> Image.Image:
    """Keep only `levels` shades of each of red, green and blue."""
    levels = max(2, min(16, levels))
    rgb = np.asarray(frame.convert("RGB"), dtype=np.float32)
    step = 255.0 / (levels - 1)
    return _with_alpha(Image.fromarray((np.round(rgb / step) * step).astype(np.uint8), "RGB"), frame)


def edges(frame: Image.Image, style: str = "sketch") -> Image.Image:
    """"sketch": dark pencil lines on white. "neon": bright outlines on black."""
    if style == "neon":
        found = frame.convert("RGB").filter(ImageFilter.FIND_EDGES)
        return _with_alpha(ImageEnhance.Brightness(found).enhance(2.2), frame)
    gray = ImageOps.grayscale(frame)
    # A pencil sketch is the gray image divided by a blurred copy of itself
    # (a "color dodge"): flat areas go white, and edges stay dark.
    blurred = np.asarray(gray.filter(ImageFilter.GaussianBlur(radius=max(2, min(frame.size) // 80))), dtype=np.float32)
    dodged = np.clip(np.asarray(gray, dtype=np.float32) * 255.0 / np.maximum(blurred, 1.0), 0, 255)
    darkened = 255.0 * (dodged / 255.0) ** 2.2  # deepen the lines
    return _with_alpha(Image.fromarray(darkened.astype(np.uint8), "L"), frame)


def emboss(frame: Image.Image) -> Image.Image:
    return _with_alpha(frame.convert("RGB").filter(ImageFilter.EMBOSS), frame)


# Shadows, midtones and highlights of the vaporwave look.
_VAPOR_STOPS = np.array([[38, 16, 84], [255, 72, 196], [84, 240, 255]], dtype=np.float32)


def vaporwave(frame: Image.Image) -> Image.Image:
    """Purple shadows, pink midtones and cyan highlights, with faint scanlines."""
    gray = np.asarray(ImageOps.autocontrast(ImageOps.grayscale(frame)), dtype=np.float32) / 255.0
    low = np.clip(gray * 2.0, 0, 1)[..., None]
    high = np.clip(gray * 2.0 - 1.0, 0, 1)[..., None]
    mapped = np.where(gray[..., None] < 0.5,
                      _VAPOR_STOPS[0] + (_VAPOR_STOPS[1] - _VAPOR_STOPS[0]) * low,
                      _VAPOR_STOPS[1] + (_VAPOR_STOPS[2] - _VAPOR_STOPS[1]) * high)
    original = np.asarray(frame.convert("RGB"), dtype=np.float32)
    mixed = mapped * 0.75 + original * 0.25
    spacing = max(2, frame.height // 120)
    mixed[::spacing * 2] *= 0.72  # scanlines
    toned = Image.fromarray(np.clip(mixed, 0, 255).astype(np.uint8), "RGB")
    # A slight red/blue split, like a worn tape.
    red, green, blue = toned.split()
    shift = max(1, frame.width // 200)
    toned = Image.merge("RGB", (ImageChops.offset(red, shift, 0), green, ImageChops.offset(blue, -shift, 0)))
    return _with_alpha(toned, frame)
