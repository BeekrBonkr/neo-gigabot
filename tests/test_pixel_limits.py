"""A picture is limited by its pixels, not only by its size on disk."""

from __future__ import annotations

import io

import pytest
from PIL import Image

from utils import imaging
from utils.imaging import ImageToolkit, check_canvas, frame_edge_limit, load_frames


def gif(edge: int, frames: int) -> bytes:
    base = Image.new("P", (edge, edge), 0)
    pictures = []
    for index in range(frames):
        picture = base.copy()
        picture.putpixel((index % edge, 0), 1 + index % 200)
        pictures.append(picture)
    out = io.BytesIO()
    pictures[0].save(out, format="GIF", save_all=True, append_images=pictures[1:], duration=50, loop=0)
    return out.getvalue()


def test_a_small_file_of_huge_frames_is_shrunk_as_it_is_read():
    data = gif(3000, 12)  # a few kilobytes that describe 108 megapixels
    assert len(data) < 200_000
    loaded = load_frames(data, "x.gif")
    assert len(loaded.frames) == 12
    assert sum(frame.width * frame.height for frame in loaded.frames) <= imaging.MAX_TOTAL_PIXELS
    assert max(loaded.frames[0].size) < 3000

    toolkit = ImageToolkit.__new__(ImageToolkit)
    sizes = []
    toolkit.process_image_bytes(data, "x.gif", lambda frame: sizes.append(frame.size) or frame)
    assert len(sizes) == 12 and sum(width * height for width, height in sizes) <= imaging.MAX_TOTAL_PIXELS


def test_ordinary_animations_are_left_at_their_own_size():
    assert frame_edge_limit((480, 270), 120) is None
    assert frame_edge_limit((480, 270), 120, 256) == 256
    assert load_frames(gif(200, 5), "x.gif").frames[0].size == (200, 200)
    assert frame_edge_limit((4000, 4000), 120) < 1000


def test_a_picture_over_the_limit_is_refused_with_a_message_a_person_can_act_on():
    out = io.BytesIO()
    Image.new("L", (9500, 9500), 0).save(out, format="PNG")  # 90 megapixels
    with pytest.raises(Image.DecompressionBombError) as caught:
        load_frames(out.getvalue(), "x.png")
    message = ImageToolkit.__new__(ImageToolkit).user_error_message(caught.value)
    assert "too large" in message and "megapixels" in message


def test_a_picture_under_the_limit_opens_at_full_size():
    out = io.BytesIO()
    Image.new("L", (6000, 5000), 0).save(out, format="PNG")  # 30 megapixels: a phone photo
    assert load_frames(out.getvalue(), "x.png").frames[0].size == (6000, 5000)


def test_an_overlay_cannot_stretch_the_result_past_the_limit():
    with pytest.raises(ValueError, match="too large"):
        check_canvas(7000, 7000)
    check_canvas(6000, 5000)
