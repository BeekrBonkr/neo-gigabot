"""Effects drawn onto the faces found in a frame, for /image effects. Each
takes one RGBA frame and its face boxes (left, top, right, bottom). The
detector gives boxes only, so features are placed where they sit on a face
looking at the camera."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageFilter

from utils import richtext

Box = tuple[int, int, int, int]
CLOWN_NOSE = Path(__file__).resolve().parents[1] / "assets" / "images" / "clown_nose.png"


def joy(frame: Image.Image, boxes: list[Box]) -> Image.Image:
    """A crying-laughing emoji over every face."""
    result = frame.copy()
    for left, top, right, bottom in boxes:
        grow = int(max(right - left, bottom - top) * 0.15)
        richtext.draw_in_box(result, (left - grow, top - grow, right + grow, bottom + grow), "😂", "arial.ttf", fill=(255, 255, 255, 255), max_size=1200)
    return result


@lru_cache(maxsize=1)
def _clown_nose() -> Image.Image:
    return Image.open(CLOWN_NOSE).convert("RGBA")


def clown(frame: Image.Image, boxes: list[Box]) -> Image.Image:
    """A red clown nose on every face."""
    result = frame.copy()
    for left, top, right, bottom in boxes:
        side = max(4, int((right - left) * 0.34))
        nose = _clown_nose().resize((side, side), Image.LANCZOS)
        center_x, center_y = (left + right) // 2, top + int((bottom - top) * 0.62)
        result.alpha_composite(nose, (max(0, center_x - side // 2), max(0, center_y - side // 2)))
    return result


def forehead(frame: Image.Image, boxes: list[Box], amount: int = 50) -> Image.Image:
    """Stretch every forehead upward. `amount` is the extra height, as a
    percent of the face's height."""
    result = frame.copy()
    for left, top, right, bottom in sorted(boxes, key=lambda box: box[1], reverse=True):  # lowest face first: each stretch moves what is above it
        height = bottom - top
        side = int((right - left) * 0.2)
        left, right = max(0, left - side), min(frame.width, right + side)
        hairline, brow = max(0, top + int(height * 0.05)), min(frame.height, top + int(height * 0.30))
        grow = int(height * amount / 100)
        if brow - hairline < 2 or right - left < 2 or grow < 1:
            continue
        column = Image.new("RGBA", (right - left, brow), (0, 0, 0, 0))
        column.paste(result.crop((left, 0, right, hairline)), (0, -grow))  # the hair and everything above it ride up
        stretched = result.crop((left, hairline, right, brow)).resize((right - left, brow - hairline + grow), Image.LANCZOS)
        column.paste(stretched, (0, hairline - grow))
        blend = Image.new("L", column.size, 0)  # soft sides, so the stretched strip melts into the picture beside it
        blend.paste(255, (side // 2, 0, column.width - side // 2, brow))
        result.paste(column, (left, 0), blend.filter(ImageFilter.GaussianBlur(max(1, side // 4))))
    return result
