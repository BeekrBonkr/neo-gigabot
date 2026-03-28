from __future__ import annotations

import discord

from utils.settings import get_guild_settings, command_is_blocked, is_bot_channel


async def ensure_command_allowed(
    bot,
    interaction: discord.Interaction,
    command_name: str,
    *,
    allow_dm: bool = True,
    server_only_message: str = "This command can only be used in a server.",
) -> bool:
    if interaction.guild is None or interaction.channel is None:
        if allow_dm:
            return True
        await bot.embeds.error_interaction(
            interaction,
            "Server Only",
            server_only_message,
            ephemeral=True,
        )
        return False

    if command_is_blocked(bot.storage_path, interaction.guild.id, command_name):
        await bot.embeds.error_interaction(
            interaction,
            "Command Blocked",
            f"`/{command_name}` is blocked in this server.",
            ephemeral=True,
        )
        return False

    if is_bot_channel(bot.storage_path, interaction.guild.id, interaction.channel.id):
        return True

    settings = get_guild_settings(bot.storage_path, interaction.guild.id)
    bot_channels = settings.get("bot_channels", []) or []
    if bot_channels:
        mentions = ", ".join(f"<#{channel_id}>" for channel_id in bot_channels)
        description = f"This command can only be used in {mentions}."
    else:
        description = "This command is not allowed in this channel."

    await bot.embeds.warning_interaction(
        interaction,
        "Wrong Channel",
        description,
        ephemeral=True,
    )
    return False
