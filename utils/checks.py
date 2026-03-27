from __future__ import annotations

import discord
from discord import app_commands


def guild_only() -> app_commands.Check:
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.guild is not None
    return app_commands.check(predicate)


def owner_only(owner_id: int):
    async def predicate(interaction: discord.Interaction) -> bool:
        return interaction.user.id == owner_id
    return app_commands.check(predicate)
