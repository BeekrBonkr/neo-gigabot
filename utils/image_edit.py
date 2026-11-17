"""Layout and utility edits for the /edit commands."""

from __future__ import annotations

import threading
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageColor, ImageDraw

from utils.imaging import load_font

MAX_OUTPUT_EDGE = 4096

# ---------- mirror / tile / circle / border / resize ----------

MIRROR_SIDES = ("left", "right", "top", "bottom")


def mirror(frame: Image.Image, side: str = "left") -> Image.Image:
    """Reflect the named half onto the other half."""
    width, height = frame.size
    result = frame.copy()
    if side in ("left", "right"):
        half = width // 2
        if side == "left":
            result.paste(frame.crop((0, 0, half, height)).transpose(Image.FLIP_LEFT_RIGHT), (width - half, 0))
        else:
            result.paste(frame.crop((width - half, 0, width, height)).transpose(Image.FLIP_LEFT_RIGHT), (0, 0))
    else:
        half = height // 2
        if side == "top":
            result.paste(frame.crop((0, 0, width, half)).transpose(Image.FLIP_TOP_BOTTOM), (0, height - half))
        else:
            result.paste(frame.crop((0, height - half, width, height)).transpose(Image.FLIP_TOP_BOTTOM), (0, 0))
    return result


def tile(frame: Image.Image, columns: int, rows: int) -> Image.Image:
    """The picture repeated in a grid that is the size of the original."""
    cell = (max(1, frame.width // columns), max(1, frame.height // rows))
    small = frame.resize(cell, Image.LANCZOS)
    result = Image.new("RGBA", (cell[0] * columns, cell[1] * rows), (0, 0, 0, 0))
    for row in range(rows):
        for column in range(columns):
            result.paste(small, (column * cell[0], row * cell[1]))
    return result


def _rounded_mask(size: tuple[int, int], radius: int) -> Image.Image:
    scale = 3  # drawn large, then shrunk, for a smooth edge
    mask = Image.new("L", (size[0] * scale, size[1] * scale), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size[0] * scale - 1, size[1] * scale - 1), radius=radius * scale, fill=255)
    return mask.resize(size, Image.LANCZOS)


def _clip(frame: Image.Image, mask: Image.Image) -> Image.Image:
    result = frame.copy()
    result.putalpha(Image.fromarray(np.minimum(np.asarray(frame.getchannel("A")), np.asarray(mask)), "L"))
    return result


def circle(frame: Image.Image) -> Image.Image:
    """Center-crop to a square and clip it to a circle."""
    side = min(frame.size)
    left, top = (frame.width - side) // 2, (frame.height - side) // 2
    square = frame.crop((left, top, left + side, top + side))
    return _clip(square, _rounded_mask((side, side), side // 2))


def parse_color(text: str) -> tuple[int, int, int]:
    """A color name ("white", "tomato") or hex code ("#5865F2", "5865F2")."""
    cleaned = text.strip()
    for candidate in (cleaned, f"#{cleaned}"):
        try:
            return ImageColor.getrgb(candidate)[:3]
        except ValueError:
            continue
    raise ValueError(f"I don't know the color `{text}`. Use a name like `white` or a hex code like `#5865F2`.")


def border(frame: Image.Image, color: tuple[int, int, int], thickness: int = 5, rounding: int = 0) -> Image.Image:
    """A frame around the picture. `thickness` and `rounding` are percents of
    the shorter side; `rounding` rounds the corners of the picture and frame."""
    short = min(frame.size)
    pad = round(short * thickness / 100.0)
    radius = round(short * rounding / 100.0)
    inner = _clip(frame, _rounded_mask(frame.size, radius)) if radius else frame
    if pad == 0:
        return inner
    size = (frame.width + pad * 2, frame.height + pad * 2)
    backing = Image.new("RGBA", size, color + (255,))
    if radius:
        backing = _clip(backing, _rounded_mask(size, radius + pad))
    backing.alpha_composite(inner, (pad, pad))
    return backing


def target_size(size: tuple[int, int], width: int | None, height: int | None, percent: int | None, *, max_edge: int = MAX_OUTPUT_EDGE) -> tuple[int, int]:
    """Work out the new size: a percent, or a width and/or height in pixels
    (one of them alone keeps the shape)."""
    old_width, old_height = size
    if percent is not None:
        new = (old_width * percent / 100.0, old_height * percent / 100.0)
    elif width is not None and height is not None:
        new = (width, height)
    elif width is not None:
        new = (width, old_height * width / old_width)
    elif height is not None:
        new = (old_width * height / old_height, height)
    else:
        raise ValueError("Give a width, a height, or a percent.")
    new = (max(1, round(new[0])), max(1, round(new[1])))
    if max(new) > max_edge:
        raise ValueError(f"That would be {new[0]}x{new[1]}. The longest side can be at most {max_edge} pixels.")
    return new


# ---------- collage ----------

COLLAGE_ROW_HEIGHT = 480
COLLAGE_CELL = 480


def collage(pictures: list[Image.Image], layout: str = "auto") -> Image.Image:
    """Two to four pictures as one: side by side at the same height ("row"),
    or as a two-column grid of equal squares ("grid")."""
    if layout == "auto":
        layout = "grid" if len(pictures) == 4 else "row"
    if layout == "row":
        height = min(COLLAGE_ROW_HEIGHT, max(picture.height for picture in pictures))
        scaled = [picture.resize((max(1, round(picture.width * height / picture.height)), height), Image.LANCZOS) for picture in pictures]
        canvas = Image.new("RGBA", (sum(picture.width for picture in scaled), height), (0, 0, 0, 0))
        x = 0
        for picture in scaled:
            canvas.paste(picture, (x, 0))
            x += picture.width
        return canvas
    rows = (len(pictures) + 1) // 2
    canvas = Image.new("RGBA", (COLLAGE_CELL * 2, COLLAGE_CELL * rows), (0, 0, 0, 0))
    for index, picture in enumerate(pictures):
        scale = max(COLLAGE_CELL / picture.width, COLLAGE_CELL / picture.height)
        filled = picture.resize((max(COLLAGE_CELL, round(picture.width * scale)), max(COLLAGE_CELL, round(picture.height * scale))), Image.LANCZOS)
        left, top = (filled.width - COLLAGE_CELL) // 2, (filled.height - COLLAGE_CELL) // 2
        cell = filled.crop((left, top, left + COLLAGE_CELL, top + COLLAGE_CELL))
        # an odd one out sits centered on the last row
        x = COLLAGE_CELL // 2 if index == len(pictures) - 1 and len(pictures) % 2 else (index % 2) * COLLAGE_CELL
        canvas.paste(cell, (x, (index // 2) * COLLAGE_CELL))
    return canvas


# ---------- palette ----------

def palette(picture: Image.Image, count: int = 5) -> tuple[Image.Image, list[str]]:
    """The picture's main colors, most common first: a strip of swatches and
    their hex codes. See-through pixels are ignored."""
    small = picture.resize((min(120, picture.width), min(120, picture.height)), Image.BILINEAR)
    pixels = np.asarray(small).reshape(-1, 4)
    solid = pixels[pixels[:, 3] >= 128][:, :3]
    if len(solid) == 0:
        raise ValueError("That image is completely transparent.")
    strip = Image.fromarray(solid.reshape(1, -1, 3), "RGB")
    reduced = strip.quantize(colors=count, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    table = reduced.getpalette()
    counts = sorted(reduced.getcolors(), reverse=True)  # (how many pixels, palette index)
    colors = [tuple(table[index * 3:index * 3 + 3]) for _, index in counts]
    swatch, label_height = 120, 34
    image = Image.new("RGBA", (swatch * len(colors), swatch + label_height), (32, 34, 37, 255))
    draw = ImageDraw.Draw(image)
    font = load_font("arial.ttf", 18)
    codes = []
    for index, color in enumerate(colors):
        code = "#{:02X}{:02X}{:02X}".format(*color)
        codes.append(code)
        draw.rectangle((index * swatch, 0, (index + 1) * swatch - 1, swatch - 1), fill=color + (255,))
        draw.text((index * swatch + swatch // 2, swatch + label_height // 2), code, font=font, fill=(235, 235, 235, 255), anchor="mm")
    return image, codes


# ---------- background removal ----------
#
# U-2-Net's small model (u2netp, about 4.5 MB, Apache 2.0, from
# github.com/xuebinqin/U-2-Net, converted to ONNX by the rembg project). It
# marks the main subject of a picture; that map becomes the transparency.
# OpenCV runs it, so there is nothing extra to install.

MATTE_MODEL = Path(__file__).resolve().parents[1] / "assets" / "models" / "u2netp.onnx"
MATTE_INPUT = 320
_MATTE_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_MATTE_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_matte_net: cv2.dnn.Net | None = None
_matte_lock = threading.Lock()  # a cv2.dnn.Net can't run two pictures at once


def subject_matte(picture: Image.Image) -> Image.Image:
    """How much each pixel belongs to the main subject, as an L image."""
    global _matte_net
    rgb = np.asarray(picture.convert("RGB").resize((MATTE_INPUT, MATTE_INPUT), Image.LANCZOS), dtype=np.float32)
    rgb = (rgb / max(1.0, float(rgb.max())) - _MATTE_MEAN) / _MATTE_STD
    blob = np.ascontiguousarray(rgb.transpose(2, 0, 1)[None])
    with _matte_lock:
        if _matte_net is None:
            if not MATTE_MODEL.exists():
                raise RuntimeError("The background removal model is missing from assets/models/.")
            _matte_net = cv2.dnn.readNetFromONNX(str(MATTE_MODEL))
        _matte_net.setInput(blob)
        predicted = _matte_net.forward()[0, 0]
    low, high = float(predicted.min()), float(predicted.max())
    scaled = (predicted - low) / max(1e-6, high - low)
    return Image.fromarray((scaled * 255).astype(np.uint8), "L").resize(picture.size, Image.LANCZOS)


def remove_background(picture: Image.Image) -> Image.Image:
    """Keep the main subject and make everything else transparent."""
    matte = np.asarray(subject_matte(picture), dtype=np.float32) / 255.0
    # Push the soft map toward a clean cut-out while keeping soft edges.
    matte = np.clip((matte - 0.25) / 0.5, 0.0, 1.0)
    alpha = np.asarray(picture.getchannel("A"), dtype=np.float32) * matte
    result = picture.copy()
    result.putalpha(Image.fromarray(alpha.astype(np.uint8), "L"))
    return result
