from __future__ import annotations

import discord
from discord.ext import commands

from utils.checks import is_guild_context


class Moderation(commands.Cog):
    """Moderation commands and listeners."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.command(name="say")
    @commands.check(is_guild_context)
    @commands.has_permissions(manage_messages=True)
    async def say(self, ctx: commands.Context, *, message: str) -> None:
        embed = self.bot.embeds.create(
            title="Message",
            description=message,
            author_name=str(ctx.author),
            author_icon_url=ctx.author.display_avatar.url,
        )
        await self.bot.embeds.send(ctx, embed=embed)

    @say.error
    async def say_error(self, ctx: commands.Context, error: commands.CommandError) -> None:
        if isinstance(error, commands.MissingPermissions):
            await self.bot.embeds.error(
                ctx,
                "Missing Permission",
                "You need `Manage Messages` to use this command.",
            )
            return
        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
