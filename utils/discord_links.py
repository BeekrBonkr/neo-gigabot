from __future__ import annotations

import re

import discord

MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>@me|\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)$"
)


def parse_message_link(message_link: str) -> tuple[str, int, int]:
    match = MESSAGE_LINK_RE.match(message_link.strip())
    if not match:
        raise ValueError("That is not a valid Discord message link.")

    guild_id = match.group("guild_id")
    channel_id = int(match.group("channel_id"))
    message_id = int(match.group("message_id"))
    return guild_id, channel_id, message_id


async def resolve_channel_for_link(
    bot: discord.Client,
    guild_id: str,
    channel_id: int,
    *,
    hint_guild: discord.Guild | None = None,
) -> discord.abc.Messageable:
    if guild_id == "@me":
        channel = bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                raise ValueError("I could not access that DM channel.")
        return channel

    guild_id_int = int(guild_id)
    guild = hint_guild if hint_guild and hint_guild.id == guild_id_int else bot.get_guild(guild_id_int)
    if guild is None:
        try:
            guild = await bot.fetch_guild(guild_id_int)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            raise ValueError("I am not in that server or I cannot access it.")

    channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
    if channel is None:
        try:
            channel = await bot.fetch_channel(channel_id)
        except (discord.Forbidden, discord.NotFound, discord.HTTPException):
            raise ValueError("I could not access the channel from that message link.")

    return channel


async def can_read(channel: discord.abc.Messageable, viewer: discord.abc.User) -> bool:
    """Whether `viewer` can read messages in `channel`."""
    guild = getattr(channel, "guild", None)
    if guild is None:  # a DM: only the person it is with
        recipient = getattr(channel, "recipient", None)
        return recipient is not None and recipient.id == viewer.id
    member = guild.get_member(viewer.id)
    if member is None:
        try:
            member = await guild.fetch_member(viewer.id)
        except discord.HTTPException:
            return False
    permissions = channel.permissions_for(member)
    return bool(permissions.view_channel and permissions.read_message_history)


async def fetch_message_from_link(
    bot: discord.Client,
    message_link: str,
    *,
    hint_guild: discord.Guild | None = None,
    viewer: discord.abc.User | None = None,
) -> discord.Message:
    """The message a link points at. The bot reads it with its own access, so
    `viewer` (the person who gave the link) must be able to read that channel
    themselves: otherwise a link is a way to have the bot fetch from a channel,
    or a server, the person is not in."""
    guild_id, channel_id, message_id = parse_message_link(message_link)
    channel = await resolve_channel_for_link(bot, guild_id, channel_id, hint_guild=hint_guild)
    if viewer is not None and not await can_read(channel, viewer):
        raise ValueError("I could not fetch that message. Make sure the link is valid and that you can see that channel.")

    if not hasattr(channel, "fetch_message"):
        raise ValueError("That message link does not point to a fetchable text channel.")

    try:
        return await channel.fetch_message(message_id)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
        raise ValueError("I could not fetch that message. Make sure the link is valid and I can view that channel.")
