import io

import numpy as np
from PIL import Image

from utils import image_carve as ic


def scene() -> Image.Image:
    """Flat gray with a detailed square in the middle: the part carving should spare."""
    pixels = np.full((60, 80, 3), 120, dtype=np.uint8)
    rng = np.random.default_rng(3)
    pixels[20:40, 30:50] = rng.integers(0, 255, (20, 20, 3), dtype=np.uint8)
    return Image.fromarray(pixels, "RGB")


def busy(image: Image.Image) -> int:
    pixels = np.asarray(image).astype(int)
    return int((np.abs(pixels - 120).sum(axis=2) > 0).sum())


def test_carving_reaches_the_asked_size_and_spares_the_detail():
    source = scene()
    carve = ic.plan(source, 50, 40)
    carved = ic.apply(source, carve)
    assert carve.size == carved.size == (50, 40)
    assert busy(carved) == busy(source) == 400  # every pixel taken out was flat background
    plain = source.resize((50, 40), Image.NEAREST)
    assert busy(plain) < 300  # a plain resize would have shrunk the square too


def test_each_seam_takes_one_pixel_from_every_row():
    energy = ic._energy(np.asarray(scene()))
    keep = ic._pick_seams(ic._path_costs(energy), 7)
    assert keep.shape == (60, 80) and (keep.sum(axis=1) == 73).all()
    assert keep[:, 30:50][20:40].all()  # none of them went through the square


def test_a_plan_is_replayed_on_other_frames_of_any_size():
    source = scene()
    carve = ic.plan(source, 40, 60)  # only the width is carved
    assert ic.apply(source, carve).size == (40, 60)
    other = Image.new("RGB", (160, 120), (10, 200, 30))
    replayed = ic.apply(other, carve)
    assert replayed.size == (40, 60) and replayed.getpixel((5, 5)) == (10, 200, 30)
    untouched = ic.plan(source, 80, 60)
    assert ic.apply(source, untouched).tobytes() == source.tobytes()


def test_every_frame_of_an_animation_is_carved_and_keeps_its_timing():
    frames = [Image.new("RGB", (40, 30), (index * 60, 90, 200 - index * 40)) for index in range(4)]
    buffer = io.BytesIO()
    frames[0].save(buffer, format="GIF", save_all=True, append_images=frames[1:], duration=[30, 40, 50, 60], loop=0)
    data, name = ic.carve_bytes(buffer.getvalue(), "a.gif", 50, 50, 450)
    result = Image.open(io.BytesIO(data))
    assert name == "contentaware.gif" and result.size == (20, 15) and result.n_frames == 4
    seen = []
    for index in range(4):
        result.seek(index)
        seen.append((result.info["duration"], result.convert("RGB").getpixel((5, 5))[0] // 30))
    assert seen == [(30, 0), (40, 2), (50, 4), (60, 6)]  # four different frames, not the last one repeated
    still = io.BytesIO()
    Image.new("RGB", (900, 600), (9, 9, 9)).save(still, format="PNG")
    data, name = ic.carve_bytes(still.getvalue(), "a.png", 50, 10, 450)
    assert name == "contentaware.png" and Image.open(io.BytesIO(data)).size == (225, 30)
