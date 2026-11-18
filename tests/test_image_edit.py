import numpy as np
import pytest
from PIL import Image

from utils import image_edit as ed


def picture(size=(120, 80)) -> Image.Image:
    """Left half red, right half blue, with a green stripe along the top."""
    image = Image.new("RGBA", size, (200, 40, 40, 255))
    image.paste((40, 40, 200, 255), (size[0] // 2, 0, size[0], size[1]))
    image.paste((40, 200, 40, 255), (0, 0, size[0], size[1] // 8))
    return image


def test_mirror_reflects_the_kept_half():
    red, blue, green = (200, 40, 40, 255), (40, 40, 200, 255), (40, 200, 40, 255)
    assert ed.mirror(picture(), "left").getpixel((110, 40)) == red
    assert ed.mirror(picture(), "right").getpixel((10, 40)) == blue
    assert ed.mirror(picture(), "top").getpixel((10, 78)) == green
    assert ed.mirror(picture(), "bottom").getpixel((10, 2)) == red
    assert all(ed.mirror(picture(), side).size == (120, 80) for side in ed.MIRROR_SIDES)


def test_tile_repeats_the_picture_at_about_the_original_size():
    tiled = ed.tile(picture(), 3, 2)
    assert tiled.size == (120, 80)
    cell = tiled.crop((0, 0, 40, 40))
    assert tiled.crop((40, 40, 80, 80)).tobytes() == cell.tobytes() and tiled.crop((80, 0, 120, 40)).tobytes() == cell.tobytes()
    assert ed.tile(picture((100, 100)), 3, 3).size == (99, 99)


def test_circle_is_a_square_with_see_through_corners():
    result = ed.circle(picture())
    assert result.size == (80, 80) and result.getpixel((0, 0))[3] == 0 and result.getpixel((40, 40))[3] == 255
    faded = picture()
    faded.putalpha(90)
    assert ed.circle(faded).getpixel((40, 40))[3] == 90


def test_border_grows_the_picture_and_rounds_corners():
    framed = ed.border(picture(), (255, 255, 255), thickness=10)
    assert framed.size == (136, 96) and framed.getpixel((2, 2)) == (255, 255, 255, 255) and framed.getpixel((20, 40)) == (200, 40, 40, 255)
    rounded = ed.border(picture(), (255, 255, 255), thickness=10, rounding=30)
    assert rounded.size == (136, 96) and rounded.getpixel((0, 0))[3] == 0 and rounded.getpixel((68, 2)) == (255, 255, 255, 255)
    only_rounded = ed.border(picture(), (0, 0, 0), thickness=0, rounding=30)
    assert only_rounded.size == (120, 80) and only_rounded.getpixel((0, 0))[3] == 0 and only_rounded.getpixel((60, 40))[3] == 255


def test_colors_are_read_from_names_and_hex_codes():
    assert ed.parse_color("white") == (255, 255, 255)
    assert ed.parse_color("#5865F2") == ed.parse_color("5865f2") == (88, 101, 242)
    assert ed.parse_color(" Tomato ") == (255, 99, 71)
    with pytest.raises(ValueError, match="don't know the color"):
        ed.parse_color("blurple-ish")


def test_target_size_handles_each_way_of_asking():
    assert ed.target_size((400, 200), None, None, 50) == (200, 100)
    assert ed.target_size((400, 200), 100, None, None) == (100, 50)
    assert ed.target_size((400, 200), None, 50, None) == (100, 50)
    assert ed.target_size((400, 200), 30, 70, None) == (30, 70)
    assert ed.target_size((400, 200), None, None, 1) == (4, 2)
    assert ed.target_size((3, 900), 1, None, None) == (1, 300)
    with pytest.raises(ValueError, match="at most 4096"):
        ed.target_size((400, 200), None, None, 1000 * 2)
    with pytest.raises(ValueError, match="at most 1024"):
        ed.target_size((400, 200), 2000, None, None, max_edge=1024)
    with pytest.raises(ValueError, match="Give a width"):
        ed.target_size((400, 200), None, None, None)


def test_collage_layouts():
    wide, tall = picture((200, 100)), picture((100, 200))
    row = ed.collage([wide, tall])
    assert row.size == (400 + 100, 200)  # both brought to the taller one's height
    assert ed.collage([wide, tall, wide, tall]).size == (ed.COLLAGE_CELL * 2, ed.COLLAGE_CELL * 2)  # four make a grid by default
    three = ed.collage([wide, tall, wide], "grid")
    assert three.size == (ed.COLLAGE_CELL * 2, ed.COLLAGE_CELL * 2)
    assert three.getpixel((5, ed.COLLAGE_CELL + 50))[3] == 0  # the odd one sits centered, not at the left
    assert three.getpixel((ed.COLLAGE_CELL, ed.COLLAGE_CELL + 50))[3] == 255
    assert ed.collage([picture((3000, 2000)), picture((3000, 2000))]).size[1] == ed.COLLAGE_ROW_HEIGHT


def test_palette_finds_the_main_colors_most_common_first():
    image = Image.new("RGBA", (100, 100), (250, 10, 10, 255))
    image.paste((10, 10, 250, 255), (0, 0, 100, 30))
    image.paste((0, 0, 0, 0), (0, 90, 100, 100))  # see-through pixels are not a color
    swatches, codes = ed.palette(image, 2)
    assert codes == ["#FA0A0A", "#0A0AFA"]
    assert swatches.size == (240, 154) and swatches.getpixel((10, 10)) == (250, 10, 10, 255)
    with pytest.raises(ValueError, match="completely transparent"):
        ed.palette(Image.new("RGBA", (10, 10), (0, 0, 0, 0)))


def test_background_removal_keeps_the_subject_and_the_size():
    # A photo-like scene: a soft gray wall with a strongly colored figure in the middle.
    rng = np.random.default_rng(3)
    wall = (150 + rng.normal(0, 4, (240, 240, 3))).clip(0, 255).astype(np.uint8)
    image = Image.fromarray(wall, "RGB").convert("RGBA")
    figure = Image.new("RGBA", (90, 150), (190, 60, 40, 255))
    image.paste(figure, (75, 60))
    image.paste((230, 190, 160, 255), (95, 30, 145, 80))
    result = ed.remove_background(image)
    assert result.size == image.size and result.mode == "RGBA"
    alpha = np.asarray(result.getchannel("A"))
    assert alpha[120:160, 100:140].mean() > 200  # the figure stays
    assert alpha[:20, :20].mean() < 40 and alpha[-20:, -20:].mean() < 40  # the wall goes
    assert result.convert("RGB").tobytes() == image.convert("RGB").tobytes()  # only transparency changes
