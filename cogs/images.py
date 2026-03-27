from __future__ import annotations

import io
import math
import random
from pathlib import Path
from typing import Callable

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageSequence

from utils.settings import command_is_blocked, get_guild_settings, is_bot_channel


class Images(commands.Cog):
    """Slash-command image manipulation and meme commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.assets_dir = self.bot.project_root / "assets" / "images"

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

    # ---------- image source helpers ----------

    async def _fetch_bytes_from_url(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": "neo-gigabot/1.0"}) as response:
                response.raise_for_status()
                return await response.read()

    async def _history_image_url(self, interaction: discord.Interaction) -> tuple[str | None, str | None]:
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            return None, None

        async for message in channel.history(limit=50):
            for attachment in message.attachments:
                content_type = attachment.content_type or ""
                name = attachment.filename.lower()
                if content_type.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")):
                    return attachment.url, attachment.filename

            for embed in message.embeds:
                if embed.image and embed.image.url:
                    return embed.image.url, "embedded_image"
                if embed.thumbnail and embed.thumbnail.url:
                    return embed.thumbnail.url, "embedded_thumbnail"

        return None, None

    async def _get_source_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
    ) -> tuple[bytes, str]:
        if attachment is not None:
            content_type = attachment.content_type or ""
            name = attachment.filename.lower()
            if not (content_type.startswith("image/") or name.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"))):
                raise ValueError("That attachment is not a supported image.")
            return await attachment.read(), attachment.filename

        url, filename = await self._history_image_url(interaction)
        if not url:
            raise ValueError("No recent image was found in the last 50 messages.")
        return await self._fetch_bytes_from_url(url), filename or "image"

    def _is_gif(self, image: Image.Image, filename: str | None = None) -> bool:
        fmt = (image.format or "").upper()
        if fmt == "GIF":
            return True
        if filename and filename.lower().endswith(".gif"):
            return True
        return getattr(image, "is_animated", False)

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

        def measure(fnt: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
            bbox = draw.textbbox((0, 0), text, font=fnt, stroke_width=3)
            return bbox[2] - bbox[0], bbox[3] - bbox[1]

        text_width, text_height = measure(font)
        while text_width > working.width - 20 and font_size > 12:
            font_size -= 2
            font = self._get_font(font_size)
            text_width, text_height = measure(font)

        x = (working.width - text_width) // 2
        if position == "top":
            y = 10
        elif position == "bottom":
            y = working.height - text_height - 20
        else:
            y = (working.height - text_height) // 2

        draw.text(
            (x, y),
            text,
            font=font,
            fill=(255, 255, 255, 255),
            stroke_width=3,
            stroke_fill=(0, 0, 0, 255),
        )
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
            converted.save(output, format="JPEG" if target_format == "jpg" else target_format.upper())
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

    # ---------- overlay / append helpers ----------

    def _asset_path(self, filename: str) -> Path:
        path = self.assets_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing asset: {filename}. Put the legacy overlay PNGs in `assets/images/`.")
        return path

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
            self.bot.embeds.field("Other", "`/deepfry` `/pfp`"),
        ]
        await self.bot.embeds.respond(interaction, title="Image Commands", fields=fields)

    @app_commands.command(name="extract", description="Re-upload the most recent image or GIF.")
    @app_commands.describe(attachment="Optional image attachment. Leave empty to use the most recent image in chat.")
    @app_commands.checks.cooldown(1, 5.0)
    async def extract(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "extract"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
            output, out_name = self._extract_bytes(image_bytes, filename)
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Extracted Image")
        except Exception as exc:
            await self._send_error(interaction, "Extract Failed", str(exc))

    @app_commands.command(name="jpegify", description="Crush image quality into cursed JPEG mush.")
    @app_commands.describe(attachment="Optional image attachment.", quality="JPEG quality from 1 to 30.")
    @app_commands.checks.cooldown(1, 5.0)
    async def jpegify(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        quality: app_commands.Range[int, 1, 30] = 1,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "jpegify"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(angle="Rotation angle in degrees.", attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def rotate(
        self,
        interaction: discord.Interaction,
        angle: float,
        attachment: discord.Attachment | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "rotate"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def stretch(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "stretch"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def pull(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "pull"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def squeeze(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "squeeze"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def squash(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "squash"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.", degrees="How strong the swirl should be.")
    @app_commands.checks.cooldown(1, 5.0)
    async def swirl(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        degrees: app_commands.Range[int, 45, 720] = 180,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "swirl"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.", speed="Frame duration in milliseconds.")
    @app_commands.checks.cooldown(1, 3.0)
    async def shake(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        speed: app_commands.Range[int, 10, 250] = 50,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "shake"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
            output, out_name = self._shake_bytes(image_bytes, filename, speed)
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title="Shaken")
        except Exception as exc:
            await self._send_error(interaction, "Shake Failed", str(exc))

    @app_commands.command(name="convert", description="Convert an image into another format.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        format="Target format: png, jpg, jpeg, bmp, gif, or webp.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def convert(
        self,
        interaction: discord.Interaction,
        format: str,
        attachment: discord.Attachment | None = None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "convert"):
            return

        try:
            image_bytes, _ = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.", sharpen_passes="How aggressive the deepfry should be.")
    @app_commands.checks.cooldown(1, 3.0)
    async def deepfry(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        sharpen_passes: app_commands.Range[int, 1, 20] = 12,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, "deepfry"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(text="The text to place.", attachment="Optional image attachment.", size="Requested font size.")
    @app_commands.checks.cooldown(1, 5.0)
    async def toptext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        size: app_commands.Range[int, 12, 120] = 50,
    ) -> None:
        await self._text_command(interaction, "toptext", text, "top", attachment, size)

    @app_commands.command(name="middletext", description="Add meme text to the middle of an image or GIF.")
    @app_commands.describe(text="The text to place.", attachment="Optional image attachment.", size="Requested font size.")
    @app_commands.checks.cooldown(1, 5.0)
    async def middletext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        size: app_commands.Range[int, 12, 120] = 50,
    ) -> None:
        await self._text_command(interaction, "middletext", text, "middle", attachment, size)

    @app_commands.command(name="bottomtext", description="Add meme text to the bottom of an image or GIF.")
    @app_commands.describe(text="The text to place.", attachment="Optional image attachment.", size="Requested font size.")
    @app_commands.checks.cooldown(1, 5.0)
    async def bottomtext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        size: app_commands.Range[int, 12, 120] = 50,
    ) -> None:
        await self._text_command(interaction, "bottomtext", text, "bottom", attachment, size)

    async def _text_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        text: str,
        position: str,
        attachment: discord.Attachment | None,
        size: int,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def flip(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "flip"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def flop(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        if not await self._ensure_image_command_allowed(interaction, "flop"):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def chimp(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._append_template_command(interaction, "chimp", "chimp.png", attachment)

    @app_commands.command(name="cooked", description="Append the cooked panel to the bottom of an image.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 3.0)
    async def cooked(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._append_template_command(interaction, "cooked", "cooked.png", attachment)

    async def _append_template_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        asset_name: str,
        attachment: discord.Attachment | None,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
            output, out_name = self._append_bytes(image_bytes, filename, self._asset_path(asset_name), placement="bottom")
            await self._defer(interaction)
            await self._send_processed_file(interaction, data=output, filename=out_name, title=command_name.title())
        except Exception as exc:
            await self._send_error(interaction, f"{command_name.title()} Failed", str(exc))

    @app_commands.command(name="doom", description="Overlay the classic doom UI.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def doom(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._overlay_template_command(interaction, "doom", "doomui.png", attachment, placement="bottom", strategy="fit")

    @app_commands.command(name="craftify", description="Overlay the crafty template.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def craftify(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._overlay_template_command(interaction, "craftify", "crafty.png", attachment, placement="bottom", strategy="fit")

    @app_commands.command(name="halflife", description="Overlay the Half-Life HUD template.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def halflife(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._overlay_template_command(interaction, "halflife", "hl2.png", attachment, placement="bottom", strategy="fit")

    @app_commands.command(name="murica", description="Stretch the patriotic overlay across the image.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def murica(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._overlay_template_command(interaction, "murica", "murica.png", attachment, placement="center", strategy="stretch")

    @app_commands.command(name="point", description="Overlay the soyjak pointing template.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def point(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._overlay_template_command(interaction, "point", "point.png", attachment, placement="center", strategy="stretch")

    @app_commands.command(name="northkorea", description="Overlay the authoritarian template.")
    @app_commands.describe(attachment="Optional image attachment.")
    @app_commands.checks.cooldown(1, 5.0)
    async def northkorea(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None) -> None:
        await self._overlay_template_command(interaction, "northkorea", "nk.png", attachment, placement="center", strategy="stretch")

    async def _overlay_template_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        asset_name: str,
        attachment: discord.Attachment | None,
        *,
        placement: str,
        strategy: str,
    ) -> None:
        if not await self._ensure_image_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self._get_source_image(interaction, attachment)
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
