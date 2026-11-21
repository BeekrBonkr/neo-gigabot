"""The bot as a personal app.

Discord lets a person add an app to their own account and use its commands in
servers that never added it, and in DMs. This is the one place that says which
commands that covers, and that checks each such use.

Discord sets where a command can be installed on the whole top-level command,
so a group is opened as one piece. A command inside an opened group that is not
meant for personal use (`/music play`, say) still shows up for people who
installed the bot, and is turned away here when it is run.
"""

from __future__ import annotations

from typing import Any

import discord
from discord import app_commands

# Top-level slash commands that work as a personal app.
COMMANDS = frozenset({
    "ping", "coinflip", "roll", "ascii", "8ball", "catfact", "fact", "joke", "trivia", "wiki", "weather",
    "gif", "cute", "beans", "meme", "hug", "tictactoe", "simon", "about", "feedback",
})
# Groups where every command works, apart from the ones in SERVER_ONLY.
GROUPS = frozenset({"image", "filter", "edit", "animate", "memegen"})
SERVER_ONLY = frozenset({"image text quote", "image misc clearimagesource"})  # these two are right-click menus instead
# Groups opened for a few of their commands only: /music for a person's own playlists.
PARTIAL_GROUPS = {
    "music": frozenset({
        "music playlist create", "music playlist delete", "music playlist rename", "music playlist add",
        "music playlist removesong", "music playlist shuffle", "music playlist description", "music playlist status",
        "music playlist view", "music playlist profile", "music playlist like", "music playlist unlike", "music playlist liked",
    }),
}
# Right-click menus.
MENUS = frozenset({"Edit Image", "Quote Message", "Set Image Source", "Clear Image Source"})

INSTALLS = app_commands.AppInstallationType(guild=True, user=True)
CONTEXTS = app_commands.AppCommandContext(guild=True, dm_channel=True, private_channel=True)

# Where a command was run.
SERVER = "server"  # a server that has added the bot
BOT_DM = "dm"  # the bot's own DMs
ELSEWHERE = "elsewhere"  # a server without the bot, a DM between people, a group DM


def works_as_user_app(qualified_name: str, *, menu: bool = False) -> bool:
    """Whether a command may run where the bot is not a member."""
    if menu:
        return qualified_name in MENUS
    root = qualified_name.split(" ", 1)[0]
    if root in PARTIAL_GROUPS:
        return qualified_name in PARTIAL_GROUPS[root]
    if root in GROUPS:
        return qualified_name not in SERVER_ONLY
    return qualified_name in COMMANDS


def mark_tree(tree: app_commands.CommandTree) -> None:
    """Open the personal-app commands to user installs, ahead of a sync."""
    opened = COMMANDS | GROUPS | set(PARTIAL_GROUPS)
    for command in tree.get_commands():
        if command.name in opened:
            command.allowed_installs, command.allowed_contexts = INSTALLS, CONTEXTS
    for kind in (discord.AppCommandType.message, discord.AppCommandType.user):
        for menu in tree.get_commands(type=kind):
            if menu.name in MENUS:
                menu.allowed_installs, menu.allowed_contexts = INSTALLS, CONTEXTS


def placement(interaction: discord.Interaction) -> str:
    if interaction.guild_id is not None:
        return SERVER if interaction.is_guild_integration() else ELSEWHERE
    return ELSEWHERE if interaction.context.private_channel else BOT_DM


def home_guild(interaction: discord.Interaction) -> discord.Guild | None:
    """The server the command was run in, if the bot is in it. In a server
    that has not added the bot, `interaction.guild` is still filled in with a
    stand-in that has no members, channels or settings: anything that reads a
    server's settings should ask for this instead."""
    guild = interaction.guild
    if guild is None or not interaction.is_guild_integration():
        return None
    return guild


async def shares_server(bot: Any, user: discord.abc.User) -> bool:
    """Whether the person is in at least one server the bot is in."""
    if any(guild.get_member(user.id) is not None for guild in bot.guilds):
        return True
    for guild in bot.guilds:  # a member list that is not fully loaded: ask Discord
        if guild.chunked:
            continue
        try:
            await guild.fetch_member(user.id)
            return True
        except discord.HTTPException:
            continue
    return False


def invite_url(bot: Any) -> str:
    """The link that adds the bot to a server, with the permissions its features need."""
    return discord.utils.oauth_url(
        bot.application_id,
        permissions=discord.Permissions(
            manage_roles=True, manage_channels=True, kick_members=True, ban_members=True, manage_nicknames=True,
            moderate_members=True, view_audit_log=True, send_messages=True, embed_links=True, attach_files=True,
            read_message_history=True, add_reactions=True, connect=True, speak=True, manage_messages=True,
            create_instant_invite=True,
        ),
        scopes=("bot", "applications.commands"),
    )


def _ways_in(bot: Any) -> str:
    ways = f"[Add {bot.user.name} to a server]({invite_url(bot)})"
    support = getattr(getattr(bot, "config", None), "support_server_invite", "") or ""
    if support:
        ways += f", or [join its server]({support})"
    return ways + "."


async def gate(bot: Any, interaction: discord.Interaction, command: app_commands.Command | app_commands.ContextMenu) -> bool:
    """Whether this use of a command may go ahead, as far as personal use goes.
    Sends the refusal itself. In a server that has the bot nothing changes.
    Anywhere else the person has to share a server with the bot, and outside
    the bot's own DMs the command has to be one that works as a personal app."""
    where = placement(interaction)
    if where == SERVER:
        return True
    if not await shares_server(bot, interaction.user):
        await bot.embeds.error_interaction(
            interaction,
            "Join A Server First",
            f"{bot.user.name} only works here for people who share a server with it. {_ways_in(bot)}",
            ephemeral=True,
        )
        return False
    if where == ELSEWHERE and not works_as_user_app(command.qualified_name, menu=isinstance(command, app_commands.ContextMenu)):
        await bot.embeds.error_interaction(
            interaction,
            "Server Only",
            f"`/{command.qualified_name}` only works in a server that has added {bot.user.name}. {_ways_in(bot)}",
            ephemeral=True,
        )
        return False
    return True
