import io

import pytest
from PIL import Image

from utils import image_animate as an
from utils.imaging import Frames, encode_frames, load_frames


def still(size=(60, 40)) -> Frames:
    image = Image.new("RGBA", size, (200, 40, 40, 255))
    image.paste((40, 40, 200, 255), (0, 0, size[0] // 2, size[1]))
    return Frames([image], [100])


def moving(count=6, durations=None) -> Frames:
    frames = [Image.new("RGBA", (30, 20), (index * 40, 100, 200, 255)) for index in range(count)]
    return Frames(frames, durations or [100] * count, animated=True)


@pytest.mark.parametrize("build", [an.spin, an.rainbow, an.wave, an.zoom])
def test_stills_become_a_loop_and_gifs_keep_their_frames(build):
    frames, durations = build(still())
    assert len(frames) == len(durations) == an.STILL_FRAMES and all(frame.mode == "RGBA" for frame in frames)
    assert len({frame.tobytes() for frame in frames}) > 1  # it really moves
    frames, durations = build(moving(durations=[30, 40, 50, 60, 70, 80]))
    assert len(frames) == 6 and durations == [30, 40, 50, 60, 70, 80]


def test_spin_is_a_circle_that_turns_the_asked_way():
    clockwise, _ = an.spin(still((40, 40)), 50, True)
    counter, _ = an.spin(still((40, 40)), 50, False)
    assert clockwise[0].size == (40, 40) and clockwise[0].getpixel((0, 0))[3] == 0 and clockwise[0].getpixel((20, 20))[3] == 255
    assert clockwise[3].tobytes() != counter[3].tobytes()
    assert clockwise[3].tobytes() == counter[an.STILL_FRAMES - 3].tobytes()


def test_pet_and_bonk_build_a_fixed_animation_around_the_picture():
    frames, durations = an.pet(still(), 40)
    assert len(frames) == an.PET_FRAMES and frames[0].size == (an.PET_SIZE, an.PET_SIZE) and set(durations) == {40}
    frames, durations = an.bonk(still(), 50)
    assert len(frames) == len(an.BONK_LIFTS) and frames[0].size == Image.open(an.BONK_SCENE).size
    assert max(durations) == 200 and durations.count(200) == 1  # the hit is held
    hit = an.BONK_LIFTS.index(0)
    left, top, right, bottom = an.BONK_TARGET_BOX
    inside = ((left + right) // 2 + 20, bottom - 40)
    assert frames[0].getpixel(inside) == (200, 40, 40, 255)  # the picture stands on the floor of its box
    data, name = encode_frames(frames, durations, "bonk")
    result = Image.open(io.BytesIO(data))
    total = 0
    for index in range(result.n_frames):
        result.seek(index)
        total += result.info["duration"]
    # the encoder merges frames that are identical (the held hit), keeping the loop's length
    assert name == "bonk.gif" and 6 <= result.n_frames <= len(an.BONK_LIFTS) and total == sum(durations)
    assert frames[hit].tobytes() != frames[0].tobytes() and frames[hit].tobytes() == frames[hit + 1].tobytes()


def test_boom_swells_the_picture_then_plays_the_explosion():
    frames, durations = an.boom(still(), 30)
    swell = an.BOOM_SWELL_FRAMES
    assert len(frames) == len(durations) == swell + an.BOOM_FRAMES and {frame.size for frame in frames} == {(60, 40)}
    assert durations == [30] * swell + [an.BOOM_FRAME_MS] * an.BOOM_FRAMES
    assert frames[0].tobytes() == still().frames[0].tobytes()  # it starts untouched
    assert len({frame.tobytes() for frame in frames[:swell]}) == swell  # every frame is swollen further than the last
    assert min(frames[swell + 1].getpixel((30, 20))[:3]) > 200  # the explosion opens on a bright bang
    red, _, blue, _ = frames[swell + 12].getpixel((30, 12))  # then the fireball: orange
    assert red > 150 and red > blue
    data, name = encode_frames(frames, durations, "boom")
    assert name == "boom.gif" and Image.open(io.BytesIO(data)).n_frames > an.BOOM_FRAMES


def test_boom_swell_eases_in_along_its_curve():
    steps = [an._bezier_ease(index / 10) for index in range(11)]
    assert steps[0] == pytest.approx(0, abs=1e-6) and steps[-1] == pytest.approx(1, abs=1e-6) and steps == sorted(steps)
    assert steps[5] < 0.2  # half the time gone, most of the swell still to come
    assert an._bezier_ease(0.3, (0.25, 0.25, 0.75, 0.75)) == pytest.approx(0.3, abs=1e-4)  # a straight curve is linear


def test_boom_swells_a_gif_over_its_own_frames():
    frames, durations = an.boom(moving(count=12, durations=[30] * 12))
    assert len(frames) == 12 + an.BOOM_FRAMES and durations[:12] == [30] * 12
    frames, durations = an.boom(moving(count=3, durations=[30, 40, 50]))  # too short to swell in one pass, so it repeats
    assert len(frames) == 9 + an.BOOM_FRAMES and durations[:9] == [30, 40, 50] * 3


def test_triggered_shakes_the_picture_over_its_label():
    frames, durations = an.triggered(still(), 50)
    assert len(frames) == len(an.TRIGGERED_SHAKES) and frames[0].size == (an.TRIGGERED_SIZE, an.TRIGGERED_SIZE) and set(durations) == {50}
    assert len({frame.tobytes() for frame in frames}) == len(frames)  # every frame is in a different place
    red, green, blue, _ = frames[0].getpixel((128, an.TRIGGERED_SIZE - 20))  # the label: black letters on orange
    assert red >= green >= blue


def test_speed_scales_timing_and_drops_frames_it_cannot_show():
    frames, durations = an.speed(moving(), 2.0)
    assert len(frames) == 6 and durations == [50] * 6
    frames, durations = an.speed(moving(), 0.5)
    assert durations == [200] * 6
    frames, durations = an.speed(moving(count=12, durations=[40] * 12), 8.0)  # 5ms frames: below what a GIF can show
    assert len(frames) == 3 and durations == [20, 20, 20] and sum(durations) == 60  # the loop is still 8 times as fast
    with pytest.raises(ValueError, match="single frame"):
        an.speed(moving(count=2, durations=[20, 20]), 10.0)


def test_gif_tools_refuse_stills_with_a_reason():
    for call in (lambda: an.speed(still(), 2.0), lambda: an.reverse(still()), lambda: an.pick_frame(still(), 1)):
        with pytest.raises(ValueError, match="needs a GIF"):
            call()


def test_reverse_and_pick_frame():
    source = moving(durations=[10, 20, 30, 40, 50, 60])
    frames, durations = an.reverse(source)
    assert durations == [60, 50, 40, 30, 20, 10] and frames[0] is source.frames[-1]
    frame, summary = an.pick_frame(source, 2)
    assert frame is source.frames[1] and summary == "Frame 2 of 6 · 30x20 · 0.2s per loop"
    with pytest.raises(ValueError, match="6 frames"):
        an.pick_frame(source, 7)


def test_join_fits_every_picture_on_the_first_ones_canvas():
    wide = Frames([Image.new("RGBA", (80, 20), (0, 255, 0, 255))], [100])
    frames, durations = an.join([still(), wide], 300)
    assert [frame.size for frame in frames] == [(60, 40), (60, 40)] and durations == [300, 300]
    assert frames[1].getpixel((30, 20))[1] == 255 and frames[1].getpixel((30, 2))[3] == 0  # letterboxed, not stretched


def test_a_real_gif_round_trips_through_the_tools():
    buffer = io.BytesIO()
    source = moving()
    source.frames[0].save(buffer, format="GIF", save_all=True, append_images=source.frames[1:], duration=[30, 40, 50, 60, 70, 80], loop=0)
    loaded = load_frames(buffer.getvalue(), "a.gif")
    frames, durations = an.reverse(loaded)
    data, name = encode_frames(frames, durations, "reverse")
    result = Image.open(io.BytesIO(data))
    assert name == "reverse.gif" and result.n_frames == 6 and result.info["duration"] == 80
