"""Effects drawn onto the faces found in a frame, for /image effects. Each
takes one RGBA frame and its face boxes (left, top, right, bottom). The
detector gives boxes only, so features are placed where they sit on a face
looking at the camera."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image

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
