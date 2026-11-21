"""Long GIFs and videos come back whole: thinned or slowed where they must be, never cut short without saying so."""

import io
import shutil
import subprocess
import types

import pytest
from PIL import Image

import test_animated_inputs as animated
import test_image_menu as menu
import test_video_export as video
from utils import imaging
from utils.imaging import (
    MAX_GIF_FRAMES, VIDEO_MAX_FRAMES, Frames, ImageToolkit, VideoJob, frame_budget, load_frames, video_frame_rate, write_animation,
)

LONG = [30] * 299 + [530]  # 300 frames, 9.5 seconds, the last one lingering


def long_gif() -> bytes:
    frames = []
    for index in range(len(LONG)):  # every frame different from the one before, or the GIF writer would merge them
        frame = Image.new("RGBA", (48, 32), (30, 90, 160, 255))
        frame.paste((250, 200, 40, 255), (index % 40, index % 20, index % 40 + 8, index % 20 + 8))
        frames.append(frame)
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=LONG, loop=0)
    return out.getvalue()


def test_a_long_gif_is_thinned_along_its_whole_length():
    loaded = load_frames(long_gif())
    assert len(loaded.frames) == MAX_GIF_FRAMES and sum(loaded.durations) == sum(LONG)
    # each kept frame takes on the time of the one or two left out after it
    assert set(loaded.durations[:-1]) == {60, 90} and loaded.durations[-1] > 500
    whole = Image.open(io.BytesIO(long_gif()))
    whole.seek(round(119 * 300 / 120))
    assert loaded.frames[-1].tobytes() == whole.convert("RGBA").tobytes()  # it reaches the end, not just frame 120
    short = load_frames(animated.moving())
    assert len(short.frames) == 10 and short.durations == animated.UNEVEN  # one that fits is left exactly as it was


def test_a_command_working_on_a_video_may_use_more_frames():
    token = imaging._video_job.set(None)
    try:
        assert frame_budget() == MAX_GIF_FRAMES and frame_budget(60, 150) == 60
        imaging._video_job.set(VideoJob(source=b"", source_ms=0))
        assert frame_budget() == VIDEO_MAX_FRAMES and frame_budget(60, 150) == 150
        assert len(load_frames(long_gif()).frames) == 300  # all of them now
        # but a GIF written from them is GIF-sized, and as long
        frames = [Image.new("RGBA", (8, 8), (index % 255, 0, 0, 255)) for index in range(300)]
        written = Image.open(io.BytesIO(write_animation(frames, [40] * 300, keep_gif=True)))
        assert written.n_frames == MAX_GIF_FRAMES and sum(animated.timing(write_animation(frames, [40] * 300, keep_gif=True))) == 12000
    finally:
        imaging._video_job.reset(token)


def test_frame_rate_follows_the_length_of_the_video():
    assert video_frame_rate(None) == 30 and video_frame_rate(4) == 30  # short: as smooth as it can be
    assert video_frame_rate(15) == 20 and video_frame_rate(30) == 10  # longer: fewer frames a second, all of it covered
    assert video_frame_rate(300) == 10  # past thirty seconds there is a floor, and the end is left off


@pytest.mark.parametrize("name, exact", [("image transform flip", True), ("filter invert", True), ("memegen jail", True), ("animate pet", False)])
def test_commands_keep_a_long_gifs_length(storage, name, exact):
    data, filename = video.run_command(storage, long_gif(), name, filename="long.gif")
    times = animated.timing(data)
    assert filename.endswith(".gif") and len(times) <= MAX_GIF_FRAMES
    if exact:
        assert sum(times) == sum(LONG)
    else:
        assert sum(LONG) <= sum(times) <= sum(LONG) + 1000  # a pat finishes its stroke


needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")


@needs_ffmpeg
def test_a_whole_video_comes_back_and_an_overlong_one_says_so(storage, tmp_path):
    def clip(seconds: int, rate: int) -> bytes:
        path = tmp_path / f"{seconds}.mp4"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", f"testsrc2=size=96x64:rate={rate}:duration={seconds}",
                        "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-shortest", str(path)], check=True)
        return path.read_bytes()

    twelve = clip(12, 20)
    data, filename = video.run_command(storage, twelve, "filter invert")
    found = video.streams(data, tmp_path)
    assert found["video:h264"] == pytest.approx(12.0, abs=0.2) and found["audio:aac"] == pytest.approx(12.0, abs=0.2)  # not the first five seconds

    async def body(harness):
        tools = harness.bot.image_tools
        said = []

        class Followup:
            async def send(self, *, embed, file):
                said.append((embed.description, file.filename))

        interaction = menu.FakeInteraction()
        interaction.followup = Followup()
        tools.send_processed_file = types.MethodType(ImageToolkit.send_processed_file, tools)
        command = {c.qualified_name: c for c in harness.bot.tree.walk_commands()}["filter invert"]
        for media in (clip(40, 10), twelve):
            attachment = menu.FakeAttachment(media, "clip.mp4")
            attachment.content_type = "video/mp4"
            await command.callback(command.binding, interaction, attachment=attachment)
        assert harness.errors == [], harness.errors
        return said

    (long_note, long_name), (short_note, short_name) = menu.run(storage, body)
    assert long_name == short_name == "invert.mp4"
    assert "its first 30 seconds" in long_note and "first" not in short_note
