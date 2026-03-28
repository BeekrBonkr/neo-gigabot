from __future__ import annotations

import io
import logging
import math
import random
from pathlib import Path
from typing import Callable

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageSequence

from utils.command_policy import ensure_command_allowed
from utils.image_rendering import ImageRenderer
from utils.image_sources import ImageSourceManager


LOGGER = logging.getLogger(__name__)

class Images(commands.Cog):
    """Slash-command image manipulation and quote commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.assets_dir = self.bot.project_root / "assets" / "images"
        self.sources = ImageSourceManager(bot)
        self.renderer = ImageRenderer(self.bot.project_root, self.assets_dir)

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
        return await ensure_command_allowed(
            self.bot,
            interaction,
            command_name,
            allow_dm=True,
        )

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

    # ---------- image context menu commands ----------

    async def set_image_source_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not await self._ensure_image_command_allowed(interaction, "imageinfo"):
            return

        if not self.sources.message_has_image(message):
            await self.bot.embeds.error_interaction(
                interaction,
                "No Image Found",
                "That message does not contain an image attachment or embedded image.",
                ephemeral=True,
            )
            return

        self.sources.remember_source(interaction.user.id, message.channel.id, message.id)
        await self._send_success(
            interaction,
            "Image Source Set",
            "That message is now your temporary image source for image commands.\n\n"
            "Priority order is: attachment, message link, selected message, then most recent image in the channel.",
        )

    async def clear_image_source_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not await self._ensure_image_command_allowed(interaction, "imageinfo"):
            return

        cleared = self.sources.clear_source(interaction.user.id)
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

        cleared = self.sources.clear_source(interaction.user.id)
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
            message = await self.sources.fetch_message_from_link(interaction, message_link)
            await self._defer(interaction)
            data, filename = await self.renderer.build_quote_image(message)
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.extract_bytes(image_bytes, filename)
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.jpegify_frame(frame, quality),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.resize_x(frame, 2.0),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.resize_y(frame, 2.0),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.resize_x(frame, 0.5),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.resize_y(frame, 0.5),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.swirl_frame(frame, float(degrees)),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.shake_bytes(image_bytes, filename, speed)
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
            image_bytes, _ = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.convert_bytes(image_bytes, format)
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.deepfry_frame(frame, sharpen_passes),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self.renderer.draw_meme_text(frame, text, position=position, requested_size=size),
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.process_image_bytes(
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.append_bytes(image_bytes, filename, self.renderer.asset_path(asset_name), placement="bottom")
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
            image_bytes, filename = await self.sources.get_source_image(interaction, attachment, message_link)
            output, out_name = self.renderer.overlay_bytes(
                image_bytes,
                filename,
                self.renderer.asset_path(asset_name),
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
