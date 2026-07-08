from __future__ import annotations

import logging
from datetime import timedelta
from typing import Iterable

import discord
from discord.ext import commands

from utils.settings import get_guild_settings

LOGGER = logging.getLogger(__name__)

TRUNCATE_LIMIT = 1000
FIELD_TRUNCATE_LIMIT = 500
AUDIT_LOG_WINDOW_SECONDS = 15
AUDIT_LOG_FETCH_LIMIT = 6

COLOR_MESSAGE_EDIT = discord.Color.orange()
COLOR_MESSAGE_DELETE = discord.Color.red()
COLOR_MESSAGE_BULK_DELETE = discord.Color.dark_red()
COLOR_MEMBER_JOIN = discord.Color.green()
COLOR_MEMBER_LEAVE = discord.Color.orange()
COLOR_MEMBER_KICK = discord.Color.red()
COLOR_MEMBER_BAN = discord.Color.dark_red()
COLOR_MEMBER_UNBAN = discord.Color.green()
COLOR_MEMBER_UPDATE = discord.Color.blurple()
COLOR_VOICE = discord.Color.blurple()
COLOR_ROLE_CREATE = discord.Color.green()
COLOR_ROLE_DELETE = discord.Color.red()
COLOR_ROLE_UPDATE = discord.Color.blurple()
COLOR_CHANNEL_CREATE = discord.Color.green()
COLOR_CHANNEL_DELETE = discord.Color.red()
COLOR_CHANNEL_UPDATE = discord.Color.teal()
COLOR_GUILD_UPDATE = discord.Color.gold()
COLOR_EMOJI_UPDATE = discord.Color.purple()
COLOR_STICKER_UPDATE = discord.Color.purple()
COLOR_INVITE_CREATE = discord.Color.teal()
COLOR_INVITE_DELETE = discord.Color.dark_teal()
COLOR_THREAD = discord.Color.blurple()


def truncate(text: str | None, limit: int = TRUNCATE_LIMIT) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    if total_seconds == 0:
        return "0 seconds"
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds and not parts:
        parts.append(f"{seconds}s")
    return " ".join(parts) if parts else "0 seconds"


class Logging(commands.Cog):
    """Server activity/audit logging.

    Sends message, member, role, channel, and other server-event activity to a
    per-guild log channel, configured from the /settings dashboard (Activity
    Log category). Deliberately skips a few known-noisy non-events (embed-only
    "edits" from link unfurls, position-only role/channel reorders, bot voice
    states) so the log stays signal, not noise.
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    # ---------- gating / dispatch ----------

    async def _log(
        self,
        guild: discord.Guild | None,
        embed: discord.Embed,
        *,
        channel_id: int | None = None,
        parent_channel_id: int | None = None,
        user_id: int | None = None,
    ) -> None:
        if guild is None:
            return

        settings = get_guild_settings(self.bot.storage_path, guild.id)
        if not settings.get("logging_enabled", False):
            return

        if channel_id is not None or parent_channel_id is not None:
            ignored_channels = {int(c) for c in (settings.get("ignored_log_channels") or [])}
            # For events inside a thread, honor an ignore on either the thread
            # itself or its parent channel.
            for candidate in (channel_id, parent_channel_id):
                if candidate is not None and int(candidate) in ignored_channels:
                    return

        if user_id is not None:
            ignored_users = {int(u) for u in (settings.get("ignored_users") or [])}
            if int(user_id) in ignored_users:
                return

        raw_log_channel_id = settings.get("logging_channel_id")
        if not raw_log_channel_id:
            return

        try:
            log_channel_id = int(raw_log_channel_id)
        except (TypeError, ValueError):
            return

        channel = guild.get_channel(log_channel_id)
        if not isinstance(channel, discord.TextChannel):
            return

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException) as exc:
            LOGGER.debug("Failed to send activity log in guild %s: %s", guild.id, exc)

    def _embed(
        self,
        *,
        title: str,
        color: discord.Color,
        description: str | None = None,
        fields: Iterable[tuple[str, str, bool]] | None = None,
        thumbnail_url: str | None = None,
        footer: str | None = None,
    ) -> discord.Embed:
        embed = self.bot.embeds.create(
            title=title,
            description=description,
            color=color,
            thumbnail_url=thumbnail_url,
            timestamp=True,
        )
        for name, value, inline in fields or []:
            embed.add_field(name=name, value=truncate(value, FIELD_TRUNCATE_LIMIT) or "—", inline=inline)
        if footer:
            embed.set_footer(text=footer)
        return embed

    async def _find_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: int,
        *,
        window_seconds: int = AUDIT_LOG_WINDOW_SECONDS,
    ) -> discord.AuditLogEntry | None:
        """Best-effort lookup of the audit log entry responsible for an event, so we
        can show who did it - not just that it happened. Silently gives up if the
        bot lacks View Audit Log or the API call fails; callers treat None as
        "unknown actor" rather than an error."""
        me = guild.me
        if me is None or not me.guild_permissions.view_audit_log:
            return None
        try:
            async for entry in guild.audit_logs(action=action, limit=AUDIT_LOG_FETCH_LIMIT):
                target = entry.target
                if target is not None and getattr(target, "id", None) == target_id:
                    age = discord.utils.utcnow() - entry.created_at
                    if age.total_seconds() <= window_seconds:
                        return entry
        except (discord.Forbidden, discord.HTTPException):
            return None
        return None

    # ---------- messages ----------

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.author.bot or before.guild is None:
            return
        # Discord fires this when a link embed loads too, with content unchanged -
        # only log when the actual text changed.
        if before.content == after.content:
            return

        embed = self._embed(
            title="✏️ Message Edited",
            color=COLOR_MESSAGE_EDIT,
            description=(
                f"**Author:** {after.author.mention}\n"
                f"**Channel:** {after.channel.mention}\n"
                f"[Jump to message]({after.jump_url})"
            ),
            fields=[
                ("Before", truncate(before.content) or "*(empty)*", False),
                ("After", truncate(after.content) or "*(empty)*", False),
            ],
            thumbnail_url=after.author.display_avatar.url,
            footer=f"User ID: {after.author.id} • Message ID: {after.id}",
        )
        await self._log(
            after.guild,
            embed,
            channel_id=after.channel.id,
            parent_channel_id=getattr(after.channel, "parent_id", None),
            user_id=after.author.id,
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return

        if message.content:
            content = truncate(message.content)
        elif message.attachments:
            content = "*(no text content - had attachment(s))*"
        else:
            content = "*(no text content)*"

        embed = self._embed(
            title="🗑️ Message Deleted",
            color=COLOR_MESSAGE_DELETE,
            description=f"**Author:** {message.author.mention}\n**Channel:** {message.channel.mention}",
            fields=[("Content", content, False)],
            thumbnail_url=message.author.display_avatar.url,
            footer=f"User ID: {message.author.id} • Message ID: {message.id}",
        )
        await self._log(
            message.guild,
            embed,
            channel_id=message.channel.id,
            parent_channel_id=getattr(message.channel, "parent_id", None),
            user_id=message.author.id,
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]) -> None:
        if not messages:
            return
        first = messages[0]
        if first.guild is None:
            return

        non_bot_authors = {m.author.id for m in messages if not m.author.bot}
        fields = [("Unique Authors", str(len(non_bot_authors)), True)] if non_bot_authors else []
        embed = self._embed(
            title="🧹 Messages Bulk Deleted",
            color=COLOR_MESSAGE_BULK_DELETE,
            description=f"**Channel:** {first.channel.mention}\n**Count:** {len(messages)}",
            fields=fields,
        )
        await self._log(
            first.guild,
            embed,
            channel_id=first.channel.id,
            parent_channel_id=getattr(first.channel, "parent_id", None),
        )

    # ---------- members ----------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        account_age = discord.utils.utcnow() - member.created_at
        title = "🤖 Bot Added" if member.bot else "📥 Member Joined"
        embed = self._embed(
            title=title,
            color=COLOR_MEMBER_JOIN,
            description=f"{member.mention} ({member})",
            fields=[
                ("Account Created", discord.utils.format_dt(member.created_at, style="R"), True),
                ("Account Age", format_duration(account_age), True),
                ("Member Count", str(member.guild.member_count), True),
            ],
            thumbnail_url=member.display_avatar.url,
            footer=f"User ID: {member.id}",
        )
        await self._log(member.guild, embed, user_id=member.id)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        kick_entry = await self._find_audit_entry(member.guild, discord.AuditLogAction.kick, member.id)

        fields: list[tuple[str, str, bool]] = []
        if kick_entry is not None:
            title = "👢 Member Kicked"
            color = COLOR_MEMBER_KICK
            fields.append(("Moderator", str(kick_entry.user) if kick_entry.user else "Unknown", True))
            fields.append(("Reason", kick_entry.reason or "No reason provided.", False))
        else:
            title = "📤 Member Left"
            color = COLOR_MEMBER_LEAVE

        roles = [role.mention for role in member.roles if not role.is_default()]
        if roles:
            shown = ", ".join(roles[:10]) + (" ..." if len(roles) > 10 else "")
            fields.append(("Roles", shown, False))

        embed = self._embed(
            title=title,
            color=color,
            description=f"{member.mention} ({member})",
            fields=fields,
            thumbnail_url=member.display_avatar.url,
            footer=f"User ID: {member.id}",
        )
        await self._log(member.guild, embed, user_id=member.id)

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User | discord.Member) -> None:
        entry = await self._find_audit_entry(guild, discord.AuditLogAction.ban, user.id)
        fields: list[tuple[str, str, bool]] = []
        if entry is not None:
            fields.append(("Moderator", str(entry.user) if entry.user else "Unknown", True))
            fields.append(("Reason", entry.reason or "No reason provided.", False))

        embed = self._embed(
            title="🔨 Member Banned",
            color=COLOR_MEMBER_BAN,
            description=f"{user.mention} ({user})",
            fields=fields,
            thumbnail_url=user.display_avatar.url,
            footer=f"User ID: {user.id}",
        )
        await self._log(guild, embed, user_id=user.id)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User) -> None:
        entry = await self._find_audit_entry(guild, discord.AuditLogAction.unban, user.id)
        fields: list[tuple[str, str, bool]] = []
        if entry is not None:
            fields.append(("Moderator", str(entry.user) if entry.user else "Unknown", True))

        embed = self._embed(
            title="🕊️ Member Unbanned",
            color=COLOR_MEMBER_UNBAN,
            description=f"{user.mention} ({user})",
            fields=fields,
            thumbnail_url=user.display_avatar.url,
            footer=f"User ID: {user.id}",
        )
        await self._log(guild, embed, user_id=user.id)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member) -> None:
        changes: list[tuple[str, str, bool]] = []

        if before.nick != after.nick:
            changes.append(("Nickname", f"`{before.nick or before.name}` → `{after.nick or after.name}`", False))

        before_roles = set(before.roles)
        after_roles = set(after.roles)
        added_roles = after_roles - before_roles
        removed_roles = before_roles - after_roles
        if added_roles:
            changes.append(("Roles Added", ", ".join(role.mention for role in added_roles), False))
        if removed_roles:
            changes.append(("Roles Removed", ", ".join(role.mention for role in removed_roles), False))

        if before.timed_out_until != after.timed_out_until:
            now = discord.utils.utcnow()
            if after.timed_out_until and after.timed_out_until > now:
                remaining = after.timed_out_until - now
                changes.append((
                    "Timed Out",
                    f"Until {discord.utils.format_dt(after.timed_out_until, style='f')} ({format_duration(remaining)} remaining)",
                    False,
                ))
            elif before.timed_out_until:
                changes.append(("Timeout Removed", "Timeout was lifted or expired.", False))

        if not changes:
            return

        embed = self._embed(
            title="👤 Member Updated",
            color=COLOR_MEMBER_UPDATE,
            description=f"{after.mention} ({after})",
            fields=changes,
            thumbnail_url=after.display_avatar.url,
            footer=f"User ID: {after.id}",
        )
        await self._log(after.guild, embed, user_id=after.id)

    # ---------- voice ----------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        if before.channel == after.channel:
            # Mute/deafen/self-video toggles etc. fire this too - not log-worthy.
            return

        if before.channel is None and after.channel is not None:
            title = "🔊 Joined Voice Channel"
            description = f"{member.mention} joined {after.channel.mention}"
            channel_id = after.channel.id
        elif before.channel is not None and after.channel is None:
            title = "🔈 Left Voice Channel"
            description = f"{member.mention} left {before.channel.mention}"
            channel_id = before.channel.id
        else:
            title = "🔄 Switched Voice Channel"
            description = f"{member.mention} moved from {before.channel.mention} to {after.channel.mention}"
            channel_id = after.channel.id

        embed = self._embed(title=title, color=COLOR_VOICE, description=description, footer=f"User ID: {member.id}")
        await self._log(member.guild, embed, channel_id=channel_id, user_id=member.id)

    # ---------- roles ----------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role) -> None:
        embed = self._embed(
            title="🎭 Role Created", color=COLOR_ROLE_CREATE, description=role.mention, footer=f"Role ID: {role.id}"
        )
        await self._log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role) -> None:
        entry = await self._find_audit_entry(role.guild, discord.AuditLogAction.role_delete, role.id)
        fields = [("Deleted By", str(entry.user) if entry and entry.user else "Unknown", True)] if entry else []
        embed = self._embed(
            title="🎭 Role Deleted",
            color=COLOR_ROLE_DELETE,
            description=f"**{role.name}**",
            fields=fields,
            footer=f"Role ID: {role.id}",
        )
        await self._log(role.guild, embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role) -> None:
        changes: list[tuple[str, str, bool]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.color != after.color:
            changes.append(("Color", f"`{before.color}` → `{after.color}`", True))
        if before.hoist != after.hoist:
            changes.append(("Hoisted", f"`{before.hoist}` → `{after.hoist}`", True))
        if before.mentionable != after.mentionable:
            changes.append(("Mentionable", f"`{before.mentionable}` → `{after.mentionable}`", True))
        if before.permissions != after.permissions:
            changes.append(("Permissions", "Changed (see server audit log for the full diff).", False))

        if not changes:
            # Most role_update events are just position churn from a drag-reorder -
            # not worth a log entry.
            return

        embed = self._embed(
            title="🎭 Role Updated", color=COLOR_ROLE_UPDATE, description=after.mention, fields=changes, footer=f"Role ID: {after.id}"
        )
        await self._log(after.guild, embed)

    # ---------- channels ----------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel) -> None:
        embed = self._embed(
            title="📺 Channel Created",
            color=COLOR_CHANNEL_CREATE,
            description=f"{channel.mention} (`{type(channel).__name__}`)",
            footer=f"Channel ID: {channel.id}",
        )
        await self._log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        entry = await self._find_audit_entry(channel.guild, discord.AuditLogAction.channel_delete, channel.id)
        fields = [("Deleted By", str(entry.user) if entry and entry.user else "Unknown", True)] if entry else []
        embed = self._embed(
            title="📺 Channel Deleted",
            color=COLOR_CHANNEL_DELETE,
            description=f"**#{channel.name}** (`{type(channel).__name__}`)",
            fields=fields,
            footer=f"Channel ID: {channel.id}",
        )
        await self._log(channel.guild, embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel) -> None:
        changes: list[tuple[str, str, bool]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))

        before_topic = getattr(before, "topic", None)
        after_topic = getattr(after, "topic", None)
        if before_topic != after_topic:
            changes.append((
                "Topic",
                f"`{truncate(before_topic, 200) or 'None'}` → `{truncate(after_topic, 200) or 'None'}`",
                False,
            ))

        before_nsfw = getattr(before, "nsfw", None)
        after_nsfw = getattr(after, "nsfw", None)
        if before_nsfw != after_nsfw:
            changes.append(("NSFW", f"`{before_nsfw}` → `{after_nsfw}`", True))

        before_slowmode = getattr(before, "slowmode_delay", None)
        after_slowmode = getattr(after, "slowmode_delay", None)
        if before_slowmode != after_slowmode:
            changes.append(("Slowmode", f"`{before_slowmode}s` → `{after_slowmode}s`", True))

        if not changes:
            # Most channel_update events are position/permission-overwrite churn
            # (category moves, per-channel perm sync) - not worth a log entry.
            return

        embed = self._embed(
            title="📺 Channel Updated",
            color=COLOR_CHANNEL_UPDATE,
            description=after.mention,
            fields=changes,
            footer=f"Channel ID: {after.id}",
        )
        await self._log(after.guild, embed, channel_id=after.id)

    # ---------- guild ----------

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild) -> None:
        changes: list[tuple[str, str, bool]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.icon != after.icon:
            changes.append(("Icon", "Changed", True))
        if before.owner_id != after.owner_id:
            changes.append(("Owner", f"<@{before.owner_id}> → <@{after.owner_id}>", False))
        if before.verification_level != after.verification_level:
            changes.append(("Verification Level", f"`{before.verification_level}` → `{after.verification_level}`", True))
        if before.afk_channel != after.afk_channel:
            changes.append(("AFK Channel", f"`{before.afk_channel}` → `{after.afk_channel}`", True))

        if not changes:
            return

        embed = self._embed(
            title="🏠 Server Updated",
            color=COLOR_GUILD_UPDATE,
            fields=changes,
            thumbnail_url=after.icon.url if after.icon else None,
        )
        await self._log(after, embed)

    @commands.Cog.listener()
    async def on_guild_emojis_update(
        self,
        guild: discord.Guild,
        before: Iterable[discord.Emoji],
        after: Iterable[discord.Emoji],
    ) -> None:
        before_ids = {emoji.id for emoji in before}
        after_list = list(after)
        after_ids = {emoji.id for emoji in after_list}
        before_list = list(before)
        added = [emoji for emoji in after_list if emoji.id not in before_ids]
        removed = [emoji for emoji in before_list if emoji.id not in after_ids]

        if not added and not removed:
            return

        fields: list[tuple[str, str, bool]] = []
        if added:
            fields.append(("Added", " ".join(str(emoji) for emoji in added[:20]), False))
        if removed:
            fields.append(("Removed", ", ".join(f"`{emoji.name}`" for emoji in removed[:20]), False))

        embed = self._embed(title="😀 Emojis Updated", color=COLOR_EMOJI_UPDATE, fields=fields)
        await self._log(guild, embed)

    @commands.Cog.listener()
    async def on_guild_stickers_update(
        self,
        guild: discord.Guild,
        before: Iterable[discord.GuildSticker],
        after: Iterable[discord.GuildSticker],
    ) -> None:
        before_ids = {sticker.id for sticker in before}
        after_list = list(after)
        after_ids = {sticker.id for sticker in after_list}
        before_list = list(before)
        added = [sticker for sticker in after_list if sticker.id not in before_ids]
        removed = [sticker for sticker in before_list if sticker.id not in after_ids]

        if not added and not removed:
            return

        fields: list[tuple[str, str, bool]] = []
        if added:
            fields.append(("Added", ", ".join(f"`{sticker.name}`" for sticker in added[:20]), False))
        if removed:
            fields.append(("Removed", ", ".join(f"`{sticker.name}`" for sticker in removed[:20]), False))

        embed = self._embed(title="🏷️ Stickers Updated", color=COLOR_STICKER_UPDATE, fields=fields)
        await self._log(guild, embed)

    # ---------- invites ----------

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite) -> None:
        if invite.guild is None or not isinstance(invite.guild, discord.Guild):
            return

        inviter = str(invite.inviter) if invite.inviter else "Unknown"
        fields = [
            ("Channel", invite.channel.mention if invite.channel else "Unknown", True),
            ("Created By", inviter, True),
            ("Max Uses", str(invite.max_uses) if invite.max_uses else "Unlimited", True),
            (
                "Expires",
                discord.utils.format_dt(invite.expires_at, style="R") if invite.expires_at else "Never",
                True,
            ),
        ]
        embed = self._embed(title="🔗 Invite Created", color=COLOR_INVITE_CREATE, description=f"`{invite.code}`", fields=fields)
        await self._log(
            invite.guild,
            embed,
            channel_id=invite.channel.id if invite.channel else None,
            user_id=invite.inviter.id if invite.inviter else None,
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite) -> None:
        if invite.guild is None or not isinstance(invite.guild, discord.Guild):
            return

        fields = [("Channel", invite.channel.mention if invite.channel else "Unknown", True)]
        embed = self._embed(title="🔗 Invite Deleted", color=COLOR_INVITE_DELETE, description=f"`{invite.code}`", fields=fields)
        await self._log(invite.guild, embed, channel_id=invite.channel.id if invite.channel else None)

    # ---------- threads ----------

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread) -> None:
        parent = thread.parent.mention if thread.parent else "Unknown"
        embed = self._embed(
            title="🧵 Thread Created",
            color=COLOR_THREAD,
            description=f"{thread.mention} in {parent}",
            footer=f"Thread ID: {thread.id}",
        )
        await self._log(thread.guild, embed, channel_id=thread.parent_id, user_id=thread.owner_id)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread) -> None:
        embed = self._embed(
            title="🧵 Thread Deleted",
            color=COLOR_THREAD,
            description=f"**{thread.name}**",
            footer=f"Thread ID: {thread.id}",
        )
        await self._log(thread.guild, embed, channel_id=thread.parent_id)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread) -> None:
        changes: list[tuple[str, str, bool]] = []
        if before.name != after.name:
            changes.append(("Name", f"`{before.name}` → `{after.name}`", False))
        if before.archived != after.archived:
            changes.append(("Archived", f"`{before.archived}` → `{after.archived}`", True))
        if before.locked != after.locked:
            changes.append(("Locked", f"`{before.locked}` → `{after.locked}`", True))

        if not changes:
            return

        embed = self._embed(
            title="🧵 Thread Updated",
            color=COLOR_THREAD,
            description=after.mention,
            fields=changes,
            footer=f"Thread ID: {after.id}",
        )
        await self._log(after.guild, embed, channel_id=after.parent_id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Logging(bot))
