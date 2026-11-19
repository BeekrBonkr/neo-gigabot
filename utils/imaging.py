from __future__ import annotations

import asyncio
import functools
import io
import itertools
import logging
import random
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageEnhance, ImageFont, ImageSequence

from utils.discord_links import fetch_message_from_link
from utils.richtext import PLACEHOLDER_BASE, RichText

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
# A lot of what Discord shows as a GIF is a short silent video: everything from
# its GIF picker, and many uploads. These are turned into animated images on the way in.
VIDEO_EXTENSIONS = (".mp4", ".webm", ".mov", ".m4v")
SOURCE_TTL_SECONDS = 60 * 15

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


# Only so much of a video is kept: what a GIF made from it can reasonably hold.
VIDEO_MAX_EDGE = 480
VIDEO_MAX_FPS = 25
VIDEO_TIMEOUT_SECONDS = 30.0
VIDEO_CONTAINERS = {"mov": "mov,mp4,m4a,3gp,3g2,mj2", "matroska": "matroska,webm"}


def video_container(data: bytes) -> str | None:
    """Which kind of video file these bytes are, going by how they start, or
    None for anything else. Names and content types are not trusted."""
    if data[4:8] == b"ftyp":
        return "mov"
    if data[:4] == b"\x1a\x45\xdf\xa3":
        return "matroska"
    return None


def video_to_animation(data: bytes) -> bytes:
    """The start of a video as an animated WebP: at most MAX_GIF_FRAMES frames,
    no faster than VIDEO_MAX_FPS, no larger than VIDEO_MAX_EDGE. Everything
    downstream reads it the way it reads a GIF, and WebP is several times
    quicker to make than a GIF and keeps the video's colors."""
    container = video_container(data)
    ffmpeg = shutil.which("ffmpeg")
    if container is None or ffmpeg is None:
        raise ValueError("I can't read that video.")
    gap = 0.95 / VIDEO_MAX_FPS
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
                   "-i", source.name, "-an", "-sn", "-map", "0:v:0", "-vf", steps, "-fps_mode", "vfr",
                   "-frames:v", str(MAX_GIF_FRAMES), "-threads", "2", "-c:v", "libwebp_anim", "-q:v", "90",
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
# Every GIF frame is expanded to RGBA in memory during processing; anything past
# this many frames is dropped rather than letting one upload eat gigabytes.
MAX_GIF_FRAMES = 120
# Image work runs in executor threads; this many at once, bot-wide, each with
# this long to finish. GIFs multiply the cost by their frame count, so the
# timeout has headroom well past what a single frame needs.
MAX_CONCURRENT_CPU_BOUND = 2
CPU_BOUND_TIMEOUT_SECONDS = 60.0
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


def fit_within(image: Image.Image, max_edge: int) -> Image.Image:
    """The image shrunk so its longer edge is at most max_edge (never enlarged)."""
    longest = max(image.size)
    if longest <= max_edge:
        return image
    scale = max_edge / longest
    return image.resize((max(1, round(image.width * scale)), max(1, round(image.height * scale))), Image.LANCZOS)


def load_frames(image_bytes: bytes, filename: str | None = None, *, max_edge: int | None = None, first_only: bool = False) -> Frames:
    """Decode an upload into RGBA frames, capped at MAX_GIF_FRAMES. `max_edge`
    shrinks big inputs; `first_only` takes a GIF's first frame as a still."""
    with Image.open(io.BytesIO(image_bytes)) as image:
        animated = bool(getattr(image, "is_animated", False)) and not first_only
        loop = int(image.info.get("loop", 0) or 0)
        frames: list[Image.Image] = []
        durations: list[int] = []
        source = itertools.islice(ImageSequence.Iterator(image), MAX_GIF_FRAMES) if animated else [image]
        for frame in source:
            converted = frame.convert("RGBA")  # decoded first: some formats only know a frame's timing once it is read
            durations.append(int(frame.info.get("duration", 100) or 100))
            frames.append(fit_within(converted, max_edge) if max_edge else converted)
    return Frames(frames, durations, loop, animated and len(frames) > 1)


def encode_frames(frames: list[Image.Image], durations: list[int] | int, stem: str, *, loop: int = 0) -> tuple[bytes, str]:
    """PNG for one frame, GIF for several. Returns (data, filename)."""
    output = io.BytesIO()
    if len(frames) == 1:
        frames[0].save(output, format="PNG")
        return output.getvalue(), f"{stem}.png"
    if isinstance(durations, int):
        durations = [durations] * len(frames)
    frames = [frame.convert("RGBA") for frame in frames]
    frames[0].save(output, format="GIF", save_all=True, append_images=frames[1:], loop=loop,
                   duration=[max(MIN_GIF_FRAME_MS, int(d)) for d in durations], disposal=2)
    return output.getvalue(), f"{stem}.gif"


def apply_to_frames(image_bytes: bytes, filename: str | None, processor: Callable[[Image.Image], Image.Image], stem: str,
                    *, max_edge: int | None = None) -> tuple[bytes, str]:
    """Run processor over every frame of a still or GIF and encode the result."""
    loaded = load_frames(image_bytes, filename, max_edge=max_edge)
    return encode_frames([processor(frame) for frame in loaded.frames], loaded.durations, stem, loop=loaded.loop)


class ImageSourceStore:
    """Per-user 'selected image source' cache, set via the Set/Clear Image Source
    context menus. Shared across every image-command cog (not just cogs/images.py,
    which owns the context menu registration) so a source picked once is usable by
    any command that resolves an image, regardless of which cog defines it."""

    def __init__(self) -> None:
        self._sources: dict[int, dict[str, int | float]] = {}

    def _prune(self) -> None:
        now = time.time()
        stale = [user_id for user_id, data in self._sources.items() if now - float(data["timestamp"]) > SOURCE_TTL_SECONDS]
        for user_id in stale:
            self._sources.pop(user_id, None)

    def remember(self, user_id: int, channel_id: int, message_id: int) -> None:
        self._sources[user_id] = {
            "channel_id": channel_id,
            "message_id": message_id,
            "timestamp": time.time(),
        }

    def clear(self, user_id: int) -> bool:
        return self._sources.pop(user_id, None) is not None

    def get(self, user_id: int) -> dict[str, int | float] | None:
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
            future = loop.run_in_executor(None, func)
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
        limit = interaction.guild.filesize_limit if interaction.guild is not None else DEFAULT_UPLOAD_LIMIT_BYTES
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
        file = discord.File(io.BytesIO(data), filename=filename)
        embed = self.bot.embeds.info_embed(title, description or f"Generated `{filename}`.")
        embed.set_image(url=f"attachment://{filename}")
        await interaction.followup.send(embed=embed, file=file)

    def user_error_message(self, exc: Exception) -> str:
        """ValueError/RuntimeError raised inside the image pipeline carry
        deliberately user-facing text ("No recent image was found...", "That
        took too long..."). Anything else is a bug - log it and keep
        PIL/aiohttp internals out of the embed."""
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

        if interaction.guild is not None:
            channel = interaction.guild.get_channel(channel_id) or interaction.guild.get_thread(channel_id)
        else:
            channel = self.bot.get_channel(channel_id)

        if channel is None or not hasattr(channel, "fetch_message"):
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

        async for message in channel.history(limit=50):
            if not self.message_has_image(message):
                continue
            return await self.extract_image_from_message(message)

        raise ValueError("No recent image was found in the last 50 messages.")

    async def get_source_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> tuple[bytes, str]:
        """The image a command should work on. A video comes back as an animated WebP."""
        data, filename = await self._find_source_image(interaction, attachment, message_link)
        if video_container(data) is None:
            return data, filename
        return await self.run_cpu_bound(lambda: video_to_animation(data)), f"{Path(filename).stem}.webp"

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
        """Iterate a GIF's frames, capped at MAX_GIF_FRAMES. Every frame gets
        expanded to full RGBA in memory during processing, so all GIF loops in
        the image pipeline go through this cap - one upload must not be able to
        expand into gigabytes."""
        return itertools.islice(ImageSequence.Iterator(image), MAX_GIF_FRAMES)

    def gif_save_kwargs(self, image: Image.Image) -> dict:
        return {
            "save_all": True,
            "loop": image.info.get("loop", 0),
            "duration": image.info.get("duration", 100),
            "disposal": 2,
        }

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
                output = io.BytesIO()
                frames[0].save(output, format="GIF", append_images=frames[1:], **self.gif_save_kwargs(image))
                return output.getvalue(), gif_name

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
                resized_overlay = overlay_image.resize((new_width, new_height), Image.LANCZOS)
            else:
                if placement in {"top", "bottom"}:
                    new_width = frame.width
                    new_height = int(overlay_image.height * new_width / overlay_image.width)
                else:
                    new_height = frame.height
                    new_width = int(overlay_image.width * new_height / overlay_image.height)
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
