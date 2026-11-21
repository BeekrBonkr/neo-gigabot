"""/animate datamosh: the plan for breaking a stream, and the command on real clips."""

import io
import shutil

import pytest
from PIL import Image

import test_image_menu as menu
import test_video_export as video
from utils import image_mosh as mosh
from utils.image_mosh import HOLD, Settings, plan
from utils.imaging import Frames

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="needs ffmpeg")
clips = video.clips  # the two-second test videos, with and without sound

DELTAS = [True] + [False] * 23  # a keyframe, then nothing but motion
QUIET = dict(melt=0, skip=0, shuffle=0)


def test_plan_leaves_an_untouched_stream_alone():
    assert plan(DELTAS, Settings(**QUIET)) == list(range(24))


def test_plan_melts_by_repeating_the_motion_at_each_point():
    kept = plan(DELTAS, Settings(melt=3, every=10, skip=0))
    # frame 1 is applied four times and stands in for 2-4; the same at 11 and 21 (cut short by the end)
    assert kept[:8] == [0, 1, 1, 1, 1, 5, 6, 7] and kept[10:15] == [10, 11, 11, 11, 11] and kept[-3:] == [21, 21, 21]
    assert len(kept) == 24  # the clip is as long as it was
    stretched = plan(DELTAS, Settings(melt=3, every=10, keep_length=False))
    assert stretched[:7] == [0, 1, 1, 1, 1, 2, 3] and len(stretched) == 24 + 3 * 3


def test_plan_skips_hold_the_picture_or_shorten_the_clip():
    kept = plan(DELTAS, Settings(melt=0, every=8, skip=2))
    assert kept[:6] == [0, 1, HOLD, HOLD, 4, 5] and len(kept) == 24
    cut = plan(DELTAS, Settings(melt=0, every=8, skip=2, keep_length=False))
    assert cut[:4] == [0, 1, 4, 5] and len(cut) == 24 - 3 * 2


def test_plan_only_moshes_between_start_and_end():
    kept = plan(DELTAS, Settings(melt=2, every=4, start=50, end=75))
    # mosh points at 12 and 16, the only ones from halfway to three quarters; the rest plays as it was
    assert kept == list(range(12)) + [12, 12, 12, 15, 16, 16, 16] + list(range(19, 24))


def test_plan_takes_keyframes_out_unless_cuts_are_kept_clean():
    cut_at_six = [True] + [False] * 5 + [True] + [False] * 5
    assert plan(cut_at_six, Settings(**QUIET)) == [0, 1, 2, 3, 4, 5, HOLD, 7, 8, 9, 10, 11]
    assert plan(cut_at_six, Settings(**QUIET, keep_length=False)) == [0, 1, 2, 3, 4, 5, 7, 8, 9, 10, 11]
    assert plan(cut_at_six, Settings(**QUIET, clean_cuts=True)) == list(range(12))
    assert 6 not in plan(cut_at_six, Settings(melt=4, every=1))  # a keyframe is never the thing repeated


def test_plan_shuffles_the_same_way_for_the_same_seed():
    one = plan(DELTAS, Settings(melt=0, shuffle=60, seed=7))
    assert one == plan(DELTAS, Settings(melt=0, shuffle=60, seed=7)) != plan(DELTAS, Settings(melt=0, shuffle=60, seed=8))
    assert sorted(one) == list(range(24)) and one[0] == 0 and one != list(range(24))
    assert len(plan(DELTAS, Settings(melt=40, every=1, keep_length=False), limit=50)) == 50


def clip(count=16, size=(96, 64), drift=4) -> Frames:
    frames = []
    for index in range(count):
        frame = Image.new("RGBA", size, (30, 90, 160, 255))
        frame.paste((240, 200, 40, 255), (drift * index, 16, drift * index + 28, 48))
        frames.append(frame)
    return Frames(frames, [50] * count, 0, True)


def still() -> Frames:
    return Frames([Image.new("RGBA", (96, 64), (200, 40, 40, 255))], [100])


def test_a_real_clip_is_broken_and_keeps_its_length():
    source = clip()
    untouched, times = mosh.datamosh(source, Settings(**QUIET))
    assert len(untouched) == 16 and times == [50] * 16 and untouched[0].size == (96, 64)
    melted, times = mosh.datamosh(source, Settings(melt=6, every=4))
    assert len(melted) == 16 and sum(times) == 800
    assert melted[8].tobytes() != untouched[8].tobytes()  # the motion went somewhere it was not meant to
    longer, times = mosh.datamosh(source, Settings(melt=6, every=4, keep_length=False))
    assert len(longer) > 16
    for options in (dict(block_size="chunky"), dict(motion="ghost"), dict(reverse=True), dict(blockiness=31), dict(skip=3, shuffle=50)):
        frames, _ = mosh.datamosh(source, Settings(**options))
        assert len(frames) == 16 and frames[0].size == (96, 64), options


def test_a_second_clip_drags_the_first_ones_picture_around():
    frames, times = mosh.datamosh(still(), Settings(**QUIET), into=clip())
    assert len(frames) == 16 and len(times) == 16  # the still, then the clip's motion without the picture that opened it
    red = [frame.getpixel((90, 60)) for frame in frames]
    assert red[0][0] > 150 and red[-1][0] > 150  # still the first picture's color where nothing moved, to the end
    assert frames[-1].tobytes() != frames[0].tobytes()

    with pytest.raises(ValueError, match="something that moves"):
        mosh.datamosh(still(), Settings())
    with pytest.raises(ValueError, match="`into` has to be"):
        mosh.datamosh(clip(), Settings(), into=still())
    slideshow = Frames([Image.new("RGBA", (96, 64), color) for color in ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255))], [100] * 3, 0, True)
    with pytest.raises(ValueError, match="no motion"):
        mosh.datamosh(slideshow, Settings())


def gif_of(frames: Frames) -> bytes:
    out = io.BytesIO()
    frames.frames[0].save(out, format="GIF", save_all=True, append_images=frames.frames[1:], duration=frames.durations, loop=0)
    return out.getvalue()


def test_the_command_gives_a_gif_for_a_gif_and_a_video_with_sound_for_a_video(storage, clips, tmp_path):
    data, filename = video.run_command(storage, gif_of(clip()), "animate datamosh", filename="clip.gif", melt=8, every=5)
    result = Image.open(io.BytesIO(data))
    assert filename == "datamosh.gif" and result.n_frames > 4

    data, filename = video.run_command(storage, clips["sound"], "animate datamosh")
    found = video.streams(data, tmp_path)
    assert filename == "datamosh.mp4" and set(found) == {"video:h264", "audio:aac"} and found["video:h264"] == pytest.approx(2.0, abs=0.15)

    stretch = next(choice for choice in __import__("cogs.image_animate", fromlist=["x"]).MOSH_LENGTH if choice.value == "stretch")
    data, filename = video.run_command(storage, clips["sound"], "animate datamosh", melt=20, every=6, length=stretch)
    found = video.streams(data, tmp_path)
    assert list(found) == ["video:h264"] and found["video:h264"] > 3.0  # stretched past its sound, so it goes without


def test_the_right_click_panel_sets_things_up_and_runs_the_command(storage):
    MoshForm, MoshPanel = menu.image_menu.MoshForm, menu.image_menu.MoshPanel  # the module the harness set up

    async def body(harness):
        cog = harness.cog
        assert harness.bot.tree.get_command("Datamosh", type=__import__("discord").AppCommandType.message) is not None
        empty = menu.FakeInteraction()
        await cog.datamosh(empty, __import__("types").SimpleNamespace(attachments=[], embeds=[]))
        assert "Nothing To Mosh" in empty.response.sent[0]["embed"].title

        target = __import__("types").SimpleNamespace(attachments=[menu.FakeAttachment(gif_of(clip()), "clip.gif")], embeds=[])
        opened = menu.FakeInteraction()
        await cog.datamosh(opened, target)
        panel = opened.response.sent[0]["view"]
        assert isinstance(panel, MoshPanel) and opened.response.sent[0]["ephemeral"] is True
        # four dropdowns, one for each pick-one setting, and a row of buttons
        assert [type(item).__name__ for item in panel.children].count("MoshChoice") == 4
        assert [item.label for item in panel.children if hasattr(item, "label")] == ["Mosh", "Amounts", "Range and seed", "Reverse: off"]
        assert "melt **6**" in panel.embed().description and "seed **random**" in panel.embed().description

        edits = []

        class Editing(menu.Response):
            async def edit_message(self, **kwargs):
                edits.append(kwargs)
                self.done = True

        def press():
            interaction = menu.FakeInteraction()
            interaction.response = Editing()
            return interaction

        # the two forms hold every number the command takes, filled in with where things stand
        forms = {}
        for label in ("Amounts", "Range and seed"):
            pressed = press()
            await next(item for item in panel.children if getattr(item, "label", "") == label).callback(pressed)
            forms[label] = pressed.response.modal
            assert isinstance(forms[label], MoshForm)
        amounts = {parameter.name: component for parameter, component in forms["Amounts"].fields}
        assert list(amounts) == ["melt", "every", "skip", "shuffle", "blockiness"] and amounts["melt"].default == "6"
        amounts["melt"]._value, amounts["every"]._value, amounts["skip"]._value = "9", "5", ""
        amounts["shuffle"]._value, amounts["blockiness"]._value = "20", "99"
        refused = press()
        await forms["Amounts"].on_submit(refused)
        assert "Check Those Values" in refused.response.sent[0]["embed"].title and panel.values == {}  # nothing half-applied
        amounts["blockiness"]._value = "12"
        await forms["Amounts"].on_submit(press())
        assert panel.values == {"melt": 9, "every": 5, "shuffle": 20, "blockiness": 12} and "melt **9**" in edits[-1]["embed"].description

        chunky = next(item for item in panel.children if getattr(item, "parameter", None) is not None and item.parameter.name == "block_size")
        chunky._values = ["chunky"]
        await chunky.callback(press())
        await panel.toggle_reverse(press())
        assert panel.values["block_size"] == "chunky" and panel.values["reverse"] is True and panel.reverse_button.label == "Reverse: on"
        assert [option.default for option in chunky.options] == [False, False, True]

        seen = {}
        real = mosh.datamosh

        def watch(clip_frames, settings, into=None):
            seen["settings"] = settings
            return real(clip_frames, settings, into)

        mosh.datamosh = watch
        try:
            await panel.run(menu.FakeInteraction())
        finally:
            mosh.datamosh = real
        assert harness.errors == [] and harness.sent[0]["filename"] == "datamosh.gif"
        used = seen["settings"]
        assert (used.melt, used.every, used.shuffle, used.blockiness, used.block_size, used.reverse) == (9, 5, 20, 12, "chunky", True)
        assert used.skip == 0 and used.motion == "smear"  # what was left alone is the command's own default

    menu.run(storage, body)
