from __future__ import annotations

from discord.ext import commands


class Fun(commands.Cog):
    """Fun and utility test commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="ping")
    async def ping(self, ctx: commands.Context) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await self.bot.embeds.info(
            ctx,
            "Pong",
            f"Gateway latency: `{latency_ms}ms`",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
