"""Draws the placeholder art for the garden game into assets/garden/generated/.

Everything here stands in for art we do not have yet. Bought art lives in
assets/garden/packs/ and is never written by this script.

    .venv/bin/python scripts/garden_placeholders.py
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "assets" / "garden" / "generated"

GLASS, GLASS_LIGHT, FRAME = (176, 214, 208), (201, 230, 222), (104, 140, 128)
STONE, STONE_DARK, FLOOR, FLOOR_DARK = (140, 146, 138), (116, 122, 116), (118, 98, 78), (98, 80, 64)
WOOD, WOOD_LIGHT, WOOD_DARK = (142, 96, 56), (176, 124, 74), (98, 64, 38)
HOOP, WATER, WATER_LIGHT = (92, 98, 106), (72, 138, 214), (150, 198, 240)
OUTLINE = (44, 34, 30)


def greenhouse() -> Image.Image:
    """The room: glass wall, low stone wall, floor, and a shelf for three pots."""
    image = Image.new("RGBA", (160, 100), GLASS)
    d = ImageDraw.Draw(image)
    for x in range(0, 160, 32):  # panes, each with a lighter streak
        d.rectangle([x + 4, 6, x + 9, 50], fill=GLASS_LIGHT)
        d.line([x, 0, x, 58], fill=FRAME)
    d.line([0, 28, 159, 28], fill=FRAME)
    d.rectangle([0, 0, 159, 1], fill=FRAME)
    d.rectangle([0, 58, 159, 87], fill=STONE)
    for row, y in enumerate(range(58, 88, 6)):  # stone courses
        d.line([0, y, 159, y], fill=STONE_DARK)
        for x in range(8 if row % 2 else 0, 160, 16):
            d.line([x, y, x, y + 5], fill=STONE_DARK)
    d.rectangle([0, 88, 159, 99], fill=FLOOR)
    for x in range(0, 160, 20):
        d.line([x, 88, x, 99], fill=FLOOR_DARK)
    d.line([0, 88, 159, 88], fill=FLOOR_DARK)
    # the shelf: its top edge is y=70, where the pots stand
    for x in (40, 118):
        d.rectangle([x, 74, x + 2, 88], fill=WOOD_DARK)
    d.rectangle([34, 70, 126, 74], fill=WOOD)
    d.line([34, 70, 126, 70], fill=WOOD_LIGHT)
    d.line([34, 74, 126, 74], fill=WOOD_DARK)
    return image


def barrel(level: int) -> Image.Image:
    """The rain barrel, level 0 (empty) to 3 (full)."""
    image = Image.new("RGBA", (20, 26), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.rectangle([2, 4, 17, 24], fill=WOOD, outline=OUTLINE)
    d.rectangle([1, 8, 18, 20], fill=WOOD, outline=OUTLINE)
    for x in (5, 9, 13):
        d.line([x, 5, x, 23], fill=WOOD_DARK)
    d.line([3, 6, 3, 22], fill=WOOD_LIGHT)
    for y in (8, 20):
        d.line([1, y, 18, y], fill=HOOP)
    d.ellipse([2, 1, 17, 7], fill=OUTLINE)
    d.ellipse([3, 2, 16, 6], fill=(36, 28, 26) if level == 0 else WATER)
    if level:
        d.line([6, 3, 9, 3], fill=WATER_LIGHT)
    # a gauge on the front: three marks, lit up to the level
    for mark in range(3):
        d.rectangle([8, 17 - mark * 4, 11, 18 - mark * 4], fill=WATER if mark < level else WOOD_DARK)
    return image


def from_rows(rows: list[str], palette: dict[str, tuple[int, ...]]) -> Image.Image:
    image = Image.new("RGBA", (len(rows[0]), len(rows)), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, key in enumerate(row):
            if key != ".":
                image.putpixel((x, y), palette[key])
    return image


ICON_PALETTE = {
    "o": OUTLINE + (255,), "w": WATER + (255,), "l": WATER_LIGHT + (255,), "y": (250, 200, 60, 255), "Y": (255, 232, 130, 255),
    "g": (150, 156, 164, 255), "G": (104, 110, 120, 255), "W": (255, 255, 255, 255), "b": (236, 240, 244, 255),
}
ICONS = {
    "icon_water": ["...oo...", "..owwo..", "..owwo..", ".owlwwo.", ".owlwwo.", ".owwwwo.", "..owwo..", "...oo..."],
    "icon_sun": ["...y....", ".y.y.y..", "..yYy...", "yyYYYyy.", "..yYy...", ".y.y.y..", "...y....", "........"],
    "lock": ["..GGGG..", ".G....G.", ".G....G.", "oooooooo", "ogggggGo", "oggoggGo", "ogggggGo", "oooooooo"],
    "sparkle": ["...W...", "...W...", ".W.W.W.", "WWWYWWW", ".W.W.W.", "...W...", "...W..."],
    # a speech bubble with a drop in it: "I need water"
    "need_water": [
        ".ooooooo.", "obbbbbbbo", "obbbwbbbo", "obbwwwbbo", "obbwlwbbo", "obbbwbbbo", "obbbbbbbo", ".ooobooo.", "....o....",
    ],
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    greenhouse().save(OUT / "greenhouse.png")
    for level in range(4):
        barrel(level).save(OUT / f"barrel_{level}.png")
    for name, rows in ICONS.items():
        from_rows(rows, ICON_PALETTE).save(OUT / f"{name}.png")
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
