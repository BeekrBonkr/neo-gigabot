from __future__ import annotations

from discord.ext import commands


class Images(commands.Cog):
    """Image manipulation and meme commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="imageinfo")
    async def image_info(self, ctx: commands.Context) -> None:
        await self.bot.embeds.info(
            ctx,
            "Images Cog",
            "Image cog is loaded. Legacy image commands can be migrated here.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Images(bot))
