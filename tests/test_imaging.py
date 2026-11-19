import asyncio
import io

import pytest
from PIL import Image

from utils.imaging import Frames, ImageToolkit, apply_to_frames, encode_frames, fit_within, load_frames


def png(size=(40, 30), color=(200, 40, 40, 255)) -> bytes:
    out = io.BytesIO()
    Image.new("RGBA", size, color).save(out, format="PNG")
    return out.getvalue()


def gif(count=3, size=(40, 30), durations=(50, 80, 120)) -> bytes:
    frames = [Image.new("RGBA", size, (i * 60, 100, 200, 255)) for i in range(count)]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=list(durations)[:count], loop=0)
    return out.getvalue()


def test_load_frames_reads_stills_and_gifs():
    still = load_frames(png())
    assert isinstance(still, Frames) and len(still.frames) == 1 and not still.animated and still.size == (40, 30)
    moving = load_frames(gif(), "a.gif")
    assert moving.animated and len(moving.frames) == 3 and moving.durations == [50, 80, 120]
    assert all(frame.mode == "RGBA" for frame in moving.frames)
    assert len(load_frames(gif(), first_only=True).frames) == 1
    assert load_frames(png((400, 100)), max_edge=100).size == (100, 25)
    assert fit_within(Image.new("RGBA", (10, 10)), 100).size == (10, 10)  # never enlarged


def test_encode_frames_picks_the_format_and_keeps_timing():
    data, name = encode_frames([Image.new("RGBA", (8, 8))], [100], "one")
    assert name == "one.png" and Image.open(io.BytesIO(data)).format == "PNG"
    frames = [Image.new("RGBA", (8, 8), (i * 80, 0, 0, 255)) for i in range(3)]
    data, name = encode_frames(frames, [5, 60, 90], "many")
    result = Image.open(io.BytesIO(data))
    assert name == "many.gif" and result.n_frames == 3
    durations = []
    for index in range(3):
        result.seek(index)
        durations.append(result.info["duration"])
    assert durations == [20, 60, 90]  # too-short frames are slowed to what viewers can show


def test_apply_to_frames_runs_on_every_frame():
    seen = []
    data, name = apply_to_frames(gif(), "a.gif", lambda frame: seen.append(frame.size) or frame, "out")
    assert name == "out.gif" and len(seen) == 3
    assert apply_to_frames(png(), "a.png", lambda frame: frame, "out")[1] == "out.png"


class _Guild:
    filesize_limit = 10 * 1024 * 1024


class _Interaction:
    guild = _Guild()


def test_results_too_big_to_upload_are_refused_with_a_reason():
    tools = ImageToolkit(bot=None)
    tools.check_upload_size(_Interaction(), b"x" * 100)
    with pytest.raises(ValueError, match="more than the 10 MB"):
        tools.check_upload_size(_Interaction(), b"x" * (10 * 1024 * 1024 + 1))


def test_cpu_bound_work_runs_off_the_loop_and_times_out():
    tools = ImageToolkit(bot=None)

    async def scenario():
        assert await tools.run_cpu_bound(lambda: 7) == 7
        import time
        with pytest.raises(RuntimeError, match="took too long"):
            await tools.run_cpu_bound(lambda: time.sleep(0.3), timeout=0.05)
        await asyncio.sleep(0.4)  # the abandoned worker finishes and frees its slot
        assert tools._cpu_semaphore._value == 2

    asyncio.run(scenario())


def _embed_message(image=None, thumbnail=None, attachments=(), video=None):
    import discord
    parts = {"image": image, "thumbnail": thumbnail, "video": video}
    embed = discord.Embed.from_dict({"type": "rich", **{name: part for name, part in parts.items() if part}})
    return type("Message", (), {"attachments": list(attachments), "embeds": [embed]})()


CDN = "https://cdn.discordapp.com/attachments/1/2/bonk.gif?ex=1"
ELSEWHERE = {"url": "https://i.ytimg.com/vi/x/hq.jpg", "proxy_url": "https://images-ext-1.discordapp.net/external/abc/https/i.ytimg.com/vi/x/hq.jpg"}


def test_an_embedded_picture_is_only_taken_from_discords_own_hosts():
    from utils.imaging import embed_image_url, is_discord_image_host
    assert embed_image_url(_embed_message(image={"url": CDN})) == CDN
    assert embed_image_url(_embed_message(thumbnail=ELSEWHERE)) == ELSEWHERE["proxy_url"]  # Discord's copy, not the site
    assert embed_image_url(_embed_message(thumbnail={"url": "https://i.ytimg.com/vi/x/hq.jpg"})) is None
    assert not is_discord_image_host("https://images-ext-1.discordapp.net.evil.example/x.png")
    assert not ImageToolkit(bot=None).message_has_image(_embed_message(thumbnail={"url": "https://evil.example/x.png"}))


def test_the_channel_search_skips_pictures_it_cannot_fetch_and_a_bad_selection_is_dropped():
    tools = ImageToolkit(bot=None)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        return b"data"

    tools.fetch_bytes_from_url = fetch
    unusable = _embed_message(thumbnail={"url": "https://evil.example/x.png"})
    usable = _embed_message(image={"url": CDN})

    class Channel:
        id = 5

        def history(self, limit):
            async def walk():
                for message in (unusable, usable):
                    yield message
            return walk()

        async def fetch_message(self, message_id):
            return unusable

    channel = Channel()
    guild = type("Guild", (), {"get_channel": lambda self, _id: channel, "get_thread": lambda self, _id: None})()
    interaction = type("Interaction", (), {"channel": channel, "guild": guild, "user": type("User", (), {"id": 9})()})()
    tools.sources.remember(9, 5, 77)

    assert asyncio.run(tools.get_source_image(interaction)) == (b"data", "embedded_image.png")
    assert fetched == [CDN] and tools.sources.get(9) is None


GIF_PICKER = {
    "url": "https://media.tenor.com/abc/cat.mp4",
    "proxy_url": "https://images-ext-1.discordapp.net/external/abc/https/media.tenor.com/abc/cat.mp4",
}


def test_an_embedded_video_file_is_taken_before_its_still():
    from utils.imaging import embed_image_url
    assert embed_image_url(_embed_message(video=GIF_PICKER, thumbnail=ELSEWHERE)) == GIF_PICKER["proxy_url"]
    player = {"url": "https://www.youtube.com/embed/x", "proxy_url": "https://images-ext-1.discordapp.net/external/abc/https/www.youtube.com/embed/x"}
    assert embed_image_url(_embed_message(video=player, thumbnail=ELSEWHERE)) == ELSEWHERE["proxy_url"]  # a player page is not a file
    tools = ImageToolkit(bot=None)
    clip = type("Attachment", (), {"content_type": "video/mp4", "filename": "clip"})()
    named = type("Attachment", (), {"content_type": None, "filename": "CLIP.WEBM"})()
    text = type("Attachment", (), {"content_type": "text/plain", "filename": "notes.txt"})()
    assert tools.supported_attachment(clip) and tools.supported_attachment(named) and not tools.supported_attachment(text)


def _clip(tmp_path, name, *more) -> bytes:
    import subprocess
    path = tmp_path / name
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=640x360:rate=50:duration=1", *more, str(path)], check=True)
    return path.read_bytes()


def test_a_video_becomes_a_gif_on_the_way_in(tmp_path):
    import shutil
    from utils.imaging import VIDEO_MAX_EDGE, video_container, video_to_gif
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg is not installed")
    png = io.BytesIO()
    Image.new("RGB", (4, 4)).save(png, format="PNG")
    assert video_container(png.getvalue()) is None
    for name, more, kind in (("a.mp4", ("-pix_fmt", "yuv420p"), "mov"), ("a.webm", ("-c:v", "libvpx", "-b:v", "500k"), "matroska")):
        clip = _clip(tmp_path, name, *more)
        assert video_container(clip) == kind
        loaded = load_frames(video_to_gif(clip), "a.gif")
        assert loaded.animated and loaded.size == (VIDEO_MAX_EDGE, 270)  # shrunk, shape kept
        assert 20 <= len(loaded.frames) <= 27 and set(loaded.durations[:-1]) == {40}  # 50 frames a second slowed to 25
    with pytest.raises(ValueError, match="can't read that video"):
        video_to_gif(clip[:8] + b"junk" * 50)

    tools = ImageToolkit(bot=None)

    async def find(*_):
        return clip, "funny.webm"

    tools._find_source_image = find
    data, name = asyncio.run(tools.get_source_image(None))
    assert name == "funny.gif" and data[:3] == b"GIF"
