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


def test_respects_puts_the_picture_in_the_portrait_frame():
    scene, plain = mm.respects(picture()), mm._template("respects.jpg")
    assert scene.size == plain.size == (1280, 720)
    changed = np.argwhere(np.asarray(scene) != np.asarray(plain))
    (top, left), (bottom, right) = changed[:, :2].min(axis=0), changed[:, :2].max(axis=0)
    xs, ys = [x for x, _ in mm.RESPECTS_PORTRAIT], [y for _, y in mm.RESPECTS_PORTRAIT]
    assert min(xs) - 2 <= left and right <= max(xs) + 2 and min(ys) - 2 <= top and bottom <= max(ys) + 2  # nothing else is touched


def test_who_did_this_fills_the_gap_between_the_title_and_the_faces():
    scene = mm.who_did_this(picture())
    left, top, right, bottom = mm.WHO_DID_THIS_BOX
    assert scene.size == (717, 717)
    assert scene.getpixel((left + 40, (top + bottom) // 2))[:3] == (40, 60, 200) and scene.getpixel((right - 40, (top + bottom) // 2))[:3] == (200, 60, 40)
    assert scene.getpixel((360, 20)) == mm._template("whodidthis.png").getpixel((360, 20))


def test_wheeze_swaps_the_bubble_words_for_the_picture():
    scene = mm.wheeze(picture())
    left, top, right, bottom = mm.WHEEZE_PICTURE
    assert scene.size == mm._template("wheeze.png").size
    assert scene.getpixel(((left + right) // 2 + 20, (top + bottom) // 2))[:3] == (200, 60, 40)
    assert scene.getpixel((left + 2, bottom - 2))[:3] == (255, 255, 255)  # the old words are wiped, not just covered


def test_keep_item_shows_the_picture_above_the_note():
    card = mm.keep_item(picture((300, 100)))
    assert card.size == (mm.KEEP_ITEM_WIDTH, 640)
    assert card.getpixel((mm.KEEP_ITEM_WIDTH // 2 + 60, 240))[:3] == (200, 60, 40)
    assert card.getpixel((84, 522))[:3] != (255, 255, 255)  # the check mark's disc


def test_achievement_writes_on_the_popup_and_keeps_its_corners_clear():
    blank, named = mm._template("achievement.png"), mm.achievement("Did a thing")
    assert named.size == blank.size and named.getpixel((0, 0))[3] == 0
    assert named.tobytes() != blank.tobytes() and mm.achievement("x" * 60).size == blank.size


def test_rip_carves_words_and_an_optional_picture():
    words, with_picture = mm.rip("Somebody"), mm.rip("Somebody", picture())
    assert words.size == with_picture.size == (600, 900)
    assert words.tobytes() != mm._template("rip.jpg").tobytes() and words.tobytes() != with_picture.tobytes()
    red, green, blue, _ = with_picture.getpixel(((mm.RIP_STONE[0] + mm.RIP_STONE[2]) // 2, mm.RIP_STONE[1] + 200))
    assert max(red, green, blue) - min(red, green, blue) < 12  # the picture is carved in gray


def test_rtx_puts_the_pictures_side_by_side():
    off, on = Image.new("RGBA", (50, 80), (10, 200, 10, 255)), Image.new("RGBA", (90, 40), (10, 10, 200, 255))
    result = mm.rtx(off, on)
    assert result.size == (mm.RTX_PANEL * 2 + mm.RTX_GAP, mm.RTX_PANEL)
    assert result.getpixel((240, 40))[:3] == (10, 200, 10) and result.getpixel((mm.RTX_PANEL + mm.RTX_GAP + 240, 40))[:3] == (10, 10, 200)
    assert result.getpixel((30, mm.RTX_PANEL - 80))[:3] != (10, 200, 10)  # a badge sits on the lower left


def test_news_covers_the_frame_and_writes_the_headline():
    quiet, loud = mm.news(picture(), ""), mm.news(picture(), "something happened")
    assert quiet.size == loud.size == mm.NEWS_SIZE
    assert quiet.getpixel((300, 200))[:3] == (40, 60, 200)  # the picture shows above the banner
    left, top, right, bottom = mm.NEWS_HEADLINE
    assert quiet.crop((left, top, right, bottom)).tobytes() != loud.crop((left, top, right, bottom)).tobytes()


def test_presidential_alert_fits_long_text():
    blank = mm._template("presidential.png")
    assert mm.presidential("x " * 80).size == blank.size and mm.presidential("hello").tobytes() != blank.tobytes()


def test_notification_lays_a_banner_over_the_top():
    scene = mm.notification(picture((120, 400)), "hello there", "Somebody")
    assert scene.size == (mm.NOTIFICATION_WIDTH, 960)  # tall pictures are cropped, not squeezed
    assert min(scene.getpixel((320, 30))[:3]) > 200  # the pale banner
    assert scene.getpixel((40, 600))[:3] == (40, 60, 200)
    assert mm.notification(picture((400, 40)), "hi", "x").size == (mm.NOTIFICATION_WIDTH, 300)
