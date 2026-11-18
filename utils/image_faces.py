"""Effects drawn onto the faces found in a frame, for /image effects. Each
takes one RGBA frame and its face boxes (left, top, right, bottom). The
detector gives boxes only, so features are placed where they sit on a face
looking at the camera."""

from __future__ import annotations

from PIL import Image

from utils import richtext

Box = tuple[int, int, int, int]


def joy(frame: Image.Image, boxes: list[Box]) -> Image.Image:
    """A crying-laughing emoji over every face."""
    result = frame.copy()
    for left, top, right, bottom in boxes:
        grow = int(max(right - left, bottom - top) * 0.15)
        richtext.draw_in_box(result, (left - grow, top - grow, right + grow, bottom + grow), "😂", "arial.ttf", fill=(255, 255, 255, 255), max_size=1200)
    return result
