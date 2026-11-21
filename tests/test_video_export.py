"""A video going into an image command comes back as a video, with its sound."""

import asyncio
import io
import json
import shutil
import subprocess
import types

import pytest
from PIL import Image

import test_image_menu as menu
from utils import imaging
from utils.imaging import VIDEO_PLACEHOLDER, ImageToolkit, VideoJob, frames_to_video, write_animation

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")


@pytest.fixture(scope="module")
def clips(tmp_path_factory) -> dict[str, bytes]:
    """Two seconds of moving picture: once with a tone under it, once silent."""
    folder = tmp_path_factory.mktemp("clips")
    picture = ["-f", "lavfi", "-i", "testsrc2=size=160x120:rate=20:duration=2"]
    tone = ["-f", "lavfi", "-i", "sine=frequency=440:duration=2"]
    encode = ["-c:v", "libx264", "-pix_fmt", "yuv420p"]
    subprocess.run(["ffmpeg", "-v", "error", "-y", *picture, *tone, *encode, "-c:a", "aac", "-shortest", str(folder / "sound.mp4")], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", *picture, *encode, str(folder / "silent.mp4")], check=True)
    return {name: (folder / f"{name}.mp4").read_bytes() for name in ("sound", "silent")}


def streams(video: bytes, tmp_path) -> dict[str, float]:
    """What is in a video file: each kind of stream and how long it runs."""
    path = tmp_path / "result.mp4"
    path.write_bytes(video)
    found = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type,codec_name,duration", "-of", "json", str(path)],
        capture_output=True, text=True, check=True,
    ).stdout)["streams"]
    return {f"{stream['codec_type']}:{stream['codec_name']}": float(stream["duration"]) for stream in found}


def frames(count=10, size=(64, 48)) -> list[Image.Image]:
    return [Image.new("RGBA", size, (index * 20, 100, 200, 255)) for index in range(count)]


def test_frames_become_a_video_as_long_as_they_were(clips, tmp_path):
    even = streams(frames_to_video(frames(), [100] * 10), tmp_path)
    assert list(even) == ["video:h264"] and even["video:h264"] == pytest.approx(1.0, abs=0.06)
    uneven = streams(frames_to_video(frames(), [40] * 9 + [640]), tmp_path)  # one frame that lingers
    assert uneven["video:h264"] == pytest.approx(1.0, abs=0.06)
    odd = frames_to_video(frames(4, (63, 47)), [100] * 4)  # H.264 wants even sides: no error
    assert odd[4:8] == b"ftyp"

    with_sound = streams(frames_to_video(frames(), [100] * 10, sound_from=clips["sound"]), tmp_path)
    assert set(with_sound) == {"video:h264", "audio:aac"} and with_sound["audio:aac"] == pytest.approx(1.0, abs=0.1)  # cut to the picture
    assert list(streams(frames_to_video(frames(), [100] * 10, sound_from=clips["silent"]), tmp_path)) == ["video:h264"]


def test_a_video_job_takes_the_frames_instead_of_a_gif():
    token = imaging._video_job.set(None)
    try:
        assert Image.open(io.BytesIO(VIDEO_PLACEHOLDER)).format == "GIF"  # still a picture, for anything that looks at it
        assert write_animation(frames(3), [50] * 3)[:6] == b"GIF89a" and write_animation(frames(3), [50] * 3) != VIDEO_PLACEHOLDER
        job = VideoJob(source=b"", source_ms=150)
        imaging._video_job.set(job)
        assert write_animation(frames(3), [50] * 3) == VIDEO_PLACEHOLDER and len(job.frames) == 3 and job.durations == [50] * 3
        job.frames = None
        assert write_animation(frames(3), [50] * 3, keep_gif=True) != VIDEO_PLACEHOLDER and job.frames is None  # a command that promises a GIF
    finally:
        imaging._video_job.reset(token)


def run_command(storage, clip: bytes, name: str, filename="clip.mp4", **options):
    """One command through its real handler and the real send, on an attached file."""
    async def body(harness):
        tools = harness.bot.image_tools
        sent = []

        async def send(interaction, *, data, filename, title, description=None):
            sent.append(await ImageToolkit.finish_video(tools, interaction, data, filename))

        tools.send_processed_file = send
        tools.defer = types.MethodType(ImageToolkit.defer, tools)
        command = {c.qualified_name: c for c in harness.bot.tree.walk_commands()}[name]
        attachment = menu.FakeAttachment(clip, filename)
        attachment.content_type = "video/mp4" if filename.endswith(".mp4") else "image/gif"
        interaction = menu.FakeInteraction()
        interaction.response.done = True  # already acknowledged: defer has nothing to send
        await command.callback(command.binding, interaction, attachment=attachment, **options)
        assert harness.errors == [], harness.errors
        return sent[0]

    return menu.run(storage, body)


@pytest.mark.parametrize("name", ["filter invert", "image transform flip", "image text toptext", "memegen jail", "memegen respects", "animate pet"])
def test_a_video_comes_back_as_a_video_with_its_sound(storage, clips, tmp_path, name):
    options = {"text": "hello"} if name == "image text toptext" else {}
    data, filename = run_command(storage, clips["sound"], name, **options)
    found = streams(data, tmp_path)
    assert filename.endswith(".mp4") and set(found) == {"video:h264", "audio:aac"}
    assert found["video:h264"] == pytest.approx(2.0, abs=0.6 if name == "animate pet" else 0.15)  # a pat finishes its last stroke


def test_what_does_not_come_back_as_a_video_with_sound(storage, clips, tmp_path):
    # a GIF stays a GIF
    gif = io.BytesIO()
    frames()[0].save(gif, format="GIF", save_all=True, append_images=frames()[1:], duration=100, loop=0)
    data, filename = run_command(storage, gif.getvalue(), "filter invert", filename="clip.gif")
    assert filename.endswith(".gif") and Image.open(io.BytesIO(data)).n_frames == 10
    # a result that is a still stays a still
    data, filename = run_command(storage, clips["sound"], "animate frames")
    assert filename.endswith(".png")
    # played at another speed, the sound would no longer fit: a silent video
    data, filename = run_command(storage, clips["sound"], "animate speed", percent=200)
    assert filename.endswith(".mp4") and list(streams(data, tmp_path)) == ["video:h264"]
    # asking for a GIF by name gets one
    data, filename = run_command(storage, clips["sound"], "image effects convert", format="gif")
    assert filename == "converted.gif" and Image.open(io.BytesIO(data)).n_frames > 10
    # and convert makes a GIF into a video, or keeps a video's sound
    data, filename = run_command(storage, gif.getvalue(), "image effects convert", filename="clip.gif", format="mp4")
    assert filename == "converted.mp4" and list(streams(data, tmp_path)) == ["video:h264"]
    data, filename = run_command(storage, clips["sound"], "image effects convert", format="mp4")
    assert set(streams(data, tmp_path)) == {"video:h264", "audio:aac"}


def test_a_video_that_cannot_be_made_falls_back_to_the_gif(storage, clips, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("no encoder")

    monkeypatch.setattr(imaging, "frames_to_video", broken)
    data, filename = run_command(storage, clips["silent"], "filter invert")
    assert filename.endswith(".gif") and Image.open(io.BytesIO(data)).n_frames > 10
