"""GIFs and videos going into the image commands: what comes back is a GIF
that still moves, and that lasts as long as what went in."""

import asyncio
import io

import pytest
from PIL import Image

import test_image_menu as menu
from cogs import image_menu
from utils.imaging import Frames, gif_durations, play_together, thin_frames

UNEVEN = [40, 40, 40, 200, 40, 40, 40, 40, 40, 40]  # one frame that lingers, as real GIFs have


def moving(durations=UNEVEN, size=(96, 72)) -> bytes:
    frames = []
    for index in range(len(durations)):
        frame = Image.new("RGBA", size, (20 * index, 120, 200, 255))
        frame.paste((255, 255 - 20 * index, 0, 255), (6 * index, 10, 6 * index + 24, 50))
        frames.append(frame)
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=list(durations), loop=0)
    return out.getvalue()


def timing(data: bytes) -> list[int]:
    image = Image.open(io.BytesIO(data))
    times = []
    for index in range(getattr(image, "n_frames", 1)):
        image.seek(index)
        image.load()
        times.append(int(image.info.get("duration", 0) or 0))
    return times


def test_gif_times_keep_the_whole_length():
    video = [66, 67, 67] * 10  # 15 frames a second, which a GIF cannot hold exactly
    shown = gif_durations(video)
    assert all(time % 10 == 0 and time >= 20 for time in shown) and abs(sum(shown) - sum(video)) <= 5
    assert gif_durations([40, 200, 40]) == [40, 200, 40] and gif_durations([5, 5]) == [20, 20]
    frames, times = thin_frames(list(range(10)), UNEVEN, 4)
    assert len(frames) == 4 and frames[0] == 0 and sum(times) == sum(UNEVEN)
    assert thin_frames([1, 2], [50, 50], 4) == ([1, 2], [50, 50])


def test_inputs_playing_together_follow_the_clock():
    def clip(count, each):
        return Frames([Image.new("RGBA", (4, 4), (index, 0, 0, 255)) for index in range(count)], [each] * count, 0, count > 1)

    long, short, still = clip(6, 100), clip(2, 100), clip(1, 100)
    together, times, _ = play_together([short, long, still])
    assert times == [100] * 6  # the one with the most frames sets the pace
    assert [picture[0].getpixel((0, 0))[0] for picture in together] == [0, 1, 0, 1, 0, 1]  # the shorter one loops
    assert [picture[1].getpixel((0, 0))[0] for picture in together] == [0, 1, 2, 3, 4, 5]
    assert all(picture[2] is still.frames[0] for picture in together)
    assert play_together([still, still])[1] == [100] and len(play_together([long], limit=3)[0]) == 3


ANSWERS = dict(menu.FORM_ANSWERS)
STILL_ON_PURPOSE = {"animate frames", "edit palette"}  # one frame, and a swatch of colors
OWN_PACE = {"animate speed", "animate boom", "animate pet", "animate bonk", "animate triggered", "edit removebg"}


def test_every_command_gives_a_moving_gif_for_a_moving_input(storage):
    async def body(harness):
        results = {}
        for name, command in harness.offered().items():
            if name.rsplit(" ", 1)[-1] in image_menu.FACE_COMMANDS:
                continue  # the drawn test picture has no face; test_image_faces covers the frame handling
            harness.sent.clear(), harness.errors.clear()
            options = {}
            parameters = {parameter.name: parameter for parameter in command.parameters}
            for field, raw in ANSWERS.get(name, {}).items():
                value = image_menu.parse_field(parameters[field], raw)
                if command._params[field].is_choice_annotation():
                    value = next(choice for choice in parameters[field].choices if choice.value == value)
                options[field] = value
            if name == "image effects convert":
                options["format"] = "gif"
            await command.callback(command.binding, menu.FakeInteraction(), attachment=menu.FakeAttachment(moving(), "clip.gif"), **options)
            results[name] = (list(harness.sent), list(harness.errors))
        return results

    for name, (sent, errors) in menu.run(storage, body).items():
        assert errors == [] and len(sent) == 1, (name, errors)
        times = timing(sent[0]["data"])
        if name in STILL_ON_PURPOSE:
            assert len(times) == 1, name
            continue
        assert len(times) > 1 and sent[0]["filename"].endswith(".gif"), name
        if name not in OWN_PACE:
            assert sum(times) == sum(UNEVEN), (name, times)  # not sped up, not slowed down
        if name.startswith(("image transform", "image template", "image text", "filter")) and name != "image transform shake":
            assert times == UNEVEN, (name, times)  # and the frame that lingers still does


def test_formats_made_from_several_pictures_move_too(storage):
    async def body(harness):
        commands = {command.qualified_name: command for command in harness.bot.tree.walk_commands()}
        still = menu.FakeAttachment(menu.png_bytes(), "still.png")
        clip = menu.FakeAttachment(moving(), "clip.gif")
        short = menu.FakeAttachment(moving([100, 100]), "short.gif")
        results = {}
        for name, options in (
            ("edit collage", {"image1": still, "image2": clip}),
            ("memegen drake", {"reject_image": clip, "approve": "this"}),
            ("memegen rtx", {"off_image": still, "on_image": clip}),
            ("animate togif", {"image1": still, "image2": clip, "image3": short, "delay": 300}),
            ("image effects overlay", {"attachment": clip, "overlay_attachment": still}),
            ("image effects convert", {"attachment": clip, "format": "webp"}),
            ("image effects convert", {"attachment": clip, "format": "png"}),
        ):
            harness.sent.clear(), harness.errors.clear()
            await commands[name].callback(commands[name].binding, menu.FakeInteraction(), **options)
            assert harness.errors == [], (name, harness.errors)
            results[name, options.get("format")] = (harness.sent[0]["filename"], timing(harness.sent[0]["data"]))
        return results

    results = menu.run(storage, body)
    for name in ("edit collage", "memegen drake", "memegen rtx", "image effects overlay"):
        filename, times = results[name, None]
        assert filename.endswith(".gif") and sum(times) == sum(UNEVEN), name
    # a still for its delay, then each clip at its own pace
    assert results["animate togif", None][1] == [300] + UNEVEN + [100, 100]
    assert results["image effects convert", "webp"] == ("converted.webp", UNEVEN)  # WebP can move, so it does
    assert results["image effects convert", "png"][1] == [0]  # PNG cannot: the first frame


def test_pats_and_bonks_play_over_a_moving_picture():
    from utils import image_animate as an

    clip = Frames([Image.new("RGBA", (64, 64), (index * 20, 0, 0, 255)) for index in range(12)], [50] * 12, 0, True)
    still = Frames(clip.frames[:1], [100])
    for build, loop in ((an.pet, an.PET_FRAMES), (an.triggered, len(an.TRIGGERED_SHAKES)), (an.bonk, len(an.BONK_LIFTS))):
        once, _ = build(still, 50)
        assert len(once) == loop  # a still: one pass of the effect
        frames, times = build(clip, 50)
        assert len(frames) % loop == 0 and len(frames) >= loop and sum(times) >= 600  # the clip plays through, the effect ends on its last step
        assert frames[0].tobytes() == once[0].tobytes() and frames[loop - 1].tobytes() != once[loop - 1].tobytes()  # and the picture under it changes
    with pytest.raises(ValueError):
        an.reverse(still)
