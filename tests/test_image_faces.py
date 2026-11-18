from PIL import Image

from utils import image_faces as fx

FACE = (60, 40, 140, 140)


def plain(size=(200, 200)) -> Image.Image:
    image = Image.new("RGBA", size, (90, 120, 150, 255))
    image.paste((230, 190, 160, 255), FACE)
    return image


def test_joy_covers_the_face_and_leaves_the_rest():
    result = fx.joy(plain(), [FACE])
    assert result.size == (200, 200) and result.getpixel((100, 70)) != plain().getpixel((100, 70))
    assert result.getpixel((5, 5)) == plain().getpixel((5, 5)) and fx.joy(plain(), []).tobytes() == plain().tobytes()


def test_clown_puts_a_red_nose_below_the_middle_of_the_face():
    result = fx.clown(plain(), [FACE])
    red, green, blue, _ = result.getpixel((100, 40 + 62))
    assert red > 180 and green < 90 and blue < 90
    assert result.getpixel((100, 50)) == plain().getpixel((100, 50))  # the forehead is untouched
    assert fx.clown(plain(), [(0, 0, 6, 6)]).size == (200, 200)  # a face at the edge still fits
