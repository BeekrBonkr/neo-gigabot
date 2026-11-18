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
