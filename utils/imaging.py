from __future__ import annotations

import io
import itertools
import logging
import random
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageEnhance, ImageSequence

from utils.discord_links import fetch_message_from_link

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
SOURCE_TTL_SECONDS = 60 * 15

# Embed-image URLs are attacker-controlled (any user can post a link that
# unfurls to an arbitrary URL), so fetches are restricted to Discord's own CDN
# - same reasoning as the music cog's ALLOWED_URL_HOSTS. Attachments already
# live on these hosts, so nothing legitimate is lost.
ALLOWED_IMAGE_HOSTS: tuple[str, ...] = (
    "cdn.discordapp.com",
    "media.discordapp.net",
)
# Cap what we'll buffer into RAM, whether from a URL or an oversized attachment.
MAX_IMAGE_DOWNLOAD_BYTES = 25 * 1024 * 1024
# Every GIF frame is expanded to RGBA in memory during processing; anything past
# this many frames is dropped rather than letting one upload eat gigabytes.
MAX_GIF_FRAMES = 120

# The commands slow/heavy enough (DNN face detection, seam-carving DP, per-pixel
# swirl warping, ...) that someone firing off a bunch back-to-back is actually
# piling up real work, not just spamming a cheap resize. Tracked per (user,
# channel) so the snark below is aimed at whoever's actually causing the pile-up.
HEAVY_COMMANDS = frozenset(
    {"contentaware", "facebox", "blurfaces", "facecrop", "faceswap", "googlyeyes", "deepfry", "swirl", "shake"}
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

    # ---------- source selection helpers ----------

    def supported_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = attachment.content_type or ""
        name = attachment.filename.lower()
        return content_type.startswith("image/") or name.endswith(SUPPORTED_EXTENSIONS)

    def message_has_image(self, message: discord.Message) -> bool:
        if any(self.supported_attachment(attachment) for attachment in message.attachments):
            return True

        for embed in message.embeds:
            if (embed.image and embed.image.url) or (embed.thumbnail and embed.thumbnail.url):
                return True

        return False

    async def fetch_bytes_from_url(self, url: str) -> bytes:
        host = (urlparse(url).hostname or "").lower()
        if host not in ALLOWED_IMAGE_HOSTS:
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

        for embed in message.embeds:
            if embed.image and embed.image.url:
                return await self.fetch_bytes_from_url(embed.image.url), "embedded_image.png"
            if embed.thumbnail and embed.thumbnail.url:
                return await self.fetch_bytes_from_url(embed.thumbnail.url), "embedded_thumbnail.png"

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
