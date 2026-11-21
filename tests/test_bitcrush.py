"""Bit crushing: /animate bitcrush on videos and sound files, and as an option of /animate datamosh."""

import shutil
import subprocess
import types

import pytest

import test_datamosh as moshing
import test_image_menu as menu
import test_video_export as video
from utils import audio_fx
from utils.audio_fx import apply_audio_filter, audio_format, crush_filter

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None, reason="needs ffmpeg")
clips = video.clips


@pytest.fixture(scope="module")
def sounds(tmp_path_factory) -> dict[str, bytes]:
    """A second of tone in the kinds of sound file people post."""
    folder = tmp_path_factory.mktemp("sounds")
    made = {}
    for extension, encoder in (("wav", ["-c:a", "pcm_s16le"]), ("mp3", ["-c:a", "libmp3lame"]), ("ogg", ["-c:a", "libvorbis"]),
                               ("flac", ["-c:a", "flac"]), ("m4a", ["-c:a", "aac"])):
        path = folder / f"tone.{extension}"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "sine=frequency=440:duration=1", *encoder, str(path)], check=True)
        made[extension] = path.read_bytes()
    return made


def test_the_filter_says_what_was_asked_for():
    crush = crush_filter(4, 16, "log", mix=75, drive=20)
    assert "bits=4" in crush and "samples=16" in crush and "mode=log" in crush
    assert "weights='0.25 0.75'" in crush and "amix" not in crush_filter()  # a quarter of the original kept; by default none
    assert "level_in=10.0000" in crush and "level_out=0.1000" in crush  # 20 dB in, taken back out
    assert "alimiter" in crush and "level_in=1.0000" in crush_filter()
    assert set(audio_fx.CRUSH_PRESETS) == {"light", "heavy", "destroyed"}


def crushed_tone(audio_filter: str | None) -> list[int]:
    """Half a second of a loud tone through a filter, as plain sample values."""
    import array

    command = ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=0.5", "-af", "volume=6" + (f",{audio_filter}" if audio_filter else ""),
               "-ac", "1", "-f", "s16le", "-"]
    return list(array.array("h", subprocess.run(command, capture_output=True, check=True).stdout))[2000:18000]


def test_the_sound_really_is_crushed():
    def changes(samples):
        return sum(1 for before, after in zip(samples, samples[1:]) if before != after) / len(samples)

    clean = crushed_tone(None)
    assert len(set(clean)) > 1000 and changes(clean) > 0.9
    fewer_bits = crushed_tone(crush_filter(3, 1))
    assert len(set(fewer_bits)) <= 2 ** 4 + 1  # three bits each side of zero: a handful of levels, where there were thousands
    held = crushed_tone(crush_filter(16, 10))
    assert changes(held) == pytest.approx(0.1, abs=0.02)  # ten samples become one
    both = crushed_tone(crush_filter(*audio_fx.CRUSH_PRESETS["heavy"]))
    assert len(set(both)) <= 2 ** 6 + 1 and changes(both) < 0.1
    half = crushed_tone(crush_filter(3, 1, mix=50))
    assert len(set(half)) > 100  # half the original is still in there
    soft = crushed_tone("volume=0.05," + crush_filter(*audio_fx.CRUSH_PRESETS["destroyed"]))
    assert max(soft) > 0  # a quiet sound survives the harshest preset
    assert max(crushed_tone(crush_filter(6, 8, drive=24))) <= max(clean) * 1.1  # driven hard, it comes out no louder


def test_sound_files_are_told_apart_by_how_they_start(sounds, clips):
    assert {name: audio_format(data) for name, data in sounds.items() if name != "m4a"} == {"wav": "wav", "mp3": "mp3", "ogg": "ogg", "flac": "flac"}
    assert audio_format(clips["sound"]) is None and audio_format(b"#EXTM3U\nfile:///etc/passwd") is None and audio_format(b"") is None


def test_a_video_keeps_its_picture_and_gets_a_new_sound(clips, tmp_path):
    made, extension = apply_audio_filter(clips["sound"], crush_filter(3, 30))
    before, after = video.streams(clips["sound"], tmp_path), video.streams(made, tmp_path)
    assert extension == "mp4" and set(after) == {"video:h264", "audio:aac"}
    assert after["video:h264"] == before["video:h264"]  # copied, not made again: every frame, at full size
    assert made != clips["sound"]
    with pytest.raises(ValueError, match="any sound"):
        apply_audio_filter(clips["silent"], crush_filter())
    for junk in (b"not media at all", b"#EXTM3U\nfile:///etc/passwd\n", menu.png_bytes()):
        with pytest.raises(ValueError, match="not a video or an audio file"):
            apply_audio_filter(junk, crush_filter())


def test_sound_files_come_back_as_sound_files(sounds, tmp_path):
    for name, data in sounds.items():
        made, extension = apply_audio_filter(data, crush_filter())
        assert extension == ("m4a" if name == "m4a" else "mp3"), name
        found = video.streams(made, tmp_path)
        assert len(found) == 1 and next(iter(found)).startswith("audio:") and next(iter(found.values())) == pytest.approx(1.0, abs=0.15), name


def test_the_command_on_an_attachment_and_on_a_clicked_message(storage, clips, sounds, tmp_path):
    data, filename = video.run_command(storage, clips["sound"], "animate bitcrush", bits=4)
    assert filename == "bitcrushed.mp4" and set(video.streams(data, tmp_path)) == {"video:h264", "audio:aac"}
    data, filename = video.run_command(storage, sounds["wav"], "animate bitcrush", filename="tone.wav")
    assert filename == "bitcrushed.mp3"

    async def clicked(harness):
        tools = harness.bot.image_tools
        attachment = menu.FakeAttachment(clips["sound"], "clip.mp4")
        attachment.content_type = "video/mp4"
        message = types.SimpleNamespace(attachments=[menu.FakeAttachment(menu.png_bytes(), "first.png"), attachment], embeds=[])
        with tools.pinned_source(message):  # as the Edit Image menu does
            assert (await tools.get_media_source(menu.FakeInteraction()))[1] == "clip.mp4"  # the picture beside it is passed over
        with pytest.raises(ValueError, match="Attach a video"):
            await tools.get_media_source(menu.FakeInteraction())
        with tools.pinned_source(types.SimpleNamespace(attachments=[menu.FakeAttachment(menu.png_bytes(), "only.png")], embeds=[])):
            with pytest.raises(ValueError, match="no video or audio"):
                await tools.get_media_source(menu.FakeInteraction())
        assert "animate bitcrush" in harness.offered()  # and Edit Image lists it

    menu.run(storage, clicked)


def test_datamosh_can_crush_the_sound_of_its_video(storage, clips, tmp_path):
    from cogs import image_animate

    heavy = next(choice for choice in image_animate.MOSH_SOUND if choice.value == "heavy")
    seen = []
    real = image_animate.set_video_sound_filter

    def watch(audio_filter):
        seen.append((audio_filter, real(audio_filter)))
        return seen[-1][1]

    image_animate.set_video_sound_filter = watch
    try:
        data, filename = video.run_command(storage, clips["sound"], "animate datamosh", bitcrush=heavy)
        assert filename == "datamosh.mp4" and set(video.streams(data, tmp_path)) == {"video:h264", "audio:aac"}
        assert seen == [(crush_filter(*audio_fx.CRUSH_PRESETS["heavy"]), True)]
        plain, _ = video.run_command(storage, clips["sound"], "animate datamosh", seed=1)
        assert len(seen) == 1  # left alone unless asked
        # a GIF has no sound: the option does nothing, and the reply does not claim otherwise
        seen.clear()
        data, filename = video.run_command(storage, moshing.gif_of(moshing.clip()), "animate datamosh", filename="clip.gif", bitcrush=heavy)
        assert filename == "datamosh.gif" and seen[0][1] is False
    finally:
        image_animate.set_video_sound_filter = real
