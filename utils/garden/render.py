"""Draws a greenhouse as a picture.

One renderer serves Discord and the website, so they always show the same
thing. The scene is composed at its native pixel size and then enlarged by a
whole number, which keeps pixel art sharp.

Which file each sprite comes from is in assets/garden/sprites.json; nothing
here knows whether a sprite is placeholder art or bought art.
"""

from __future__ import annotations

import io
import json
from functools import lru_cache
from typing import Any

from PIL import Image, ImageDraw, ImageEnhance

from utils.garden import rules

SPRITES_FILE = rules.ASSETS / "sprites.json"
SCALE = 6
# Where things stand in the room picture (scripts/garden_placeholders.py draws it to match).
SHELF_Y = 52                  # the pots stand on this row
POT_CENTERS = (45, 68, 91)
SOIL_DEPTH = 4                # how far below the pot's rim a growing plant is rooted
BARREL_AT = (6, 71)           # left edge and the bottom of its shadow
BAND_HEIGHT = 32              # every species has a 32 pixel band on the flower sheet

# 3x5 digits and a slash, for the two counters in the corner.
GLYPHS = {
    "0": "111101101101111", "1": "010110010010111", "2": "111001111100111", "3": "111001111001111", "4": "101101111001001",
    "5": "111100111001111", "6": "111100111101111", "7": "111001010010010", "8": "111101111101111", "9": "111101111001111",
    "/": "001001010100100",
}
INK, SHADOW = (255, 255, 255, 255), (30, 26, 34, 255)


class MissingArt(RuntimeError):
    """A sprite file is not on this machine (the bought packs are not in git)."""


@lru_cache(maxsize=1)
def _manifest() -> dict[str, Any]:
    return json.loads(SPRITES_FILE.read_text(encoding="utf-8"))


@lru_cache(maxsize=None)
def _file(relative: str) -> Image.Image:
    path = rules.ASSETS / relative
    if not path.is_file():
        raise MissingArt(f"Garden art is missing: assets/garden/{relative}")
    with Image.open(path) as image:
        return image.convert("RGBA")


def _trim(image: Image.Image) -> Image.Image:
    box = image.getbbox()
    return image.crop(box) if box else image


@lru_cache(maxsize=None)
def image(name: str) -> Image.Image:
    return _file(_manifest()["images"][name])


@lru_cache(maxsize=None)
def cut(name: str) -> Image.Image:
    entry = _manifest()["cuts"][name]
    return _trim(_file(_manifest()["sheets"][entry["sheet"]]).crop(tuple(entry["box"])))


@lru_cache(maxsize=None)
def plant_sprite(species: str, part: str, index: int = 0) -> Image.Image:
    """`part` is 'stages' (index 0 to 4), 'potted' or 'packet'."""
    entry = _manifest()["species"][species]
    left, right = entry[part][index] if part == "stages" else entry[part]
    sheet = _file(_manifest()["sheets"][entry["sheet"]])
    return _trim(sheet.crop((left, entry["row"], right, entry["row"] + BAND_HEIGHT)))


def available() -> bool:
    try:
        cut("pot")
        image("greenhouse")
    except (MissingArt, OSError, KeyError):
        return False
    return True


def _stand(canvas: Image.Image, sprite: Image.Image, center_x: int, bottom: int) -> tuple[int, int]:
    """Paste a sprite with the middle of its bottom edge at a point. Returns its top-left corner."""
    x, y = center_x - sprite.width // 2, bottom - sprite.height
    canvas.alpha_composite(sprite, (x, y))
    return x, y


def _number(canvas: Image.Image, text: str, x: int, y: int) -> None:
    for offset, color in (((1, 1), SHADOW), ((0, 0), INK)):
        cursor = x + offset[0]
        for char in text:
            bits = GLYPHS[char]
            for index, bit in enumerate(bits):
                if bit == "1":
                    canvas.putpixel((cursor + index % 3, y + offset[1] + index // 3), color)
            cursor += 4


def _pot(canvas: Image.Image, pot: dict[str, Any], center_x: int) -> None:
    empty, plant = cut("pot"), pot.get("plant")
    # the shadow a pot throws on the shelf
    canvas.alpha_composite(Image.new("RGBA", (empty.width - 2, 1), (40, 24, 12, 110)), (center_x - empty.width // 2 + 1, SHELF_Y))
    if pot.get("locked"):
        dim = ImageEnhance.Color(ImageEnhance.Brightness(empty).enhance(0.7)).enhance(0.35)
        _, top = _stand(canvas, dim, center_x, SHELF_Y)
        _stand(canvas, image("lock"), center_x, top - 1)
        return
    if plant is None:
        _stand(canvas, empty, center_x, SHELF_Y)
        return
    if plant["mature"]:
        _, top = _stand(canvas, plant_sprite(plant["species"], "potted"), center_x, SHELF_Y)
        canvas.alpha_composite(image("sparkle"), (center_x + 5, max(0, top - 3)))
        return
    _, top = _stand(canvas, empty, center_x, SHELF_Y)
    if plant["stage"] > 0:
        # Stage 0 is a seed under the soil, so only the pot shows.
        _, top = _stand(canvas, plant_sprite(plant["species"], "stages", plant["stage"] - 1), center_x, top + SOIL_DEPTH)
    if plant["needs_water"]:
        _stand(canvas, image("need_water"), center_x, max(12, top - 1))


def render_greenhouse(state: dict[str, Any], *, scale: int = SCALE) -> bytes:
    """A PNG of the greenhouse described by `store.get_state`."""
    canvas = image("greenhouse").copy()
    for pot, center_x in zip(state["pots"], POT_CENTERS):
        _pot(canvas, pot, center_x)

    barrel = state["barrel"]
    level = 0 if not barrel["stored"] else max(1, round(3 * barrel["stored"] / barrel["capacity"]))
    sprite = image(f"barrel_{level}")
    canvas.alpha_composite(sprite, (BARREL_AT[0], BARREL_AT[1] - sprite.height))

    # the two counters, on a dark plate so they read against the glass
    plate = Image.new("RGBA", (29, 25), (0, 0, 0, 0))
    ImageDraw.Draw(plate).rounded_rectangle([0, 0, 28, 24], radius=3, fill=(28, 32, 48, 185))
    canvas.alpha_composite(plate, (4, 5))
    canvas.alpha_composite(image("icon_water"), (7, 7))
    _number(canvas, str(min(999, state["water"])), 19, 9)
    canvas.alpha_composite(image("icon_sun"), (6, 18))
    _number(canvas, str(min(999, state["sunlight"])), 19, 20)

    big = canvas.resize((canvas.width * scale, canvas.height * scale), Image.NEAREST)
    out = io.BytesIO()
    big.save(out, format="PNG", optimize=True)
    return out.getvalue()
