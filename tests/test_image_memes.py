import numpy as np
from PIL import Image

from utils import image_memes as mm


def picture(size=(120, 90)) -> Image.Image:
    image = Image.new("RGBA", size, (200, 60, 40, 255))
    image.paste((40, 60, 200, 255), (0, 0, size[0] // 2, size[1]))
    return image


def test_speech_bubble_cuts_the_top_out_and_leaves_the_rest():
    result = mm.speech_bubble(picture())
    assert result.size == (120, 90)
    assert result.getpixel((60, 2))[3] == 0  # the bubble is see-through
    assert result.getpixel((60, 80)) == picture().getpixel((60, 80))  # the bottom is untouched
    left, right = mm.speech_bubble(picture(), "left"), mm.speech_bubble(picture(), "right")
    assert left.tobytes() != right.tobytes()
    # the point reaches below the bubble on the side it aims at
    row = int(90 * 0.20 * 1.5)
    assert left.getpixel((int(120 * 0.36), row))[3] < 255 and left.getpixel((int(120 * 0.72), row))[3] == 255
    faded = picture()
    faded.putalpha(100)
    assert mm.speech_bubble(faded).getpixel((60, 80))[3] == 100  # existing transparency is kept, not replaced


def test_jail_and_wasted_keep_the_size_and_change_the_picture():
    for effect in (mm.jail, mm.wasted):
        result = effect(picture())
        assert result.mode == "RGBA" and result.size == (120, 90) and result.tobytes() != picture().tobytes()
    red, green, blue, _ = mm.wasted(picture()).getpixel((5, 5))
    assert max(red, green, blue) - min(red, green, blue) < 40  # nearly gray
    band = np.asarray(mm.wasted(picture((300, 200))).convert("RGB"))[80:120]
    assert ((band[..., 0] > 150) & (band[..., 1] < 80)).any()  # red lettering in the middle band
    bars = np.asarray(mm.jail(picture((280, 200))).convert("RGB")).astype(int)
    column = bars[100, 20]  # the middle of the first bar
    assert abs(column[0] - column[2]) < 30 and column.max() < 150


def test_posters_have_a_fixed_size_whatever_the_picture():
    for size in ((40, 400), (400, 40), (120, 90)):
        assert mm.wanted(picture(size), "Somebody", "$1").size == mm.WANTED_SIZE
    assert mm.wanted(picture(), "", "$5,000").tobytes() != mm.wanted(picture(), "A Name", "$5,000").tobytes()
    long_name = mm.wanted(picture(), "W" * 40, "$" + "9" * 19)  # the longest text the command allows still fits
    assert long_name.size == mm.WANTED_SIZE
    assert mm.change_my_mind("x" * 140).size == mm.CMM_SIZE
    assert mm.change_my_mind("one").tobytes() != mm.change_my_mind("two").tobytes()


def test_drake_panels_take_text_or_a_picture():
    words = mm.drake("no", "yes")
    assert words.size == (mm.DRAKE_PANEL * 2, mm.DRAKE_PANEL * 2)
    with_picture = mm.drake("no", "", None, picture())
    # the bottom right panel is the picture, the top right is still text on white
    assert with_picture.getpixel((mm.DRAKE_PANEL + 300, mm.DRAKE_PANEL + 200))[:3] == (200, 60, 40)
    assert with_picture.getpixel((mm.DRAKE_PANEL + 10, 10))[:3] == (255, 255, 255)
    top, bottom = words.crop((0, 0, 400, 400)), words.crop((0, 400, 400, 800))
    assert top.tobytes() != bottom.tobytes()  # two different poses
