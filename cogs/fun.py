from __future__ import annotations

from discord.ext import commands


class Fun(commands.Cog):
    """Fun and utility commands migrated from the legacy bot."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        """Simple health check command."""
        await ctx.send(f"Pong. `{round(self.bot.latency * 1000)}ms`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
