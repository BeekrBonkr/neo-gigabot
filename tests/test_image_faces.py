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
