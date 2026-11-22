"""Commands that change sound: /audio."""

from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import audio_fx

CRUSH_SHAPES = [
    app_commands.Choice(name="Hard: even steps", value="lin"),
    app_commands.Choice(name="Soft: finer steps where it is quiet", value="log"),
]


class Audio(commands.GroupCog, group_name="audio", group_description="Change the sound of a video or an audio file."):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    @app_commands.command(name="bitcrush", description="Bit crush the sound of a video or an audio file. The picture is left as it is.")
    @app_commands.describe(
        bits="Bits left for each sample. Fewer is fizzier: 8 is an old console, 3 is rubble.",
        downsample="How many samples become one. More is more metallic and muffled.",
        mix="Percent of crushed sound against the original.",
        shape="How the steps are spaced. Soft keeps quiet sounds that hard steps would round down to silence.",
        drive="Decibels of boost going in, for more distortion. The result is kept from getting louder.",
        attachment="A video or an audio file.",
        message_link="Or a link to a message that has one.",
    )
    @app_commands.choices(shape=CRUSH_SHAPES)
    @app_commands.checks.cooldown(1, 8.0)
    async def bitcrush(
        self, interaction: discord.Interaction,
        bits: app_commands.Range[int, 2, 16] = 6, downsample: app_commands.Range[int, 1, 100] = 8,
        mix: app_commands.Range[int, 1, 100] = 100, shape: app_commands.Choice[str] | None = None,
        drive: app_commands.Range[int, 0, 30] = 0,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        crush = audio_fx.crush_filter(bits, downsample, shape.value if shape else "lin", mix=mix, drive=drive)

        def work(media: bytes, filename: str) -> tuple[bytes, str, str]:
            data, extension = audio_fx.apply_audio_filter(media, crush)
            return data, f"bitcrushed.{extension}", f"{bits} bits · every {downsample} samples become one · {mix}% crushed"

        async def media() -> tuple[bytes, str]:
            return await self.bot.image_tools.get_media_source(interaction, attachment, message_link)

        await self.bot.image_tools.run_image_command(interaction, "bitcrush", "Bit Crushed", work, source=media)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Audio(bot))
