from __future__ import annotations

import io
import math
import random
import re
import time
from pathlib import Path
from typing import Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageSequence

from utils.settings import command_is_blocked, get_guild_settings, is_bot_channel


SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
SOURCE_TTL_SECONDS = 60 * 15
MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>@me|\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)$"
)


class Images(commands.Cog):
    """Slash-command image manipulation and quote commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.assets_dir = self.bot.project_root / "assets" / "images"
        self._selected_sources: dict[int, dict[str, int | float]] = {}

        self.set_image_source_menu = app_commands.ContextMenu(
            name="Set Image Source",
            callback=self.set_image_source_context,
        )
        self.clear_image_source_menu = app_commands.ContextMenu(
            name="Clear Image Source",
            callback=self.clear_image_source_context,
        )

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.set_image_source_menu)
        self.bot.tree.add_command(self.clear_image_source_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.set_image_source_menu.name, type=self.set_image_source_menu.type)
        self.bot.tree.remove_command(self.clear_image_source_menu.name, type=self.clear_image_source_menu.type)

    # ---------- access / validation helpers ----------

    async def _ensure_image_command_allowed(
        self,
        interaction: discord.Interaction,
        command_name: str,
    ) -> bool:
        if interaction.guild is None or interaction.channel is None:
            return True

        if command_is_blocked(self.bot.storage_path, interaction.guild.id, command_name):
            await self.bot.embeds.error_interaction(
                interaction,
                "Command Blocked",
                f"`/{command_name}` is blocked in this server.",
                ephemeral=True,
            )
            return False

        settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        bot_channels = settings.get("bot_channels", []) or []
        if bot_channels and not is_bot_channel(
            self.bot.storage_path,
            interaction.guild.id,
            interaction.channel.id,
        ):
            await self.bot.embeds.warning_interaction(
                interaction,
                "Wrong Channel",
                "This command can only be used in a configured bot channel.",
                ephemeral=True,
            )
            return False

        return True

    async def _defer(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(thinking=True)

    async def _send_processed_file(
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

    async def _send_error(self, interaction: discord.Interaction, title: str, description: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=self.bot.embeds.error_embed(title, description), ephemeral=True)
        else:
            await self.bot.embeds.error_interaction(interaction, title, description, ephemeral=True)

    async def _send_success(self, interaction: discord.Interaction, title: str, description: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(embed=self.bot.embeds.success_embed(title, description), ephemeral=True)
        else:
            await self.bot.embeds.success_interaction(interaction, title, description, ephemeral=True)

    # ---------- source selection helpers ----------

    def _prune_sources(self) -> None:
        now = time.time()
        stale = [user_id for user_id, data in self._selected_sources.items() if now - float(data["timestamp"]) > SOURCE_TTL_SECONDS]
        for user_id in stale:
            self._selected_sources.pop(user_id, None)

    def _remember_source(self, user_id: int, channel_id: int, message_id: int) -> None:
        self._selected_sources[user_id] = {
            "channel_id": channel_id,
            "message_id": message_id,
            "timestamp": time.time(),
        }

    def _clear_source(self, user_id: int) -> bool:
        return self._selected_sources.pop(user_id, None) is not None

    def _supported_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = attachment.content_type or ""
        name = attachment.filename.lower()
        return content_type.startswith("image/") or name.endswith(SUPPORTED_EXTENSIONS)

    def _message_has_image(self, message: discord.Message) -> bool:
        if any(self._supported_attachment(attachment) for attachment in message.attachments):
            return True

        for embed in message.embeds:
            if (embed.image and embed.image.url) or (embed.thumbnail and embed.thumbnail.url):
                return True

        return False

    async def _fetch_bytes_from_url(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "neo-gigabot/1.0"}) as response:
                response.raise_for_status()
                return await response.read()

    def _parse_message_link(self, message_link: str) -> tuple[str, int, int]:
        match = MESSAGE_LINK_RE.match(message_link.strip())
        if not match:
            raise ValueError("That is not a valid Discord message link.")

        guild_id = match.group("guild_id")
        channel_id = int(match.group("channel_id"))
        message_id = int(match.group("message_id"))
        return guild_id, channel_id, message_id

    async def _get_channel_for_link(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        channel_id: int,
    ) -> discord.abc.Messageable:
        channel = None

        if guild_id == "@me":
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    raise ValueError("I could not access that DM channel.")
            return channel

        guild_id_int = int(guild_id)
        guild = interaction.guild if interaction.guild and interaction.guild.id == guild_id_int else self.bot.get_guild(guild_id_int)
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id_int)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                raise ValueError("I am not in that server or I cannot access it.")

        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                raise ValueError("I could not access the channel from that message link.")

        return channel

    async def _fetch_message_from_link(
        self,
        interaction: discord.Interaction,
        message_link: str,
    ) -> discord.Message:
        guild_id, channel_id, message_id = self._parse_message_link(message_link)
        channel = await self._get_channel_for_link(interaction, guild_id, channel_id)

        if not hasattr(channel, "fetch_message"):
            raise ValueError("That message link does not point to a fetchable text channel.")

        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            raise ValueError("I could not fetch that message. Make sure the link is valid and I can view that channel.")

    async def _extract_image_from_message(self, message: discord.Message) -> tuple[bytes, str]:
        for attachment in message.attachments:
            if self._supported_attachment(attachment):
                return await attachment.read(), attachment.filename

        for embed in message.embeds:
            if embed.image and embed.image.url:
                return await self._fetch_bytes_from_url(embed.image.url), "embedded_image.png"
            if embed.thumbnail and embed.thumbnail.url:
                return await self._fetch_bytes_from_url(embed.thumbnail.url), "embedded_thumbnail.png"

        raise ValueError("That message does not contain a supported image.")

    async def _selected_message_image(self, interaction: discord.Interaction) -> tuple[bytes, str] | None:
        self._prune_sources()
        source = self._selected_sources.get(interaction.user.id)
        if not source:
            return None

        channel_id = int(source["channel_id"])
        message_id = int(source["message_id"])

        if interaction.guild is not None:
            channel = interaction.guild.get_channel(channel_id) or interaction.guild.get_thread(channel_id)
        else:
            channel = self.bot.get_channel(channel_id)

        if channel is None or not hasattr(channel, "fetch_message"):
            self._selected_sources.pop(interaction.user.id, None)
            return None

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self._selected_sources.pop(interaction.user.id, None)
            return None

        return await self._extract_image_from_message(message)

    async def _history_image(self, interaction: discord.Interaction) -> tuple[bytes, str]:
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            raise ValueError("I could not inspect recent messages in this channel.")

        async for message in channel.history(limit=50):
            if not self._message_has_image(message):
                continue
            return await self._extract_image_from_message(message)

        raise ValueError("No recent image was found in the last 50 messages.")

    async def _get_source_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> tuple[bytes, str]:
        if attachment is not None:
            if not self._supported_attachment(attachment):
                raise ValueError("That attachment is not a supported image.")
            return await attachment.read(), attachment.filename

        if message_link:
            message = await self._fetch_message_from_link(interaction, message_link)
            return await self._extract_image_from_message(message)

        selected = await self._selected_message_image(interaction)
        if selected is not None:
            return selected

        return await self._history_image(interaction)

    # ---------- image context menu commands ----------

    async def set_image_source_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not await self._ensure_image_command_allowed(interaction, "imageinfo"):
            return

        if not self._message_has_image(message):
            await self.bot.embeds.error_interaction(
                interaction,
                "No Image Found",
                "That message does not contain an image attachment or embedded image.",
                ephemeral=True,
            )
            return

        self._remember_source(interaction.user.id, message.channel.id, message.id)
        await self._send_success(
            interaction,
            "Image Source Set",
            "That message is now your temporary image source for image commands.\n\n"
            "Priority order is: attachment, message link, selected message, then most recent image in the channel.",
        )

    async def clear_image_source_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not await self._ensure_image_command_allowed(interaction, "imageinfo"):
            return

        cleared = self._clear_source(interaction.user.id)
        if cleared:
            await self._send_success(
                interaction,
                "Image Source Cleared",
                "Your temporary image source has been cleared.",
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nothing To Clear",
                "You do not currently have a temporary image source selected.",
                ephemeral=True,
            )

    @app_commands.command(name="clearimagesource", description="Clear your selected image source.")
    async def clear_image_source(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_image_command_allowed(interaction, "imageinfo"):
            return

        cleared = self._clear_source(interaction.user.id)
        if cleared:
            await self._send_success(
                interaction,
                "Image Source Cleared",
                "Your temporary image source has been cleared.",
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nothing To Clear",
                "You do not currently have a temporary image source selected.",
                ephemeral=True,
            )

    # ---------- image format helpers ----------

    def _is_gif(self, image: Image.Image, filename: str | None = None) -> bool:
        fmt = (image.format or "").upper()
        if fmt == "GIF":
            return True
        if filename and filename.lower().endswith(".gif"):
            return True
        return bool(getattr(image, "is_animated", False))

    def _gif_save_kwargs(self, image: Image.Image) -> dict:
        return {
            "save_all": True,
            "loop": image.info.get("loop", 0),
            "duration": image.info.get("duration", 100),
            "disposal": 2,
        }

    # ---------- font / text helpers ----------

    def _get_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            str(self.bot.project_root / "assets" / "fonts" / "Impact.ttf"),
            str(self.bot.project_root / "assets" / "fonts" / "impact.ttf"),
            str(self.bot.project_root / "assets" / "fonts" / "arial.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ]
        for path in candidates:
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, size=size)
                except OSError:
                    pass
        return ImageFont.load_default()

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            words = paragraph.split()
            if not words:
                lines.append("")
                continue

            current = words[0]
            for word in words[1:]:
                test = f"{current} {word}"
                bbox = draw.textbbox((0, 0), test, font=font, stroke_width=3)
                if bbox[2] - bbox[0] <= max_width:
                    current = test
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    def _draw_meme_text(
        self,
        image: Image.Image,
        text: str,
        *,
        position: str,
        requested_size: int = 50,
    ) -> Image.Image:
        working = image.convert("RGBA")
        draw = ImageDraw.Draw(working)

        font_size = max(12, requested_size)
        font = self._get_font(font_size)
        wrapped = self._wrap_text(draw, text, font, working.width - 20)

        def measure(lines: list[str], fnt: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
            widths = []
            total_height = 0
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=fnt, stroke_width=3)
                widths.append(bbox[2] - bbox[0])
                total_height += bbox[3] - bbox[1] + 6
            return (max(widths) if widths else 0), max(0, total_height - 6)

        text_width, text_height = measure(wrapped, font)
        while (text_width > working.width - 20 or text_height > working.height - 20) and font_size > 12:
            font_size -= 2
            font = self._get_font(font_size)
            wrapped = self._wrap_text(draw, text, font, working.width - 20)
            text_width, text_height = measure(wrapped, font)

        x = (working.width - text_width) // 2
        if position == "top":
            y = 10
        elif position == "bottom":
            y = working.height - text_height - 20
        else:
            y = (working.height - text_height) // 2

        for line in wrapped:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_x = (working.width - line_width) // 2
            draw.text(
                (line_x, y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=3,
                stroke_fill=(0, 0, 0, 255),
            )
            y += line_height + 6
        return working

    # ---------- generic image processing helpers ----------

    def _process_image_bytes(
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
            if self._is_gif(image, filename):
                frames: list[Image.Image] = []
                for frame in ImageSequence.Iterator(image):
                    processed = processor(frame.convert("RGBA"))
                    frames.append(processed.convert("RGBA"))
                output = io.BytesIO()
                frames[0].save(output, format="GIF", append_images=frames[1:], **self._gif_save_kwargs(image))
                return output.getvalue(), gif_name

            processed = processor(image.convert("RGBA"))
            output = io.BytesIO()
            save_image = processed
            if static_format.upper() in {"JPEG", "JPG", "BMP"}:
                save_image = processed.convert("RGB")
            save_image.save(output, format=static_format.upper())
            return output.getvalue(), static_name

    def _resize_x(self, frame: Image.Image, factor: float) -> Image.Image:
        new_width = max(1, min(4000, int(frame.width * factor)))
        return frame.resize((new_width, frame.height), Image.LANCZOS)

    def _resize_y(self, frame: Image.Image, factor: float) -> Image.Image:
        new_height = max(1, min(4000, int(frame.height * factor)))
        return frame.resize((frame.width, new_height), Image.LANCZOS)

    def _jpegify_frame(self, frame: Image.Image, quality: int) -> Image.Image:
        original_size = frame.size
        rgb = frame.convert("RGB")
        temp = io.BytesIO()
        rgb.save(temp, format="JPEG", quality=max(1, min(quality, 95)))
        temp.seek(0)
        low_quality = Image.open(temp).convert("RGB")
        return low_quality.resize(original_size, Image.LANCZOS)

    def _deepfry_frame(self, frame: Image.Image, sharpen_passes: int) -> Image.Image:
        rgb = frame.convert("RGB")
        rgb = ImageEnhance.Color(rgb).enhance(2.0)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.4)
        rgb = ImageEnhance.Sharpness(rgb).enhance(2.5)
        for _ in range(max(1, sharpen_passes)):
            rgb = rgb.filter(ImageFilter.SHARPEN)
        temp = io.BytesIO()
        rgb.save(temp, format="JPEG", quality=10)
        temp.seek(0)
        return Image.open(temp).convert("RGB")

    def _swirl_frame(self, frame: Image.Image, degrees: float = 180.0) -> Image.Image:
        src = frame.convert("RGBA")
        width, height = src.size
        cx = width / 2.0
        cy = height / 2.0
        max_radius = math.hypot(cx, cy)
        src_px = src.load()
        out = Image.new("RGBA", src.size)
        out_px = out.load()

        strength = math.radians(degrees)
        for y in range(height):
            dy = y - cy
            for x in range(width):
                dx = x - cx
                radius = math.hypot(dx, dy)
                if radius == 0 or radius > max_radius:
                    sx, sy = x, y
                else:
                    theta = math.atan2(dy, dx)
                    twist = strength * (max_radius - radius) / max_radius
                    source_theta = theta - twist
                    sx = int(round(cx + radius * math.cos(source_theta)))
                    sy = int(round(cy + radius * math.sin(source_theta)))

                if 0 <= sx < width and 0 <= sy < height:
                    out_px[x, y] = src_px[sx, sy]
                else:
                    out_px[x, y] = (0, 0, 0, 0)
        return out

    def _shake_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        speed: int,
        frame_count: int = 10,
    ) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            base = next(ImageSequence.Iterator(image)).convert("RGBA") if self._is_gif(image, filename) else image.convert("RGBA")
            frames: list[Image.Image] = []
            for _ in range(frame_count):
                x_shift = random.randint(-10, 10)
                y_shift = random.randint(-10, 10)
                shifted = Image.new("RGBA", base.size, (0, 0, 0, 0))
                shifted.paste(base, (x_shift, y_shift), base)
                frames.append(shifted)
            output = io.BytesIO()
            frames[0].save(output, format="GIF", append_images=frames[1:], save_all=True, duration=max(10, speed), loop=0, disposal=2)
            return output.getvalue(), "shaky.gif"

    def _convert_bytes(
        self,
        image_bytes: bytes,
        target_format: str,
    ) -> tuple[bytes, str]:
        target_format = target_format.lower()
        allowed_types = {"jpg", "jpeg", "png", "bmp", "gif", "webp"}
        if target_format not in allowed_types:
            raise ValueError(f"Invalid format. Allowed formats: {', '.join(sorted(allowed_types))}")

        with Image.open(io.BytesIO(image_bytes)) as image:
            output = io.BytesIO()
            if target_format == "gif":
                frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(image)] if getattr(image, "is_animated", False) else [image.convert("RGBA")]
                frames[0].save(
                    output,
                    format="GIF",
                    append_images=frames[1:],
                    save_all=True,
                    loop=image.info.get("loop", 0),
                    duration=image.info.get("duration", 100),
                    disposal=2,
                )
                return output.getvalue(), "converted.gif"

            converted = image.convert("RGB") if target_format in {"jpg", "jpeg", "bmp"} else image.convert("RGBA")
            converted.save(output, format="JPEG" if target_format in {"jpg", "jpeg"} else target_format.upper())
            ext = "jpg" if target_format == "jpeg" else target_format
            return output.getvalue(), f"converted.{ext}"

    def _extract_bytes(self, image_bytes: bytes, filename: str) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            output = io.BytesIO()
            if self._is_gif(image, filename):
                frames = [frame.copy().convert("RGBA") for frame in ImageSequence.Iterator(image)]
                frames[0].save(output, format="GIF", append_images=frames[1:], **self._gif_save_kwargs(image))
                return output.getvalue(), "extracted.gif"

            image.save(output, format=image.format or "PNG")
            ext = (image.format or "PNG").lower()
            return output.getvalue(), f"extracted.{ext}"

    # ---------- quote helpers ----------

    def _asset_path(self, filename: str) -> Path:
        path = self.assets_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing asset: {filename}. Put the legacy overlay PNGs in `assets/images/`.")
        return path

    async def _build_quote_image(self, message: discord.Message) -> tuple[bytes, str]:
        content = (message.content or "").strip()
        if not content:
            raise ValueError("That message does not have any text to quote.")

        avatar = message.author.display_avatar.replace(size=512)
        avatar_bytes = await avatar.read()

        with Image.open(io.BytesIO(avatar_bytes)) as avatar_image:
            profile_pic = avatar_image.convert("RGBA").resize((400, 400), Image.LANCZOS)

        vig = None
        vig_path = self.assets_dir / "vig.png"
        if vig_path.exists():
            with Image.open(vig_path) as vig_image:
                vig = vig_image.convert("RGBA").resize((400, 400), Image.LANCZOS)

        if vig is not None:
            img = Image.alpha_composite(profile_pic, vig)
        else:
            img = profile_pic

        img = img.convert("RGB").filter(ImageFilter.GaussianBlur(radius=5)).convert("RGBA")

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 110))
        img = Image.alpha_composite(img, overlay)

        draw = ImageDraw.Draw(img)
        font = self._get_font(23)
        text = f'"{content}"\n\n- {message.author.display_name}'
        max_width = int(img.width * 0.9)
        lines = self._wrap_text(draw, text, font, max_width)

        while len(lines) > 11:
            shorter = content[: max(20, len(content) - 10)].rstrip() + "..."
            text = f'"{shorter}"\n\n- {message.author.display_name}'
            lines = self._wrap_text(draw, text, font, max_width)

        line_metrics = []
        total_height = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_metrics.append((line, line_width, line_height))
            total_height += line_height + 6
        total_height = max(0, total_height - 6)

        y_text = img.height // 2 - total_height // 2
        shadow_offset = 2

        for line, line_width, line_height in line_metrics:
            x_text = img.width // 2 - line_width // 2
            for shadow_x, shadow_y in [
                (x_text - shadow_offset, y_text - shadow_offset),
                (x_text + shadow_offset, y_text + shadow_offset),
                (x_text + shadow_offset, y_text - shadow_offset),
                (x_text - shadow_offset, y_text + shadow_offset),
            ]:
                draw.text((shadow_x, shadow_y), line, font=font, fill=(0, 0, 0, 255))
            draw.text((x_text, y_text), line, font=font, fill=(255, 255, 255, 255))
            y_text += line_height + 6

        output = io.BytesIO()
        img.save(output, "PNG")
        return output.getvalue(), "quote.png"

    # ---------- overlay / append helpers ----------

    def _overlay_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        overlay_image_path: Path,
        *,
        placement: str = "bottom",
        strategy: str = "fit",
        opacity: float = 1.0,
    ) -> tuple[bytes, str]:
        overlay_image = Image.open(overlay_image_path).convert("RGBA")

        def overlay_to_frame(input_frame: Image.Image) -> Image.Image:
            frame = input_frame.convert("RGBA")
            if strategy == "stretch":
                resized_overlay = overlay_image.resize(frame.size, Image.LANCZOS)
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
                "top": (0, 0),
                "bottom": (0, frame.height - resized_overlay.height),
                "left": (0, 0),
                "right": (frame.width - resized_overlay.width, 0),
                "center": ((frame.width - resized_overlay.width) // 2, (frame.height - resized_overlay.height) // 2),
            }
            new_frame = frame.copy()
            new_frame.paste(resized_overlay, paste_coords[placement], resized_overlay)
            return new_frame

        return self._process_image_bytes(
            image_bytes,
            filename,
            overlay_to_frame,
            static_format="PNG",
            static_name=f"{overlay_image_path.stem}.png",
            gif_name=f"{overlay_image_path.stem}.gif",
        )

    def _append_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        append_image_path: Path,
        *,
        placement: str = "bottom",
    ) -> tuple[bytes, str]:
        append_image = Image.open(append_image_path).convert("RGBA")

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

        return self._process_image_bytes(
            image_bytes,
            filename,
            add_to_frame,
            static_format="PNG",
            static_name=f"{append_image_path.stem}.png",
            gif_name=f"{append_image_path.stem}.gif",
        )

    # ---------- slash commands ----------

    @app_commands.command(name="imageinfo", description="Show what the image cog can do.")
    async def imageinfo(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_image_command_allowed(interaction, "imageinfo"):
            return

        fields = [
            self.bot.embeds.field("Transforms", "`/rotate` `/stretch` `/pull` `/squeeze` `/squash` `/swirl` `/shake` `/flip` `/flop`"),
            self.bot.embeds.field("Formats", "`/jpegify` `/convert` `/extract`"),
            self.bot.embeds.field("Meme Text", "`/toptext` `/middletext` `/bottomtext`"),
            self.bot.embeds.field("Template Overlays", "`/chimp` `/cooked` `/doom` `/craftify` `/halflife` `/murica` `/point` `/northkorea`"),
            self.bot.embeds.field("Source Priority", "Attachment → message link → selected source → latest image in channel"),
            self.bot.embeds.field("Quote", "`/quote <message_link>` only uses a Discord message link."),
            self.bot.embeds.field("Other", "`/deepfry` `/pfp` `/clearimagesource`"),
        ]
        await self.bot.embeds.respond(interaction, title="Image Commands", fields=fields)

    @app_commands.command(name="quote", description="Create a quote image from a Discord message link.")
    @app_commands.describe(message_link="Paste a Discord message link.")
    @app_commands.checks.cooldown(1, 5.0)
    async def quote(self, interaction: discord.Interaction, message_link: str) -> None:
        if not await self._ensure_image_command_allowed(interaction, "quote"):
            return

        try:
            message = await self._fetch_message_from_link(interaction, message_link)
            await self._defer(interaction)
            data, filename = await self._build_quote_image(message)
            await self._send_processed_file(
                interaction,
                data=data,
                filename=filename,
                title="Quote",
                description=f"Quoted message from {message.author.mention}.",
            )
        except Exception as exc:
            await self._send_error(interaction, "Quote Failed", str(exc))

    @app_commands.command(name="extract", description="Re-upload the selected image or GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def extract(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "extract"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._extract_bytes(image_bytes, filename)
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Extracted Image")
        except Exception as exc:
            await self._send_error(interaction, "Extract Failed", str(exc))

    @app_commands.command(name="jpegify", description="Crush image quality into cursed JPEG mush.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        quality="JPEG quality from 1 to 30.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def jpegify(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        quality: app_commands.Range[int, 1, 30] = 1,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "jpegify"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._jpegify_frame(frame, quality),
                static_format="PNG",
                static_name="jpegified.png",
                gif_name="jpegified.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="JPEGified")
        except Exception as exc:
            await self._send_error(interaction, "JPEGify Failed", str(exc))

    @app_commands.command(name="rotate", description="Rotate an image or GIF.")
    @app_commands.describe(
        angle="Rotation angle in degrees.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def rotate(
        self,
        interaction: discord.Interaction,
        angle: float,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "rotate"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: frame.rotate(angle, resample=Image.BICUBIC, expand=True),
                static_format="PNG",
                static_name="rotated.png",
                gif_name="rotated.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Rotated")
        except Exception as exc:
            await self._send_error(interaction, "Rotate Failed", str(exc))

    @app_commands.command(name="stretch", description="Double an image's width.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def stretch(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "stretch"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_x(frame, 2.0),
                static_format="PNG",
                static_name="stretched.png",
                gif_name="stretched.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Stretched")
        except Exception as exc:
            await self._send_error(interaction, "Stretch Failed", str(exc))

    @app_commands.command(name="pull", description="Double an image's height.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def pull(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "pull"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_y(frame, 2.0),
                static_format="PNG",
                static_name="pulled.png",
                gif_name="pulled.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Pulled")
        except Exception as exc:
            await self._send_error(interaction, "Pull Failed", str(exc))

    @app_commands.command(name="squeeze", description="Halve an image's width.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def squeeze(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "squeeze"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_x(frame, 0.5),
                static_format="PNG",
                static_name="squeezed.png",
                gif_name="squeezed.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Squeezed")
        except Exception as exc:
            await self._send_error(interaction, "Squeeze Failed", str(exc))

    @app_commands.command(name="squash", description="Halve an image's height.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def squash(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "squash"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_y(frame, 0.5),
                static_format="PNG",
                static_name="squashed.png",
                gif_name="squashed.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Squashed")
        except Exception as exc:
            await self._send_error(interaction, "Squash Failed", str(exc))

    @app_commands.command(name="swirl", description="Apply a swirl effect to an image or GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        degrees="How strong the swirl should be.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def swirl(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        degrees: app_commands.Range[int, 45, 720] = 180,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "swirl"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._swirl_frame(frame, float(degrees)),
                static_format="PNG",
                static_name="swirl.png",
                gif_name="swirl.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Swirled")
        except Exception as exc:
            await self._send_error(interaction, "Swirl Failed", str(exc))

    @app_commands.command(name="shake", description="Turn an image into a shaky GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        speed="Frame duration in milliseconds.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def shake(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        speed: app_commands.Range[int, 10, 250] = 50,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "shake"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._shake_bytes(image_bytes, filename, speed)
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Shaken")
        except Exception as exc:
            await self._send_error(interaction, "Shake Failed", str(exc))

    @app_commands.command(name="convert", description="Convert an image into another format.")
    @app_commands.describe(
        format="Target format: png, jpg, jpeg, bmp, gif, or webp.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def convert(
        self,
        interaction: discord.Interaction,
        format: str,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "convert"):
            return

        try:
            image_bytes, _ = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._convert_bytes(image_bytes, format)
            await self._defer(interaction)
            await self._send_processed_file(
                interaction,
                data=output,
                filename=out_name,
                title="Converted",
                description=f"Converted to `{format.lower()}`.",
            )
        except Exception as exc:
            await self._send_error(interaction, "Convert Failed", str(exc))

    @app_commands.command(name="deepfry", description="Deepfry an image or GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        sharpen_passes="How aggressive the deepfry should be.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def deepfry(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        sharpen_passes: app_commands.Range[int, 1, 20] = 12,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "deepfry"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._deepfry_frame(frame, sharpen_passes),
                static_format="JPEG",
                static_name="deepfried.jpg",
                gif_name="deepfried.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Deepfried")
        except Exception as exc:
            await self._send_error(interaction, "Deepfry Failed", str(exc))

    @app_commands.command(name="toptext", description="Add meme text to the top of an image or GIF.")
    @app_commands.describe(
        text="The text to place.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        size="Requested font size.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def toptext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        size: app_commands.Range[int, 12, 120] = 50,
    ) -> None:
        await self._text_command(interaction, "toptext", text, "top", attachment, message_link, size)

    @app_commands.command(name="middletext", description="Add meme text to the middle of an image or GIF.")
    @app_commands.describe(
        text="The text to place.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        size="Requested font size.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def middletext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        size: app_commands.Range[int, 12, 120] = 50,
    ) -> None:
        await self._text_command(interaction, "middletext", text, "middle", attachment, message_link, size)

    @app_commands.command(name="bottomtext", description="Add meme text to the bottom of an image or GIF.")
    @app_commands.describe(
        text="The text to place.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        size="Requested font size.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def bottomtext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        size: app_commands.Range[int, 12, 120] = 50,
    ) -> None:
        await self._text_command(interaction, "bottomtext", text, "bottom", attachment, message_link, size)

    async def _text_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        text: str,
        position: str,
        attachment: discord.Attachment | None,
        message_link: str | None,
        size: int,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._draw_meme_text(frame, text, position=position, requested_size=size),
                static_format="PNG",
                static_name=f"{position}text.png",
                gif_name=f"{position}text.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title=f"{position.title()} Text Added")
        except Exception as exc:
            await self._send_error(interaction, "Text Failed", str(exc))

    @app_commands.command(name="flip", description="Flip an image vertically.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def flip(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "flip"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: ImageOps.flip(frame),
                static_format="JPEG",
                static_name="flipped.jpg",
                gif_name="flipped.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Flipped")
        except Exception as exc:
            await self._send_error(interaction, "Flip Failed", str(exc))

    @app_commands.command(name="flop", description="Flip an image horizontally.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def flop(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "flop"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._process_image_bytes(
                image_bytes,
                filename,
                lambda frame: ImageOps.mirror(frame),
                static_format="JPEG",
                static_name="flopped.jpg",
                gif_name="flopped.gif",
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Flopped")
        except Exception as exc:
            await self._send_error(interaction, "Flop Failed", str(exc))

    @app_commands.command(name="pfp", description="Show a user's profile picture as a file.")
    @app_commands.describe(user="The user whose avatar you want.")
    @app_commands.checks.cooldown(1, 5.0)
    async def pfp(self, interaction: discord.Interaction, user: discord.User | discord.Member) -> None:
        if not await self._ensure_image_command_allowed(interaction, "pfp"):
            return

        try:
            await self._defer(interaction)
            avatar = user.display_avatar.replace(size=1024)
            avatar_bytes = await avatar.read()
            filename = f"{user.name}.gif" if avatar.url.lower().endswith(".gif") else f"{user.name}.png"
            file = discord.File(io.BytesIO(avatar_bytes), filename=filename)
            embed = self.bot.embeds.info_embed("Profile Picture", f"{user.mention}'s avatar.")
            embed.set_image(url=f"attachment://{filename}")
            await interaction.followup.send(embed=embed, file=file)
        except Exception as exc:
            await self._send_error(interaction, "Avatar Failed", str(exc))

    @app_commands.command(name="chimp", description="Append the chimp panel to the bottom of an image.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def chimp(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._append_template_command(interaction, "chimp", "chimp.png", attachment, message_link)

    @app_commands.command(name="cooked", description="Append the cooked panel to the bottom of an image.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def cooked(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._append_template_command(interaction, "cooked", "cooked.png", attachment, message_link)

    async def _append_template_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        asset_name: str,
        attachment: discord.Attachment | None,
        message_link: str | None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._append_bytes(image_bytes, filename, self._asset_path(asset_name), placement="bottom")
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title=command_name.title())
        except Exception as exc:
            await self._send_error(interaction, f"{command_name.title()} Failed", str(exc))

    @app_commands.command(name="doom", description="Overlay the classic doom UI.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def doom(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "doom", "doomui.png", attachment, message_link, placement="bottom", strategy="fit")

    @app_commands.command(name="craftify", description="Overlay the crafty template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def craftify(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "craftify", "crafty.png", attachment, message_link, placement="bottom", strategy="fit")

    @app_commands.command(name="halflife", description="Overlay the Half-Life HUD template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def halflife(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "halflife", "hl2.png", attachment, message_link, placement="bottom", strategy="fit")

    @app_commands.command(name="murica", description="Stretch the patriotic overlay across the image.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def murica(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "murica", "murica.png", attachment, message_link, placement="center", strategy="stretch")

    @app_commands.command(name="point", description="Overlay the soyjak pointing template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def point(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "point", "point.png", attachment, message_link, placement="center", strategy="stretch")

    @app_commands.command(name="northkorea", description="Overlay the authoritarian template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def northkorea(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "northkorea", "nk.png", attachment, message_link, placement="center", strategy="stretch")

    async def _overlay_template_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        asset_name: str,
        attachment: discord.Attachment | None,
        message_link: str | None,
        *,
        placement: str,
        strategy: str,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment, message_link)
            output, out_name = self._overlay_bytes(
                image_bytes,
                filename,
                self._asset_path(asset_name),
                placement=placement,
                strategy=strategy,
                opacity=1.0,
            )
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title=command_name.title())
        except Exception as exc:
            await self._send_error(interaction, f"{command_name.title()} Failed", str(exc))

    @imageinfo.error
    @quote.error
    @clear_image_source.error
    @extract.error
    @jpegify.error
    @rotate.error
    @stretch.error
    @pull.error
    @squeeze.error
    @squash.error
    @swirl.error
    @shake.error
    @convert.error
    @deepfry.error
    @toptext.error
    @middletext.error
    @bottomtext.error
    @flip.error
    @flop.error
    @pfp.error
    @chimp.error
    @cooked.error
    @doom.error
    @craftify.error
    @halflife.error
    @murica.error
    @point.error
    @northkorea.error
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self._send_error(
                interaction,
                "Slow Down",
                f"Try again in `{error.retry_after:.1f}` seconds.",
            )
            return

        if isinstance(error, app_commands.TransformerError):
            await self._send_error(
                interaction,
                "Invalid Value",
                "One of the command options you entered was invalid.",
            )
            return

        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Images(bot))
