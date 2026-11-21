from __future__ import annotations

import asyncio
import contextlib
import contextvars
import functools
import io
import itertools
import logging
import warnings
import random
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable, Iterator
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageEnhance, ImageFont, ImageSequence

from utils.discord_links import fetch_message_from_link
from utils.richtext import PLACEHOLDER_BASE, RichText
from utils.user_app import home_guild

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
# A lot of what Discord shows as a GIF is a short silent video: everything from
# its GIF picker, and many uploads. These are turned into animated images on the way in.
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")
MEDIA_AUDIO_EXTENSIONS = (".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".m4a", ".aac")
SOURCE_TTL_SECONDS = 60 * 15
NO_HISTORY_MESSAGE = (
    "I can't look back through this channel for an image. Attach one to the command, "
    "or right-click a message and choose Apps → Edit Image."
)

# Embed-image URLs are attacker-controlled (any user can post a link that
# unfurls to an arbitrary URL), so fetches are restricted to Discord's own hosts
# - same reasoning as the music cog's ALLOWED_URL_HOSTS. An embedded picture
# from another site is fetched through the copy Discord serves of it (the
# embed's proxy_url), never from the site itself.
ALLOWED_IMAGE_HOSTS: tuple[str, ...] = (
    "cdn.discordapp.com",
    "media.discordapp.net",
)
DISCORD_IMAGE_PROXY_HOST = re.compile(r"images-ext-\d+\.discordapp\.net")


def is_discord_image_host(url: str | None) -> bool:
    host = (urlparse(url or "").hostname or "").lower()
    return host in ALLOWED_IMAGE_HOSTS or DISCORD_IMAGE_PROXY_HOST.fullmatch(host) is not None


def embed_image_url(message: discord.Message) -> str | None:
    """The first embedded picture in the message that Discord itself serves.
    An embedded video file (a GIF from Discord's picker is one) comes before
    the still picture that stands in for it."""
    for embed in message.embeds:
        for media, is_video in ((embed.video, True), (embed.image, False), (embed.thumbnail, False)):
            if not media:
                continue
            for url in (media.url, media.proxy_url):
                if not url or not is_discord_image_host(url):
                    continue
                if is_video and not urlparse(url).path.lower().endswith(VIDEO_EXTENSIONS):
                    continue  # a player page, not a file
                return url
    return None


# How much of a GIF or a video is worked on: its first MAX_CLIP_SECONDS. A
# short video is taken at its own pace, a longer one with fewer frames a
# second, down to VIDEO_MIN_FPS at the full length. Every frame is held in
# memory, twice over while it is worked on, which is what sets the count.
MAX_CLIP_SECONDS = 45
VIDEO_MAX_EDGE = 480
VIDEO_MAX_FPS = 30
VIDEO_MIN_FPS = 10
VIDEO_MAX_FRAMES = MAX_CLIP_SECONDS * VIDEO_MIN_FPS
VIDEO_TIMEOUT_SECONDS = 180.0
VIDEO_CONTAINERS = {"mov": "mov,mp4,m4a,3gp,3g2,mj2", "matroska": "matroska,webm"}


def video_container(data: bytes) -> str | None:
    """Which kind of video file these bytes are, going by how they start, or
    None for anything else. Names and content types are not trusted."""
    if data[4:8] == b"ftyp":
        return "mov"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    return None


def video_seconds(data: bytes) -> float | None:
    """How long a video runs, or None if that cannot be told."""
    container = video_container(data)
    ffprobe = shutil.which("ffprobe")
    if container is None or ffprobe is None:
        return None
    with tempfile.NamedTemporaryFile(suffix=".video") as source:
        source.write(data)
        source.flush()
        command = [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", VIDEO_CONTAINERS[container],
                   "-show_entries", "format=duration", "-of", "csv=p=0", source.name]
        try:
            return max(0.0, float(subprocess.run(command, capture_output=True, timeout=10, text=True).stdout.strip()))
        except (ValueError, subprocess.TimeoutExpired):
            return None


def video_frame_rate(seconds: float | None) -> float:
    """How many frames a second to take from a video of this length, so that
    all of it fits in VIDEO_MAX_FRAMES if it can."""
    if not seconds:
        return float(VIDEO_MAX_FPS)
    return float(min(VIDEO_MAX_FPS, max(VIDEO_MIN_FPS, VIDEO_MAX_FRAMES / min(seconds, MAX_CLIP_SECONDS))))


def video_to_animation(data: bytes) -> bytes:
    """A video as an animated WebP: at most VIDEO_MAX_FRAMES frames, spread
    over as much of it as video_frame_rate allows, no larger than
    VIDEO_MAX_EDGE. Everything downstream reads it the way it reads a GIF, and
    WebP is several times quicker to make than a GIF and keeps the video's colors."""
    container = video_container(data)
    ffmpeg = shutil.which("ffmpeg")
    if container is None or ffmpeg is None:
        raise ValueError("I can't read that video.")
    gap = 0.95 / video_frame_rate(video_seconds(data))
    steps = (
        f"select='isnan(prev_selected_t)+gte(t-prev_selected_t\\,{gap:.4f})',"
        f"scale='min({VIDEO_MAX_EDGE},iw)':'min({VIDEO_MAX_EDGE},ih)':force_original_aspect_ratio=decrease"
    )
    with tempfile.NamedTemporaryFile(suffix=".video") as source:
        source.write(data)
        source.flush()
        # The container is named outright and only local files may be opened, so
        # a crafted upload cannot point ffmpeg at a playlist, a URL or another file.
        command = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe", "-f", VIDEO_CONTAINERS[container],
                   "-t", str(MAX_CLIP_SECONDS), "-i", source.name, "-an", "-sn", "-map", "0:v:0", "-vf", steps, "-fps_mode", "vfr",
                   "-frames:v", str(VIDEO_MAX_FRAMES), "-threads", "2", "-c:v", "libwebp_anim", "-q:v", "90",
                   "-compression_level", "0", "-loop", "0", "-f", "webp", "pipe:1"]
        try:
            done = subprocess.run(command, capture_output=True, timeout=VIDEO_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise ValueError("That video took too long to read. Try a shorter one.") from exc
    if done.returncode != 0 or not done.stdout:
        logging.getLogger(__name__).info("ffmpeg could not read a video: %s", done.stderr.decode(errors="replace")[-300:])
        raise ValueError("I can't read that video.")
    return done.stdout
# Cap what we'll buffer into RAM, whether from a URL or an oversized attachment.
MAX_IMAGE_DOWNLOAD_BYTES = 25 * 1024 * 1024
# Every frame is expanded to RGBA in memory during processing, and a GIF stores
# every frame whole, so this is as many as a GIF going in is read as and a GIF
# coming out has. A longer one is not cut short: frames are left out evenly
# along it, each kept frame showing for the time of the ones dropped after it.
MAX_GIF_FRAMES = 120
# A GIF is not read past this many frames, whatever is kept of them.
MAX_SCANNED_FRAMES = 3000
# A picture's size on disk says nothing about what it costs to work on: a few
# kilobytes of GIF can describe thousands of pixels square, and every frame is
# held as four bytes a pixel. So pictures are also limited by pixels. Pillow
# refuses to open anything over twice MAX_IMAGE_PIXELS, which makes this limit
# hold for every Image.open in the bot, whoever calls it.
MAX_INPUT_PIXELS = 40_000_000  # one picture or one frame: about 160 MB as RGBA
Image.MAX_IMAGE_PIXELS = MAX_INPUT_PIXELS // 2
warnings.simplefilter("ignore", Image.DecompressionBombWarning)  # between the two is allowed, quietly
# All the frames a command holds at once. A bigger animation has its frames
# shrunk to fit as they are read (about 730 pixels square at 120 frames).
MAX_TOTAL_PIXELS = 64_000_000
TOO_LARGE_MESSAGE = "That picture is too large to work on. Anything up to about 40 megapixels (around 7000 by 5500) is fine."
# Image work runs in executor threads, this many at once, bot-wide. A command
# takes as long as it takes: the slowest effects on a full-length video run for
# minutes. The one limit is Discord's, which stops accepting a command's reply
# fifteen minutes after it was run; work is given up a little before that.
MAX_CONCURRENT_CPU_BOUND = 2
CPU_BOUND_TIMEOUT_SECONDS = 14 * 60.0
# What a bot can upload where the server has no boosts (and in DMs).
DEFAULT_UPLOAD_LIMIT_BYTES = 10 * 1024 * 1024
# GIFs do not go faster than this in most viewers; shorter frames get slowed down instead.
MIN_GIF_FRAME_MS = 20
# How Discord writes custom emoji and mentions inside a message or a command option.
CUSTOM_EMOJI_PATTERN = re.compile(r"<a?:(\w{1,32}):(\d{15,22})>")
MENTION_PATTERN = re.compile(r"<(@!?|@&|#)(\d{15,22})>")
MAX_CUSTOM_EMOJI = 20  # distinct ones fetched for one piece of text

# The commands slow/heavy enough (DNN face detection, seam-carving DP, per-pixel
# swirl warping, ...) that someone firing off a bunch back-to-back is actually
# piling up real work, not just spamming a cheap resize. Tracked per (user,
# channel) so the snark below is aimed at whoever's actually causing the pile-up.
HEAVY_COMMANDS = frozenset(
    {"contentaware", "facebox", "blurfaces", "facecrop", "faceswap", "googlyeyes", "deepfry", "swirl", "shake",
     "removebg", "wave", "rainbow", "spin", "pet", "bonk"}
)
HEAVY_COMMAND_WINDOW_SECONDS = 30.0
HEAVY_COMMAND_SNARK_THRESHOLD = 3

SNARK_LINES = (
    "Whoa there, speedrunner - my circuits need a breather between those.",
    "One at a time, please. I've only got so many hamsters in here.",
    "You keep this up and I'm unionizing.",
    "Slow your roll - the pixels aren't going anywhere.",
    "I see we're doing a lot of this today. Cool, cool, cool, no rush on my end at all.",
    "Another one already? I'm flattered, but also tired.",
)


FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"


@functools.lru_cache(maxsize=64)
def load_font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """A font from assets/fonts ("impact.ttf", "arial.ttf") at a pixel size,
    or Pillow's built-in font if the file is missing."""
    try:
        return ImageFont.truetype(str(FONTS_DIR / name), size)
    except OSError:
        return ImageFont.load_default(size)


@dataclass(slots=True)
class Frames:
    """A decoded image: one RGBA frame for a still, several for a GIF, each
    with how long it shows (milliseconds)."""

    frames: list[Image.Image]
    durations: list[int]
    loop: int = 0
    animated: bool = False

    @property
    def size(self) -> tuple[int, int]:
        return self.frames[0].size


def frame_edge_limit(size: tuple[int, int], frames: int, max_edge: int | None = None) -> int | None:
    """The longest edge frames of an animation may keep so that `frames` of
    them fit in MAX_TOTAL_PIXELS, or `max_edge` if that is smaller. None when
    they fit as they are."""
    width, height = size
    if width * height * max(1, frames) <= MAX_TOTAL_PIXELS:
        return max_edge
    scale = (MAX_TOTAL_PIXELS / (width * height * frames)) ** 0.5
    edge = max(64, int(max(width, height) * scale))
    return min(edge, max_edge) if max_edge else edge


def check_canvas(width: int, height: int) -> None:
    """Refuse to build a picture bigger than one that would be accepted."""
    if width * height > MAX_INPUT_PIXELS:
        raise ValueError("That would make a picture too large to send. Try a smaller picture or a smaller overlay.")


def fit_within(image: Image.Image, max_edge: int) -> Image.Image:
    """The image shrunk so its longer edge is at most max_edge (never enlarged)."""
    longest = max(image.size)
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)


def frame_budget(gif: int = MAX_GIF_FRAMES, video: int = VIDEO_MAX_FRAMES) -> int:
    """How many frames the running command may work on: `video` when its
    picture came from a video (the result is a video too, which holds many
    frames cheaply), `gif` otherwise."""
    return video if _video_job.get() is not None else gif


# Things a command's work wants said in its reply, such as that the clip was
# cut. A dict the work can write to from its thread; started by defer().
_run_notes: contextvars.ContextVar[dict | None] = contextvars.ContextVar("image_run_notes", default=None)


def note_cut(seconds: float) -> None:
    notes = _run_notes.get()
    if notes is not None:
        notes.setdefault("cut_at", seconds)


def _frames_to_keep(image: Image.Image, limit: int) -> tuple[int, set[int] | None]:
    """(how many frames of the picture to read, which of them to keep). None
    means all of them: only a picture with more than `limit` is thinned. A GIF
    that runs past MAX_CLIP_SECONDS is read up to there, which is noted for
    the reply."""
    count = min(int(getattr(image, "n_frames", 1) or 1), MAX_SCANNED_FRAMES)
    if image.format == "GIF" and count > 1:  # a GIF's timing can be read without decoding its pictures
        clock = 0
        for index in range(count):
            image.seek(index)
            clock += int(image.info.get("duration", 100) or 100)
            if clock >= MAX_CLIP_SECONDS * 1000 and index + 1 < count:
                count = index + 1
                note_cut(clock / 1000.0)
                break
        image.seek(0)
    if count <= limit:
        return count, None
    return count, {round(index * count / limit) for index in range(limit)}


def load_frames(image_bytes: bytes, filename: str | None = None, *, max_edge: int | None = None, first_only: bool = False) -> Frames:
    """Decode an upload into RGBA frames. An animation with more frames than
    the command may work on (frame_budget) is thinned evenly, so it lasts as
    long as it did. `max_edge` shrinks big inputs; `first_only` takes a GIF's
    first frame as a still."""
    with Image.open(io.BytesIO(image_bytes)) as image:
        animated = bool(getattr(image, "is_animated", False)) and not first_only
        loop = int(image.info.get("loop", 0) or 0)
        frames: list[Image.Image] = []
        durations: list[int] = []
        if not animated:
            converted = image.convert("RGBA")
            return Frames([fit_within(converted, max_edge) if max_edge else converted], [int(image.info.get("duration", 100) or 100)], loop)
        count, keep = _frames_to_keep(image, frame_budget())
        max_edge = frame_edge_limit(image.size, count if keep is None else len(keep), max_edge)
        for index, frame in enumerate(itertools.islice(ImageSequence.Iterator(image), count)):
            if keep is not None and index not in keep:
                frame.load()  # decoded even so: some formats only know a frame's timing once it is read
                durations[-1] += int(frame.info.get("duration", 100) or 100)
                continue
            converted = frame.convert("RGBA")
            durations.append(int(frame.info.get("duration", 100) or 100))
            frames.append(fit_within(converted, max_edge) if max_edge else converted)
    return Frames(frames, durations, loop, len(frames) > 1)


def gif_durations(durations: list[int] | list[float]) -> list[int]:
    """Frame times a GIF can hold. A GIF counts in hundredths of a second, and
    a time that is not a whole number of them is cut short when written: a
    video's 66 ms frames would each show for 60, and the result would run a
    tenth too fast. Here each frame is rounded and what was lost or gained is
    passed on to the next, so the whole still lasts as long as it should."""
    result: list[int] = []
    owed = 0.0
    for duration in durations:
        wanted = max(MIN_GIF_FRAME_MS, float(duration or 100)) + owed
        shown = max(MIN_GIF_FRAME_MS, int(round(wanted / 10.0)) * 10)
        owed = wanted - shown
        result.append(shown)
    return result


def encode_frames(frames: list[Image.Image], durations: list[int] | int, stem: str, *, loop: int = 0,
                  keep_gif: bool = False) -> tuple[bytes, str]:
    """PNG for one frame, GIF for several. Returns (data, filename). A command
    working on a video gets a stand-in for the GIF (see write_animation)
    unless it asks to `keep_gif`."""
    output = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(output, format="PNG")
        return output.getvalue(), f"{stem}.png"
    if isinstance(durations, int):
        durations = [durations] * len(frames)
    return write_animation(frames, durations, loop, keep_gif=keep_gif), f"{stem}.gif"


@dataclass(slots=True)
class VideoJob:
    """A command whose picture came from a video. The result goes back as a
    video with the original sound, so the finished frames are kept here
    instead of being written as a GIF; send_processed_file makes the video."""

    source: bytes  # the video as it was uploaded: where the sound comes from
    source_ms: int  # how much of it the frames cover
    interaction: object = None
    frames: list[Image.Image] | None = None
    durations: list[int] | None = None
    sound_filter: str | None = None  # an ffmpeg audio filter for the sound, set by the command
    cut_at: float | None = None  # seconds used, when the video ran longer than could be worked on


_video_job: contextvars.ContextVar[VideoJob | None] = contextvars.ContextVar("video_job", default=None)
# What a command is handed in place of a GIF while a video job holds its frames.
def _placeholder() -> bytes:
    output = io.BytesIO()
    Image.new("L", (1, 1)).save(output, format="GIF", comment=b"neo-gigabot video job")
    return output.getvalue()


VIDEO_PLACEHOLDER = _placeholder()


def write_animation(frames: list[Image.Image], durations: list[int], loop: int = 0, *, keep_gif: bool = False) -> bytes:
    """The frames as a GIF. When the command's picture came from a video (and
    the command is not one that promises a GIF: `keep_gif`), they are set
    aside for the video instead and VIDEO_PLACEHOLDER is returned."""
    job = _video_job.get()
    if job is not None and not keep_gif:
        job.frames, job.durations = list(frames), [int(duration) for duration in durations]
        return VIDEO_PLACEHOLDER
    output = io.BytesIO()
    frames, durations = thin_frames(list(frames), list(durations), MAX_GIF_FRAMES)  # a GIF from a video's frames: fewer, as long
    frames = [frame.convert("RGBA") for frame in frames]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], loop=loop,
                   duration=gif_durations(durations), disposal=2)
    return output.getvalue()


VIDEO_OUT_FPS_LIMIT = 50
VIDEO_OUT_MAX_TICKS = 1500
VIDEO_OUT_TIMEOUT_SECONDS = 300.0


def frames_to_video(frames: list[Image.Image], durations: list[int], *, sound_from: bytes | None = None,
                    sound_filter: str | None = None) -> bytes:
    """The frames as an MP4 (H.264, plays in Discord and on phones), with the
    sound of the video `sound_from` laid under it from its start, cut to the
    length of the frames, and run through the ffmpeg audio filter
    `sound_filter` if there is one. Transparent parts come out black."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("I can't make videos here.")
    width, height = frames[0].size
    total_ms = max(1, sum(durations))
    # ffmpeg is fed plain pictures at one steady rate. Frames that all last
    # about the same go in as they are; uneven ones are repeated on a finer
    # clock, which costs nothing in the file since a repeat compresses away.
    if max(durations) - min(durations) <= 3:
        rate, order = len(frames) * 1000.0 / total_ms, list(range(len(frames)))
    else:
        tick = max(1000.0 / VIDEO_OUT_FPS_LIMIT, float(min(durations)), total_ms / VIDEO_OUT_MAX_TICKS)
        rate, order, ends, clock = 1000.0 / tick, [], list(itertools.accumulate(durations)), 0.0
        index = 0
        while clock < total_ms:
            while index < len(frames) - 1 and clock >= ends[index]:
                index += 1
            order.append(index)
            clock += tick
    sound = video_container(sound_from) if sound_from else None
    with tempfile.TemporaryDirectory() as folder:
        target = Path(folder) / "out.mp4"
        command = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file,pipe",
                   "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{width}x{height}", "-framerate", f"{rate:.4f}", "-i", "pipe:0"]
        if sound:
            original = Path(folder) / "original.video"
            original.write_bytes(sound_from)
            # named outright, as when it was read: a crafted upload cannot point ffmpeg anywhere else
            command += ["-f", VIDEO_CONTAINERS[sound], "-i", str(original), "-map", "0:v:0", "-map", "1:a:0?",
                        "-c:a", "aac", "-b:a", "128k"]
            if sound_filter:
                command += ["-af", sound_filter]
        command += ["-t", f"{total_ms / 1000.0:.3f}", "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-preset", "veryfast",
                    "-crf", "23", "-pix_fmt", "yuv420p", "-threads", "2", "-movflags", "+faststart", "-f", "mp4", str(target)]
        process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        shown, pixels = -1, b""
        try:
            for index in order:
                if index != shown:  # a repeat is written again as it is
                    frame = frames[index].convert("RGBA")
                    if frame.size != (width, height):
                        frame = frame.resize((width, height), Image.LANCZOS)
                    ground = Image.new("RGB", (width, height), (0, 0, 0))
                    ground.paste(frame, (0, 0), frame)
                    shown, pixels = index, ground.tobytes()
                process.stdin.write(pixels)
            _, errors = process.communicate(timeout=VIDEO_OUT_TIMEOUT_SECONDS)
        except (BrokenPipeError, subprocess.TimeoutExpired) as exc:
            process.kill()
            process.communicate()
            raise ValueError("That video took too long to make. Try a shorter one.") from exc
        if process.returncode != 0 or not target.is_file():
            logging.getLogger(__name__).info("ffmpeg could not make a video: %s", (errors or b"").decode(errors="replace")[-300:])
            raise ValueError("I couldn't make that into a video.")
        return target.read_bytes()


def set_video_sound_filter(audio_filter: str | None) -> bool:
    """Have the running command's video come back with its sound put through
    an ffmpeg audio filter. False when the command is not working on a video."""
    job = _video_job.get()
    if job is None:
        return False
    job.sound_filter = audio_filter
    return True


def current_video_source() -> bytes | None:
    """The video the running command's picture came from, if it was one."""
    job = _video_job.get()
    return job.source if job is not None else None


def animation_ms(data: bytes) -> int:
    """How long an animation runs for, start to finish."""
    with Image.open(io.BytesIO(data)) as image:
        total = 0
        for frame in itertools.islice(ImageSequence.Iterator(image), MAX_SCANNED_FRAMES):
            frame.load()
            total += int(frame.info.get("duration", 100) or 100)
        return total


def thin_frames(frames: list, durations: list[int], limit: int) -> tuple[list, list[int]]:
    """At most `limit` frames, evenly kept, each taking on the time of the
    ones dropped after it, so the animation lasts as long as before."""
    count = len(frames)
    if count <= limit:
        return frames, durations
    kept = sorted({round(index * count / limit) for index in range(limit)})
    edges = kept[1:] + [count]
    return [frames[start] for start in kept], [sum(durations[start:end]) for start, end in zip(kept, edges)]


def play_together(sources: list[Frames], *, limit: int | None = None) -> tuple[list[tuple[Image.Image, ...]], list[int], int]:
    """Several inputs playing side by side: for each frame of the result, the
    picture every input is showing at that moment, with the frame's time.
    Stills just stay put. The input with the most frames sets the pace, and
    the others are looked up by the clock, looping if they are shorter.
    Returns (pictures per frame, durations, loop)."""
    moving = [source for source in sources if source.animated]
    if not moving:
        return [tuple(source.frames[0] for source in sources)], [100], 0
    lead = max(moving, key=lambda source: len(source.frames))
    order, durations = thin_frames(list(range(len(lead.frames))), lead.durations, limit or frame_budget())

    def showing(source: Frames, at: float) -> Image.Image:
        if not source.animated:
            return source.frames[0]
        at %= max(1, sum(source.durations))
        for frame, duration in zip(source.frames, source.durations):
            if at < duration:
                return frame
            at -= duration
        return source.frames[-1]

    together, clock = [], 0.0
    for index, duration in zip(order, durations):
        together.append(tuple(lead.frames[index] if source is lead else showing(source, clock) for source in sources))
        clock += duration
    return together, durations, lead.loop


def compose_frames(sources: list[Frames], build: Callable[..., Image.Image], stem: str,
                   *, max_edge: int | None = None, limit: int | None = None) -> tuple[bytes, str]:
    """A picture built from one or more inputs: `build(*pictures)` is called
    once for a still result, or once per frame when any input is a GIF or a
    video, which gives a GIF. `max_edge` shrinks the frames of an animated
    result (a still is left at full size), and `limit` caps how many it has."""
    together, durations, loop = play_together(sources, limit=limit)
    if len(together) == 1:
        return encode_frames([build(*together[0])], [100], stem)
    frames = [build(*pictures) for pictures in together]
    if max_edge:
        frames = [fit_within(frame, max_edge) for frame in frames]
    return encode_frames(frames, durations, stem, loop=loop)


def apply_to_frames(image_bytes: bytes, filename: str | None, processor: Callable[[Image.Image], Image.Image], stem: str,
                    *, max_edge: int | None = None) -> tuple[bytes, str]:
    """Run processor over every frame of a still or GIF and encode the result."""
    loaded = load_frames(image_bytes, filename, max_edge=max_edge)
    return encode_frames([processor(frame) for frame in loaded.frames], loaded.durations, stem, loop=loaded.loop)


# The message an image command should read its image from, set by
# ImageToolkit.pinned_source for the length of one command.
_pinned_message: contextvars.ContextVar[discord.Message | None] = contextvars.ContextVar("pinned_image_message", default=None)


class ImageSourceStore:
    """Per-user 'selected image source' cache, set via the Set/Clear Image Source
    context menus. Shared across every image-command cog (not just cogs/images.py,
    which owns the context menu registration) so a source picked once is usable by
    any command that resolves an image, regardless of which cog defines it."""

    def __init__(self) -> None:
        self._sources: dict[int, dict[str, int | float | str | None]] = {}

    def _prune(self) -> None:
        now = time.time()
        stale = [user_id for user_id, data in self._sources.items() if now - float(data["timestamp"]) > SOURCE_TTL_SECONDS]
        for user_id in stale:
            self._sources.pop(user_id, None)

    def remember(self, user_id: int, channel_id: int, message_id: int, *, url: str | None = None, filename: str | None = None) -> None:
        """`url` is the picture's own link, for when the message cannot be
        read again: a server the bot is not in, or a DM between people."""
        self._sources[user_id] = {
            "channel_id": channel_id,
            "message_id": message_id,
            "url": url,
            "filename": filename,
            "timestamp": time.time(),
        }

    def clear(self, user_id: int) -> bool:
        return self._sources.pop(user_id, None) is not None

    def get(self, user_id: int) -> dict[str, int | float | str | None] | None:
        self._prune()
        return self._sources.get(user_id)


class ImageToolkit:
    """Shared image-command plumbing: access checks, source resolution (attachment
    -> message link -> selected source -> latest image in channel), GIF-aware frame
    processing, and generic overlay/append compositing. Instantiated once as
    `bot.image_tools`, exactly like `bot.embeds` (see main.py), so cogs/images.py
    shares one instance instead of duplicating this logic."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.sources = ImageSourceStore()
        self._heavy_command_uses: dict[tuple[int, int], list[float]] = {}
        self._cpu_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CPU_BOUND)

    # ---------- running image work ----------

    async def run_cpu_bound(self, func: Callable[[], object], *, timeout: float = CPU_BOUND_TIMEOUT_SECONDS) -> object:
        """Run blocking image work in an executor thread, under the bot-wide
        concurrency cap, giving up after `timeout`."""
        # The semaphore is released when the worker thread actually finishes,
        # not when we stop waiting: a timeout can only abandon the thread, and
        # releasing early would let real concurrency exceed the cap under
        # repeated timeouts. shield() keeps wait_for from cancelling the
        # executor future so the done callback fires at true completion.
        loop = asyncio.get_running_loop()
        await self._cpu_semaphore.acquire()
        try:
            # run in a copy of this task's context, so the work can see the command's video job
            future = loop.run_in_executor(None, contextvars.copy_context().run, func)
        except BaseException:
            self._cpu_semaphore.release()
            raise

        def _on_done(done_future: asyncio.Future) -> None:
            self._cpu_semaphore.release()
            if not done_future.cancelled():
                done_future.exception()  # retrieve, so abandoned failures don't log "never retrieved"

        future.add_done_callback(_on_done)
        try:
            return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise RuntimeError("That took too long to process. Try a smaller image or less aggressive settings.") from exc

    def check_upload_size(self, interaction: discord.Interaction, data: bytes) -> None:
        guild = home_guild(interaction)  # a server without the bot is not known well enough to have a limit
        limit = guild.filesize_limit if guild is not None else DEFAULT_UPLOAD_LIMIT_BYTES
        if len(data) > limit:
            raise ValueError(
                f"The result is {len(data) / 1048576:.1f} MB, more than the {limit // 1048576} MB I can upload here. Try a smaller image or a shorter GIF."
            )

    async def run_image_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        title: str,
        work: Callable[..., tuple],
        *,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        extra_sources: list[tuple[discord.Attachment | None, str | None]] | None = None,
        source: Callable[[], Awaitable[tuple[bytes, str]]] | None = None,
        before: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        """The whole life of a simple image command: the blocked-command check,
        finding the source image, running `work(image_bytes, filename, *extras)`
        off the event loop, and sending the file or a readable error. `work`
        returns (data, filename) or (data, filename, description). Each entry of
        `extra_sources` is another image to fetch and pass along as bytes, and
        `source` replaces the usual search for the main image (an avatar, say).
        `before` runs once the command has been acknowledged and before the
        work: the place for slow preparation such as `prepare_text`."""
        if not await self.ensure_command_allowed(interaction, command_name):
            return
        await self.note_heavy_command(interaction, command_name)
        await self.defer(interaction)
        try:
            if before is not None:
                await before()
            image_bytes, filename = await (source() if source else self.get_source_image(interaction, attachment, message_link))
            extras = [(await self.get_source_image(interaction, extra, link))[0] for extra, link in extra_sources or []]
            result = await self.run_cpu_bound(lambda: work(image_bytes, filename, *extras))
            data, out_name = result[0], result[1]
            self.check_upload_size(interaction, data)
            await self.send_processed_file(
                interaction, data=data, filename=out_name, title=title, description=result[2] if len(result) > 2 else None
            )
        except Exception as exc:
            await self.send_error(interaction, f"{title} Failed", self.user_error_message(exc))

    # ---------- access / response helpers ----------

    async def note_heavy_command(self, interaction: discord.Interaction, command_name: str) -> None:
        """Track back-to-back heavy commands from the same user in the same channel
        and needle them with a snarky follow-up once they've fired off enough in
        quick succession. Purely cosmetic - never blocks or delays the command."""
        if command_name not in HEAVY_COMMANDS or interaction.channel is None:
            return

        key = (interaction.user.id, interaction.channel.id)
        now = time.time()
        # Entries go stale after HEAVY_COMMAND_WINDOW_SECONDS but only the
        # touched key gets rewritten below, so sweep the whole dict once it
        # accumulates enough dead (user, channel) keys to matter.
        if len(self._heavy_command_uses) > 256:
            self._heavy_command_uses = {
                k: times
                for k, times in self._heavy_command_uses.items()
                if any(now - t < HEAVY_COMMAND_WINDOW_SECONDS for t in times)
            }
        recent = [t for t in self._heavy_command_uses.get(key, []) if now - t < HEAVY_COMMAND_WINDOW_SECONDS]
        recent.append(now)
        self._heavy_command_uses[key] = recent

        if len(recent) >= HEAVY_COMMAND_SNARK_THRESHOLD:
            try:
                await interaction.channel.send(random.choice(SNARK_LINES))
            except discord.HTTPException:
                pass

    async def ensure_command_allowed(self, interaction: discord.Interaction, command_name: str) -> bool:
        return await self.bot.ensure_command_allowed(interaction, command_name)

    async def defer(self, interaction: discord.Interaction) -> None:
        _video_job.set(None)
        _run_notes.set({})
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)

    async def send_processed_file(
        self,
        interaction: discord.Interaction,
        *,
        data: bytes,
        filename: str,
        title: str,
        description: str | None = None,
    ) -> None:
        job = _video_job.get()
        data, filename = await self.finish_video(interaction, data, filename)
        file = discord.File(io.BytesIO(data), filename=filename)
        description = description or f"Generated `{filename}`."
        notes = _run_notes.get() or {}
        cut_at = job.cut_at if job is not None and job.frames is not None else notes.get("cut_at")
        if cut_at:
            description += f"\nThat was longer than I can work on, so this is its first {cut_at:.0f} seconds."
        embed = self.bot.embeds.info_embed(title, description)
        if filename.lower().endswith(SUPPORTED_EXTENSIONS):  # video and sound cannot go inside an embed: Discord shows them under it
            embed.set_image(url=f"attachment://{filename}")
        await interaction.followup.send(embed=embed, file=file)

    async def finish_video(self, interaction: discord.Interaction, data: bytes, filename: str) -> tuple[bytes, str]:
        """If the command worked on a video and its result moves, make the
        video: the finished frames with the original sound. Anything else is
        passed through as it is."""
        job = _video_job.get()
        _video_job.set(None)
        if job is None or job.frames is None or data != VIDEO_PLACEHOLDER:
            return data, filename
        frames, durations = job.frames, job.durations
        total = sum(durations)
        # The sound only fits if the result still runs like the original: not
        # sped up, slowed down or cut short. A second of extra at the end is fine.
        fits = job.source_ms * 0.9 <= total <= job.source_ms + 1000
        try:
            video = await self.run_cpu_bound(
                lambda: frames_to_video(frames, durations, sound_from=job.source if fits else None, sound_filter=job.sound_filter)
            )
        except ValueError:
            # no video after all: the GIF every other animated result gets
            return await self.run_cpu_bound(lambda: write_animation(frames, durations, keep_gif=True)), filename
        self.check_upload_size(interaction, video)
        return video, f"{Path(filename).stem}.mp4"

    def encode_gif(self, frames: list[Image.Image], image: Image.Image) -> bytes:
        """The processed frames of `image` as a GIF with its timing (or, for a
        command working on a video, the stand-in: see write_animation)."""
        durations = getattr(image, "_frame_durations", None) or [int(image.info.get("duration", 100) or 100)]
        if len(durations) != len(frames):
            durations = [durations[0]] * len(frames)
        return write_animation(frames, durations, int(image.info.get("loop", 0) or 0))

    def user_error_message(self, exc: Exception) -> str:
        """ValueError/RuntimeError raised inside the image pipeline carry
        deliberately user-facing text ("No recent image was found...", "That
        took too long..."). Anything else is a bug - log it and keep
        PIL/aiohttp internals out of the embed."""
        if isinstance(exc, Image.DecompressionBombError):
            return TOO_LARGE_MESSAGE
        if isinstance(exc, (ValueError, RuntimeError)):
            return str(exc)
        logging.getLogger(__name__).exception("Unexpected image-command error", exc_info=exc)
        return "Something went wrong while processing that image."

    async def send_error(self, interaction: discord.Interaction, title: str, description: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=self.bot.embeds.error_embed(title, description), ephemeral=True)
        else:
            await self.bot.embeds.error_interaction(interaction, title, description, ephemeral=True)

    async def send_success(self, interaction: discord.Interaction, title: str, description: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=self.bot.embeds.success_embed(title, description), ephemeral=True)
        else:
            await self.bot.embeds.success_interaction(interaction, title, description, ephemeral=True)

    # ---------- text ----------

    async def prepare_text(self, text: str, guild: discord.Guild | None = None) -> RichText:
        """Get text typed in Discord ready to draw: mentions become names
        (@Name, #channel), and custom emoji become pictures the text renderer
        puts inline. A custom emoji that can't be fetched is written as :name:."""

        def mention(match: re.Match) -> str:
            kind, target = match.group(1), int(match.group(2))
            if guild is None:
                return match.group(0)
            if kind == "#":
                channel = guild.get_channel_or_thread(target)
                return f"#{channel.name}" if channel else "#unknown"
            if kind == "@&":
                role = guild.get_role(target)
                return f"@{role.name}" if role else "@unknown"
            member = guild.get_member(target)
            return f"@{member.display_name}" if member else "@unknown"

        text = MENTION_PATTERN.sub(mention, text)
        images: dict[str, Image.Image] = {}
        placeholders: dict[str, str] = {}  # emoji id -> the character standing in for it
        for name, emoji_id in CUSTOM_EMOJI_PATTERN.findall(text):
            if emoji_id in placeholders or len(images) >= MAX_CUSTOM_EMOJI:
                continue
            try:
                data = await self.fetch_bytes_from_url(f"https://cdn.discordapp.com/emojis/{emoji_id}.png?size=96")
                with Image.open(io.BytesIO(data)) as picture:
                    key = chr(PLACEHOLDER_BASE + len(images))
                    images[key] = picture.convert("RGBA")
                    placeholders[emoji_id] = key
            except Exception:  # deleted emoji, network trouble, not an image: fall back to its name
                continue
        text = CUSTOM_EMOJI_PATTERN.sub(lambda match: placeholders.get(match.group(2), f":{match.group(1)}:"), text)
        return RichText(text, images)

    # ---------- source selection helpers ----------

    def supported_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = attachment.content_type or ""
        name = attachment.filename.lower()
        if content_type.startswith("image/") or name.endswith(SUPPORTED_EXTENSIONS):
            return True
        return content_type.startswith("video/") or name.endswith(VIDEO_EXTENSIONS)

    def message_has_image(self, message: discord.Message) -> bool:
        if any(self.supported_attachment(attachment) for attachment in message.attachments):
            return True
        return embed_image_url(message) is not None

    async def fetch_bytes_from_url(self, url: str) -> bytes:
        if not is_discord_image_host(url):
            raise ValueError("I only fetch embedded images hosted on Discord's CDN - upload the image or attach it directly.")

        max_bytes = MAX_IMAGE_DOWNLOAD_BYTES
        async with self.bot.http_session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
            response.raise_for_status()
            if response.content_length is not None and response.content_length > max_bytes:
                raise ValueError(f"That image is too large (max {max_bytes // (1024 * 1024)} MB).")
            data = bytearray()
            async for chunk in response.content.iter_chunked(64 * 1024):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise ValueError(f"That image is too large (max {max_bytes // (1024 * 1024)} MB).")
            return bytes(data)

    def _checked_attachment_size(self, attachment: discord.Attachment) -> None:
        if attachment.size > MAX_IMAGE_DOWNLOAD_BYTES:
            raise ValueError(f"That image is too large (max {MAX_IMAGE_DOWNLOAD_BYTES // (1024 * 1024)} MB).")

    async def fetch_message_from_link(self, interaction: discord.Interaction, message_link: str) -> discord.Message:
        return await fetch_message_from_link(self.bot, message_link, hint_guild=interaction.guild)

    async def get_media_source(
        self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> tuple[bytes, str]:
        """The video or audio file a sound command should work on, as it was
        uploaded: the attachment, the linked message's, or the message the
        Edit Image menu was opened on. Unlike get_source_image, nothing is
        decoded and there is no looking back through the channel."""
        if attachment is not None:
            self._checked_attachment_size(attachment)
            return await attachment.read(), attachment.filename
        message = await self.fetch_message_from_link(interaction, message_link) if message_link else _pinned_message.get()
        if message is None:
            raise ValueError("Attach a video or an audio file, or give a link to a message that has one.")
        for found in message.attachments:
            kind, name = (found.content_type or "").lower(), found.filename.lower()
            if kind.startswith(("video/", "audio/")) or name.endswith(VIDEO_EXTENSIONS + MEDIA_AUDIO_EXTENSIONS):
                self._checked_attachment_size(found)
                return await found.read(), found.filename
        url = embed_image_url(message)
        if url and urlparse(url).path.lower().endswith(VIDEO_EXTENSIONS):
            return await self.fetch_bytes_from_url(url), "embedded_video" + Path(urlparse(url).path).suffix
        raise ValueError("That message has no video or audio file in it.")

    def message_image_link(self, message: discord.Message) -> tuple[str, str] | None:
        """The link and filename of the picture extract_image_from_message would take."""
        for attachment in message.attachments:
            if self.supported_attachment(attachment):
                return attachment.url, attachment.filename
        url = embed_image_url(message)
        return (url, "embedded_image.png") if url else None

    async def extract_image_from_message(self, message: discord.Message) -> tuple[bytes, str]:
        for attachment in message.attachments:
            if self.supported_attachment(attachment):
                self._checked_attachment_size(attachment)
                return await attachment.read(), attachment.filename

        url = embed_image_url(message)
        if url:
            return await self.fetch_bytes_from_url(url), "embedded_image.png"

        raise ValueError("That message does not contain a supported image.")

    async def selected_message_image(self, interaction: discord.Interaction) -> tuple[bytes, str] | None:
        source = self.sources.get(interaction.user.id)
        if not source:
            return None

        channel_id = int(source["channel_id"])
        message_id = int(source["message_id"])

        guild = home_guild(interaction)
        if guild is not None:
            channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        else:
            channel = self.bot.get_channel(channel_id)

        if channel is None or not hasattr(channel, "fetch_message"):
            # Not a channel the bot can read. Used as a personal app, the
            # picture's link was kept when it was picked: fetch that.
            if source.get("url"):
                try:
                    return await self.fetch_bytes_from_url(str(source["url"])), str(source.get("filename") or "image.png")
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
            self.sources.clear(interaction.user.id)
            return None

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self.sources.clear(interaction.user.id)
            return None

        if not self.message_has_image(message):
            # edited since it was picked, say: forget it rather than fail every command until it expires
            self.sources.clear(interaction.user.id)
            return None
        return await self.extract_image_from_message(message)

    async def history_image(self, interaction: discord.Interaction) -> tuple[bytes, str]:
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            raise ValueError("I could not inspect recent messages in this channel.")

        if interaction.guild_id is not None and home_guild(interaction) is None:
            raise ValueError(NO_HISTORY_MESSAGE)
        try:
            async for message in channel.history(limit=50):
                if not self.message_has_image(message):
                    continue
                return await self.extract_image_from_message(message)
        except discord.HTTPException as exc:  # a DM between people, or a channel the bot may not read
            raise ValueError(NO_HISTORY_MESSAGE) from exc

        raise ValueError("No recent image was found in the last 50 messages.")

    @contextlib.contextmanager
    def pinned_source(self, message: discord.Message) -> Iterator[None]:
        """While this is open, a command run in the same task that was given no
        attachment and no message link takes its image from `message`. This is
        how the Edit Image menu hands a command the message that was clicked."""
        token = _pinned_message.set(message)
        try:
            yield
        finally:
            _pinned_message.reset(token)

    async def get_source_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> tuple[bytes, str]:
        """The image a command should work on. A video comes back as an
        animated WebP, and starts a video job: the command's result will be
        sent as a video with this one's sound (see VideoJob)."""
        job = _video_job.get()
        if job is not None and job.interaction is not interaction:
            _video_job.set(None)  # left by an earlier command
            job = None
        data, filename = await self._find_source_image(interaction, attachment, message_link)
        if video_container(data) is None:
            return data, filename

        def read() -> tuple[bytes, int, float | None]:
            animation = video_to_animation(data)
            return animation, animation_ms(animation), video_seconds(data)

        animation, covered, seconds = await self.run_cpu_bound(read)
        if job is None:  # with several videos in one command, the first one's sound is used
            cut_at = covered / 1000.0 if seconds and seconds * 1000 > covered + 1000 else None
            _video_job.set(VideoJob(source=data, source_ms=covered, interaction=interaction, cut_at=cut_at))
        return animation, f"{Path(filename).stem}.webp"

    async def _find_source_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> tuple[bytes, str]:
        if attachment is not None:
            if not self.supported_attachment(attachment):
                raise ValueError("That attachment is not a supported image.")
            self._checked_attachment_size(attachment)
            return await attachment.read(), attachment.filename

        if message_link:
            message = await self.fetch_message_from_link(interaction, message_link)
            return await self.extract_image_from_message(message)

        pinned = _pinned_message.get()
        if pinned is not None:
            return await self.extract_image_from_message(pinned)

        selected = await self.selected_message_image(interaction)
        if selected is not None:
            return selected

        return await self.history_image(interaction)

    # ---------- image format helpers ----------

    def is_gif(self, image: Image.Image, filename: str | None = None) -> bool:
        fmt = (image.format or "").upper()
        if fmt == "GIF":
            return True
        if filename and filename.lower().endswith(".gif"):
            return True
        return bool(getattr(image, "is_animated", False))

    def gif_frames(self, image: Image.Image):
        """Iterate a GIF's frames, as many as the command may work on
        (frame_budget). Every frame gets expanded to full RGBA in memory during
        processing, so all GIF loops in the image pipeline go through this cap -
        one upload must not be able to expand into gigabytes. A longer GIF is
        thinned evenly rather than cut short, as load_frames does.

        How long each frame shows is noted on the way past, for
        encode_gif. It is read after the caller has used the frame,
        because some formats only know a frame's timing once it is decoded."""
        durations: list[int] = []
        image._frame_durations = durations
        count, keep = _frames_to_keep(image, frame_budget())
        edge = frame_edge_limit(image.size, count if keep is None else len(keep))
        for index, frame in enumerate(itertools.islice(ImageSequence.Iterator(image), count)):
            if keep is not None and index not in keep:
                frame.load()
                durations[-1] += int(frame.info.get("duration", 100) or 100)
                continue
            yield frame if edge is None else fit_within(frame.convert("RGBA"), edge)
            durations.append(int(frame.info.get("duration", 100) or 100))

    def process_image_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        processor: Callable[[Image.Image], Image.Image],
        *,
        static_format: str = "PNG",
        static_name: str = "image.png",
        gif_name: str = "image.gif",
    ) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if self.is_gif(image, filename):
                frames: list[Image.Image] = []
                for frame in self.gif_frames(image):
                    processed = processor(frame.convert("RGBA"))
                    frames.append(processed.convert("RGBA"))
                return self.encode_gif(frames, image), gif_name

            processed = processor(image.convert("RGBA"))
            output = io.BytesIO()
            save_image = processed
            if static_format.upper() in {"JPEG", "JPG", "BMP"}:
                save_image = processed.convert("RGB")
            save_image.save(output, format=static_format.upper())
            return output.getvalue(), static_name

    # ---------- overlay / append helpers ----------
    #
    # These take a preloaded PIL Image for the overlay/append layer rather than a
    # Path, so the same compositing logic serves both cogs/images.py's static
    # template PNGs (opened from assets/images/) and its user-uploaded second
    # image (opened from attachment bytes).

    def overlay_image_onto(
        self,
        image_bytes: bytes,
        filename: str,
        overlay_image: Image.Image,
        *,
        placement: str = "bottom",
        strategy: str = "fit",
        opacity: float = 1.0,
        scale_percent: float | None = None,
        out_stem: str = "overlay",
    ) -> tuple[bytes, str]:
        overlay_image = overlay_image.convert("RGBA")

        def overlay_to_frame(input_frame: Image.Image) -> Image.Image:
            frame = input_frame.convert("RGBA")
            if strategy == "stretch":
                resized_overlay = overlay_image.resize(frame.size, Image.LANCZOS)
            elif strategy == "scale":
                pct = max(1.0, scale_percent or 40.0) / 100.0
                new_width = max(1, int(frame.width * pct))
                new_height = max(1, int(overlay_image.height * new_width / overlay_image.width))
                check_canvas(new_width, new_height)
                resized_overlay = overlay_image.resize((new_width, new_height), Image.LANCZOS)
            else:
                if placement in {"top", "bottom"}:
                    new_width = frame.width
                    new_height = int(overlay_image.height * new_width / overlay_image.width)
                else:
                    new_height = frame.height
                    new_width = int(overlay_image.width * new_height / overlay_image.height)
                check_canvas(new_width, new_height)
                resized_overlay = overlay_image.resize((new_width, new_height), Image.LANCZOS)

            if opacity < 1:
                alpha = resized_overlay.getchannel("A")
                alpha = ImageEnhance.Brightness(alpha).enhance(opacity)
                resized_overlay.putalpha(alpha)

            paste_coords = {
                "top": ((frame.width - resized_overlay.width) // 2, 0),
                "bottom": ((frame.width - resized_overlay.width) // 2, frame.height - resized_overlay.height),
                "left": (0, (frame.height - resized_overlay.height) // 2),
                "right": (frame.width - resized_overlay.width, (frame.height - resized_overlay.height) // 2),
                "center": ((frame.width - resized_overlay.width) // 2, (frame.height - resized_overlay.height) // 2),
                "topleft": (0, 0),
                "topright": (frame.width - resized_overlay.width, 0),
                "bottomleft": (0, frame.height - resized_overlay.height),
                "bottomright": (frame.width - resized_overlay.width, frame.height - resized_overlay.height),
            }
            new_frame = frame.copy()
            new_frame.paste(resized_overlay, paste_coords[placement], resized_overlay)
            return new_frame

        return self.process_image_bytes(
            image_bytes,
            filename,
            overlay_to_frame,
            static_format="PNG",
            static_name=f"{out_stem}.png",
            gif_name=f"{out_stem}.gif",
        )

    def append_image_to(
        self,
        image_bytes: bytes,
        filename: str,
        append_image: Image.Image,
        *,
        placement: str = "bottom",
        out_stem: str = "appended",
    ) -> tuple[bytes, str]:
        append_image = append_image.convert("RGBA")

        def add_to_frame(input_frame: Image.Image) -> Image.Image:
            frame = input_frame.convert("RGBA")
            if placement in {"top", "bottom"}:
                check_canvas(frame.width, frame.height + int(frame.width * append_image.height / append_image.width))
            else:
                check_canvas(frame.width + int(frame.height * append_image.width / append_image.height), frame.height)
            if placement in {"top", "bottom"}:
                scaled = append_image.resize(
                    (frame.width, int(frame.width * append_image.height / append_image.width)),
                    Image.LANCZOS,
                )
                new_width = frame.width
                new_height = frame.height + scaled.height
            else:
                scaled = append_image.resize(
                    (int(frame.height * append_image.width / append_image.height), frame.height),
                    Image.LANCZOS,
                )
                new_width = frame.width + scaled.width
                new_height = frame.height

            new_frame = Image.new("RGBA", (new_width, new_height), (0, 0, 0, 0))
            if placement == "bottom":
                new_frame.paste(frame, (0, 0), frame)
                new_frame.paste(scaled, (0, frame.height), scaled)
            elif placement == "top":
                new_frame.paste(scaled, (0, 0), scaled)
                new_frame.paste(frame, (0, scaled.height), frame)
            elif placement == "left":
                new_frame.paste(scaled, (0, 0), scaled)
                new_frame.paste(frame, (scaled.width, 0), frame)
            else:
                new_frame.paste(frame, (0, 0), frame)
                new_frame.paste(scaled, (frame.width, 0), scaled)
            return new_frame

        return self.process_image_bytes(
            image_bytes,
            filename,
            add_to_frame,
            static_format="PNG",
            static_name=f"{out_stem}.png",
            gif_name=f"{out_stem}.gif",
        )
