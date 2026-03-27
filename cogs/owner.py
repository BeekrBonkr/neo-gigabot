from __future__ import annotations

from discord.ext import commands


class Owner(commands.Cog):
    """Owner-only diagnostics and maintenance commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_check(self, ctx: commands.Context) -> bool:
        return ctx.author.id == self.bot.config.owner_id

    @commands.command(name="reload")
    async def reload_extension(self, ctx: commands.Context, extension: str) -> None:
        await self.bot.reload_extension(extension)
        await self.bot.embeds.success(
            ctx,
            "Reloaded Extension",
            f"Reloaded `{extension}` successfully.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
