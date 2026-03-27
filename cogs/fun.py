from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands


class Fun(commands.Cog):
    """Fun and utility test commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="ping", description="Check the bot's gateway latency.")
    async def ping(self, interaction: discord.Interaction) -> None:
        latency_ms = round(self.bot.latency * 1000)
        await self.bot.embeds.info_interaction(
            interaction,
            "Pong",
            f"Gateway latency: `{latency_ms}ms`",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fun(bot))
