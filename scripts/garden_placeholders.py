"""Draws the placeholder art for the garden game into assets/garden/generated/.

Everything here stands in for art we do not have yet. Bought art lives in
assets/garden/packs/ and is never written by this script.

    .venv/bin/python scripts/garden_placeholders.py
"""

from __future__ import annotations

import math
import random
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "assets" / "garden" / "generated"

Color = tuple[int, ...]


def shade(color: Color, factor: float) -> Color:
    return tuple(max(0, min(255, round(v * factor))) for v in color[:3])


def from_rows(rows: list[str], palette: dict[str, Color]) -> Image.Image:
    image = Image.new("RGBA", (len(rows[0]), len(rows)), (0, 0, 0, 0))
    for y, row in enumerate(rows):
        for x, key in enumerate(row):
            if key != ".":
                image.putpixel((x, y), palette[key][:3] + (255,))
    return image


# ---------- the room ----------

ROOM = (108, 72)
SILL_Y, WALL_Y, FLOOR_Y = 45, 48, 62      # where the window ends, the wall starts, the floor starts
SHELF = (32, 104, 52)                     # left, right, top edge (the pots stand on the top edge)
MULLIONS = (0, 27, 54, 81, 106)

SKY = [(138, 194, 232), (156, 205, 236), (175, 216, 240), (194, 227, 243), (211, 236, 245)]
HILL_FAR, HILL_NEAR, TREE, TREE_DARK = (158, 204, 160), (116, 178, 124), (84, 150, 98), (62, 122, 82)
FRAME, FRAME_SHADE = (240, 243, 235), (172, 188, 180)
STONES = [(150, 160, 166), (140, 151, 158), (158, 167, 171), (146, 155, 164)]
MORTAR = (112, 123, 132)
FLOOR, FLOOR_LIGHT, FLOOR_SEAM = (144, 107, 76), (180, 140, 100), (106, 76, 54)
WOOD, WOOD_LIGHT, WOOD_DARK = (184, 128, 74), (228, 176, 114), (122, 80, 46)
LEAF, LEAF_LIGHT, STEM = (70, 148, 84), (112, 190, 104), (56, 112, 70)


def room() -> Image.Image:
    """Inside the greenhouse: a window onto hills, a low stone wall, a plank
    floor and a shelf for three pots."""
    width, height = ROOM
    image = Image.new("RGBA", ROOM, SKY[0] + (255,))
    d = ImageDraw.Draw(image)
    rng = random.Random(11)

    for y in range(SILL_Y):  # sky, in bands rather than a smooth blend
        d.line([0, y, width, y], fill=SKY[min(len(SKY) - 1, y * len(SKY) // SILL_Y)])
    for cx, cy, half in ((17, 9, 8), (68, 6, 10), (94, 24, 6), (40, 27, 5)):  # clouds
        d.ellipse([cx - half, cy - 2, cx + half, cy + 2], fill=(252, 254, 255))
        d.ellipse([cx - half // 2, cy - 4, cx + half // 2 + 1, cy + 1], fill=(252, 254, 255))
        d.line([cx - half + 2, cy + 2, cx + half - 2, cy + 2], fill=(222, 236, 246))
    for x in range(width):  # two rows of hills
        far = round(35 + 2.5 * math.sin(x / 11.0) + 1.5 * math.sin(x / 5.0 + 1.0))
        near = round(40 + 2.5 * math.sin(x / 8.0 + 2.0) + 1.2 * math.sin(x / 3.5))
        d.line([x, far, x, SILL_Y], fill=HILL_FAR)
        d.line([x, near, x, SILL_Y], fill=HILL_NEAR)
    for cx, cy in ((12, 37), (47, 39), (74, 36), (92, 40)):  # a few trees on the near hill
        d.ellipse([cx - 3, cy - 4, cx + 3, cy + 1], fill=TREE)
        d.ellipse([cx - 1, cy - 2, cx + 3, cy + 1], fill=TREE_DARK)
        d.point([(cx - 1, cy - 3), (cx - 2, cy - 2)], fill=(120, 184, 120))
        d.line([cx, cy + 2, cx, cy + 3], fill=(96, 78, 62))

    # light catching the glass: soft diagonal streaks in every pane
    sheen = Image.new("RGBA", ROOM, (0, 0, 0, 0))
    s = ImageDraw.Draw(sheen)
    s.rectangle([0, 0, width, SILL_Y - 1], fill=(226, 246, 240, 26))
    for left in MULLIONS[:-1]:
        for start, length, alpha in ((7, 13, 80), (11, 9, 50), (17, 6, 62)):
            for step in range(length):
                s.point((left + start + 4 - step // 3, 4 + step), fill=(255, 255, 255, alpha))
                s.point((left + start + 5 - step // 3, 4 + step), fill=(255, 255, 255, alpha // 2))
        for step in range(8):
            s.point((left + 20 - step // 3, 26 + step), fill=(255, 255, 255, 56))
    image.alpha_composite(sheen)
    d = ImageDraw.Draw(image)

    # painted window frame: light on the left and top, shaded on the right and underneath
    d.line([0, 0, width, 0], fill=FRAME)
    d.line([0, 1, width, 1], fill=FRAME)
    d.line([0, 2, width, 2], fill=FRAME_SHADE)
    d.line([0, 17, width, 17], fill=FRAME)
    d.line([0, 18, width, 18], fill=FRAME_SHADE)
    for x in MULLIONS:
        d.line([x, 0, x, SILL_Y], fill=FRAME)
        d.line([x + 1, 0, x + 1, SILL_Y], fill=FRAME_SHADE)
    d.line([0, SILL_Y, width, SILL_Y], fill=(246, 242, 228))        # the sill
    d.line([0, SILL_Y + 1, width, SILL_Y + 1], fill=(216, 208, 188))
    d.line([0, SILL_Y + 2, width, SILL_Y + 2], fill=(150, 144, 130))

    # stone wall: three courses of blocks, staggered, each a slightly different grey
    course_tops = (WALL_Y, WALL_Y + 5, WALL_Y + 10, FLOOR_Y)
    for row in range(3):
        top, bottom = course_tops[row], course_tops[row + 1] - 1
        offset = 0 if row % 2 else -6
        for left in range(offset, width, 12):
            stone = STONES[rng.randrange(len(STONES))]
            d.rectangle([left, top, left + 11, bottom], fill=stone)
            d.line([left + 1, top + 1, left + 10, top + 1], fill=shade(stone, 1.07))  # a lit top edge
            d.line([left, top, left, bottom], fill=MORTAR)
        d.line([0, top, width, top], fill=MORTAR)
    d.line([0, WALL_Y, width, WALL_Y], fill=shade(MORTAR, 0.85))  # the sill's shadow

    # plank floor
    d.rectangle([0, FLOOR_Y, width, height], fill=FLOOR)
    d.line([0, FLOOR_Y, width, FLOOR_Y], fill=FLOOR_LIGHT)
    d.line([0, FLOOR_Y + 5, width, FLOOR_Y + 5], fill=FLOOR_SEAM)
    for x in range(9, width, 22):
        d.line([x, FLOOR_Y + 1, x, FLOOR_Y + 4], fill=FLOOR_SEAM)
        d.line([x + 11, FLOOR_Y + 6, x + 11, height], fill=FLOOR_SEAM)
    for _ in range(46):  # wood grain
        x, y = rng.randrange(width - 3), rng.randrange(FLOOR_Y + 1, height)
        if y != FLOOR_Y + 5:
            d.line([x, y, x + rng.randrange(1, 4), y], fill=shade(FLOOR, rng.choice((0.9, 1.08))))

    # the shelf, with brackets and the shadow it throws on the wall
    left, right, top = SHELF
    shadow = Image.new("RGBA", ROOM, (0, 0, 0, 0))
    ImageDraw.Draw(shadow).rectangle([left + 2, top + 4, right - 1, top + 6], fill=(20, 26, 40, 60))
    image.alpha_composite(shadow)
    d = ImageDraw.Draw(image)
    for x in (left + 4, right - 6):
        d.rectangle([x, top + 4, x + 2, FLOOR_Y], fill=WOOD_DARK)
        d.line([x, top + 4, x, FLOOR_Y], fill=shade(WOOD, 0.85))
        d.line([x + 3, top + 4, x + 6 if x < 60 else x + 3, top + 4], fill=WOOD_DARK)
    d.rectangle([left, top, right, top + 3], fill=WOOD)
    d.line([left, top, right, top], fill=WOOD_LIGHT)
    d.line([left, top + 3, right, top + 3], fill=WOOD_DARK)
    d.line([left, top, left, top + 3], fill=shade(WOOD, 0.8))
    d.line([right, top, right, top + 3], fill=shade(WOOD, 0.8))
    for x in range(left + 5, right - 4, 9):
        d.line([x, top + 1, x + 3, top + 1], fill=shade(WOOD, 0.9))
        d.point((x + 5, top + 2), fill=shade(WOOD, 1.1))

    # ivy trailing down from the top of the frame
    for start, length in ((100, 15), (58, 9), (22, 6), (84, 5)):
        x = start
        for step in range(length):
            y = 3 + step
            x += (0, 0, 1, 0, -1, 0)[(step + start) % 6]
            d.point((x, y), fill=STEM)
            if step % 3 == 1:
                side = 1 if (step // 3 + start) % 2 else -1
                d.point((x + side, y), fill=LEAF)
                d.point((x + 2 * side, y), fill=LEAF_LIGHT)
                d.point((x + side, y + 1), fill=LEAF)
        d.point((x, 3 + length), fill=LEAF_LIGHT)
    return image


# ---------- the rain barrel ----------

BARREL_WOOD, HOOP = (166, 112, 66), (124, 132, 146)
WATER, WATER_LIGHT, WATER_DARK = (74, 146, 224), (188, 224, 250), (44, 104, 190)
OUTLINE = (60, 42, 32)


def barrel(level: int) -> Image.Image:
    """A round barrel lit from the left, with a gauge on the front. Level 0 (empty) to 3 (full)."""
    image = Image.new("RGBA", (20, 28), (0, 0, 0, 0))
    d = ImageDraw.Draw(image)
    d.ellipse([1, 23, 18, 27], fill=(20, 22, 34, 70))  # shadow on the floor
    for y in range(5, 25):
        bulge = 1 if 9 <= y <= 20 else 0
        left, right = 2 - bulge, 17 + bulge
        for x in range(left, right + 1):
            t = (x - left) / (right - left)
            light = 1.2 - 0.62 * abs(t - 0.3) - (0.14 if t > 0.72 else 0)
            base = HOOP if y in (8, 9, 20, 21) else BARREL_WOOD
            if base is BARREL_WOOD and x in (5, 9, 13):
                light *= 0.84                      # the gaps between staves
            if base is HOOP and y in (8, 20) and 0.2 < t < 0.42:
                light *= 1.25                      # a glint on the hoops
            image.putpixel((x, y), shade(base, light) + (255,))
        image.putpixel((left, y), OUTLINE + (255,))
        image.putpixel((right, y), OUTLINE + (255,))
    d.line([3, 25, 16, 25], fill=OUTLINE)
    # the open top
    d.ellipse([1, 1, 18, 8], fill=OUTLINE)
    d.ellipse([2, 2, 17, 7], fill=shade(BARREL_WOOD, 1.25))
    d.ellipse([3, 3, 16, 6], fill=WATER if level else (50, 36, 30))
    if level:
        d.line([5, 4, 9, 4], fill=WATER_LIGHT)
        d.line([11, 5, 13, 5], fill=WATER_DARK)
    # a glass gauge: how full the barrel is
    d.rectangle([8, 11, 11, 19], fill=(214, 220, 228))
    d.rectangle([9, 12, 10, 18], fill=(34, 42, 58))
    filled = (0, 2, 5, 7)[level]
    if filled:
        d.rectangle([9, 19 - filled, 10, 18], fill=WATER)
        d.point((9, 19 - filled), fill=WATER_LIGHT)
    return image


# ---------- small pieces ----------

PALETTE: dict[str, Color] = {
    "d": (28, 66, 138), "w": WATER, "l": WATER_LIGHT, "b": WATER_DARK,                          # water
    "r": (244, 150, 40), "y": (255, 208, 64), "Y": (255, 240, 158), "o": (238, 170, 50),        # sun
    "s": (176, 182, 194), "S": (120, 126, 140), "g": (232, 184, 72), "G": (252, 220, 120),      # lock
    "h": (196, 146, 50), "k": (92, 62, 24), "e": (120, 84, 30),
    "W": (255, 255, 255), "c": (70, 84, 112), "p": (244, 247, 252), "q": (214, 222, 236),       # bubble, sparkle
}
PIECES = {
    "icon_water": [
        "...dd...", "..dwwd..", "..dwwd..", ".dwlwwd.", ".dwlwwd.", "dwlwwwwd", "dwwwwwbd", ".dwwbbd.", "..dddd..",
    ],
    "icon_sun": [
        "....r....", ".r.....r.", "...yyy...", "..yYYyy..", "r.yYyyy.r", "..yyyyo..", "...yoo...", ".r.....r.", "....r....",
    ],
    "lock": [
        "..ssss..", ".sS..sS.", ".sS..sS.", "eeeeeeee", "eGGgggge", "eGgkkghe", "eggkkghe", "egghhhhe", "eeeeeeee",
    ],
    "sparkle": ["...W...", "...W...", "..WYW..", "WWYYYWW", "..WYW..", "...W...", "...W..."],
    # a speech bubble with a drop in it: "I need water"
    "need_water": [
        "..ccccccc..", ".cpppppppc.", "cppppdppppc", "cpppdwdpppc", "cppdwlwdppc", "cppdwwwdppc", "cpppdddpppc",
        ".cqqqqqqqc.", "..cccqccc..", "....cqc....", ".....c.....",
    ],
}


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    for stale in OUT.glob("*.png"):
        stale.unlink()
    room().save(OUT / "greenhouse.png")
    for level in range(4):
        barrel(level).save(OUT / f"barrel_{level}.png")
    for name, rows in PIECES.items():
        from_rows(rows, PALETTE).save(OUT / f"{name}.png")
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))


if __name__ == "__main__":
    main()
