from __future__ import annotations

from discord.ext import commands


async def is_guild_context(ctx: commands.Context) -> bool:
    return ctx.guild is not None
