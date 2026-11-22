"""Stand-ins for Discord objects, for tests that run moderation commands."""

from __future__ import annotations

from types import SimpleNamespace as NS

import discord

from utils.embeds import EmbedManager


class Role:
    def __init__(self, role_id, name, position, perms=None, *, managed=False, default=False):
        self.id, self.name, self.position, self.managed, self._default = role_id, name, position, managed, default
        self.permissions = perms or discord.Permissions.none()
        self.mention = f"<@&{role_id}>"
        self.mentionable = False

    def is_default(self):
        return self._default

    def __lt__(self, other): return self.position < other.position
    def __le__(self, other): return self.position <= other.position
    def __gt__(self, other): return self.position > other.position
    def __ge__(self, other): return self.position >= other.position


class Member:
    def __init__(self, member_id, name, roles, *, bot=False, guild=None):
        self.id, self.name, self.roles, self.bot, self.guild = member_id, name, list(roles), bot, guild
        self.mention = f"<@{member_id}>"
        self.kicked, self.dms = False, []
        self.display_avatar = NS(url="https://cdn.example/a.png")

    @property
    def top_role(self):
        return max(self.roles, key=lambda role: role.position)

    @property
    def guild_permissions(self):
        value = 0
        for role in self.roles:
            value |= role.permissions.value
        return discord.Permissions(value)

    async def add_roles(self, *roles, reason=None):
        self.roles += [role for role in roles if role not in self.roles]

    async def remove_roles(self, *roles, reason=None):
        self.roles = [role for role in self.roles if role not in roles]

    async def kick(self, reason=None):
        self.kicked = True

    async def edit(self, nick=None, reason=None):
        self.nick = nick

    async def send(self, *args, **kwargs):
        self.dms.append(kwargs.get("embed") or (args[0] if args else None))

    def __str__(self):
        return self.name


class Response:
    def __init__(self):
        self.sent, self._done = [], False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, **kwargs):
        self._done = True
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        self._done = True

    async def defer(self, **kwargs):
        self._done = True


def make_bot(storage):
    bot = NS(storage_path=storage, user=NS(id=999, name="bot", mention="<@999>"), config=NS(website_url=""))
    bot.embeds = EmbedManager(bot)
    bot.get_cog = lambda name: None
    return bot


def make_guild(*members, owner_id=77, roles=()):
    guild = NS(id=4242, owner_id=owner_id, members=list(members), name="Server")
    guild.me = next(member for member in members if member.bot)
    guild.default_role = next((role for role in roles if role.is_default()), None)
    guild.get_role = lambda role_id: next((role for role in roles if role.id == role_id), None)
    guild.get_member = lambda member_id: next((member for member in members if member.id == member_id), None)
    for member in members:
        member.guild = guild
    return guild


def make_interaction(guild, user, channel=None):
    interaction = NS(guild=guild, user=user, response=Response(), channel=channel or NS(id=5, mention="<#5>"), guild_id=guild.id, command=None)
    interaction.is_guild_integration = lambda: True
    interaction.edits, interaction.followups = [], []

    async def edit_original_response(**kwargs):
        interaction.edits.append(kwargs)

    async def original_response():
        return NS()

    async def followup_send(*args, **kwargs):
        interaction.followups.append(kwargs)

    interaction.edit_original_response, interaction.original_response = edit_original_response, original_response
    interaction.followup = NS(send=followup_send)
    return interaction


def said(interaction) -> str:
    """Everything the command told the person, as one string."""
    parts = []
    for content, kwargs in interaction.response.sent:
        parts.append(content or "")
        embed = kwargs.get("embed")
        if embed is not None:
            parts += [embed.title or "", embed.description or ""]
    for kwargs in interaction.edits + interaction.followups:
        embed = kwargs.get("embed")
        if embed is not None:
            parts += [embed.title or "", embed.description or ""]
    return "\n".join(parts)
