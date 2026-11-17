from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import image_filters as fx
from utils.imaging import apply_to_frames

SOURCE_HELP = {
    "attachment": "Optional image attachment.",
    "message_link": "Optional Discord message link containing the image.",
}
EDGE_STYLES = [
    app_commands.Choice(name="Pencil sketch", value="sketch"),
    app_commands.Choice(name="Neon outlines", value="neon"),
]


class ImageFilters(commands.GroupCog, group_name="filter", group_description="Color filters for images and GIFs."):
    """Color filters. Each works on a still or on every frame of a GIF, and
    finds its image the same way /image does: attachment, message link, the
    selected image source, then the latest image in the channel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _filter(self, interaction: discord.Interaction, name: str, title: str, processor, attachment, message_link) -> None:
        await self.bot.image_tools.run_image_command(
            interaction, name, title,
            lambda image_bytes, filename: apply_to_frames(image_bytes, filename, processor, name),
            attachment=attachment, message_link=message_link,
        )

    @app_commands.command(name="invert", description="Flip every color to its opposite.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def invert(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._filter(interaction, "invert", "Inverted", fx.invert, attachment, message_link)

    @app_commands.command(name="grayscale", description="Make an image black and white.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def grayscale(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._filter(interaction, "grayscale", "Grayscale", fx.grayscale, attachment, message_link)

    @app_commands.command(name="sepia", description="Give an image an old-photo brown tint.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def sepia(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._filter(interaction, "sepia", "Sepia", fx.sepia, attachment, message_link)

    @app_commands.command(name="hue", description="Shift every color around the color wheel.")
    @app_commands.describe(degrees="How far to turn the colors, in degrees.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def hue(
        self, interaction: discord.Interaction, degrees: app_commands.Range[int, 1, 359] = 180,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._filter(interaction, "hue", "Hue Shifted", lambda frame: fx.hue_shift(frame, degrees), attachment, message_link)

    @app_commands.command(name="adjust", description="Change brightness, contrast and saturation.")
    @app_commands.describe(
        brightness="Percent. 100 leaves it alone.", contrast="Percent. 100 leaves it alone.",
        saturation="Percent. 100 leaves it alone, 0 removes all color.", **SOURCE_HELP,
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def adjust(
        self, interaction: discord.Interaction,
        brightness: app_commands.Range[int, 0, 300] = 100, contrast: app_commands.Range[int, 0, 300] = 100,
        saturation: app_commands.Range[int, 0, 300] = 100,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        if brightness == contrast == saturation == 100:
            await self.bot.image_tools.send_error(interaction, "Nothing To Adjust", "Set brightness, contrast or saturation to something other than 100.")
            return
        await self._filter(
            interaction, "adjust", "Adjusted", lambda frame: fx.adjust(frame, brightness, contrast, saturation), attachment, message_link
        )

    @app_commands.command(name="posterize", description="Crush an image down to a few flat colors.")
    @app_commands.describe(levels="Shades kept per color channel. Fewer is harsher.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def posterize(
        self, interaction: discord.Interaction, levels: app_commands.Range[int, 2, 8] = 3,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._filter(interaction, "posterize", "Posterized", lambda frame: fx.posterize(frame, levels), attachment, message_link)

    @app_commands.command(name="edges", description="Reduce an image to its outlines.")
    @app_commands.describe(style="Pencil sketch on white, or bright outlines on black.", **SOURCE_HELP)
    @app_commands.choices(style=EDGE_STYLES)
    @app_commands.checks.cooldown(1, 3.0)
    async def edges(
        self, interaction: discord.Interaction, style: app_commands.Choice[str] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        chosen = style.value if style else "sketch"
        await self._filter(interaction, "edges", "Outlined", lambda frame: fx.edges(frame, chosen), attachment, message_link)

    @app_commands.command(name="emboss", description="Make an image look stamped into metal.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def emboss(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._filter(interaction, "emboss", "Embossed", fx.emboss, attachment, message_link)

    @app_commands.command(name="vaporwave", description="Pink and cyan tint with scanlines.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def vaporwave(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._filter(interaction, "vaporwave", "Vaporwave", fx.vaporwave, attachment, message_link)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageFilters(bot))
