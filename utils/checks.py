"""Permission checks for slash commands.

`app_commands.checks.has_permissions` looks at the permissions a person has in
the channel the command was typed in, channel overwrites included. For a
command that acts on that channel this is right. For one that acts on the
whole server it is not: "Manage Permissions" on a single channel is the same
permission bit as Manage Roles, so a helper trusted with one channel would
pass a Manage Roles check there.
"""

from __future__ import annotations

import discord
from discord import app_commands


def server_permissions(**perms: bool):
    """Like `app_commands.checks.has_permissions`, but against the permissions
    the person's roles give them server-wide."""

    def predicate(interaction: discord.Interaction) -> bool:
        have = getattr(interaction.user, "guild_permissions", None)
        missing = [name for name, value in perms.items() if have is None or getattr(have, name) != value]
        if missing:
            raise app_commands.MissingPermissions(missing)
        return True

    return app_commands.check(predicate)


def may_manage_channel(interaction: discord.Interaction, channel: discord.abc.GuildChannel) -> bool:
    """Whether the person may manage `channel` itself, which need not be the
    channel the command was run in."""
    return bool(channel.permissions_for(interaction.user).manage_channels)


def required_permissions(command: app_commands.Command) -> list[str]:
    """The member permissions a command checks before it runs, by their code
    names. Read from its `has_permissions` or `server_permissions` check,
    which keeps them in the check's closure."""
    required: list[str] = []
    for check in getattr(command, "checks", []):
        name = getattr(check, "__qualname__", "")
        if "bot_has" in name or not ("has_permissions" in name or "server_permissions" in name):
            continue
        for cell in check.__closure__ or ():
            if isinstance(cell.cell_contents, dict):
                required += [permission for permission, wanted in cell.cell_contents.items() if wanted]
    return list(dict.fromkeys(required))

