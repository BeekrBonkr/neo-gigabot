from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils.durations import parse_duration
from utils.tempbans import add_tempban, due_tempbans, remove_tempban
from utils.settings import (
    dm_on_mod_action_enabled,
    get_guild_settings,
    get_mod_log_channel_id,
    is_mod_log_enabled,
    is_mod_log_event_enabled,
    save_guild_settings,
)
from utils.sticky_roles import pop_roles as pop_sticky_roles
from utils.sticky_roles import save_roles as save_sticky_roles
from utils.warnings import add_warning, clear_warnings, count_warnings, list_warnings, remove_warning

LOGGER = logging.getLogger(__name__)

MAX_TIMEOUT = timedelta(days=28)

MASS_ACTION_DELAY_SECONDS = 0.5
MASS_ACTION_PROGRESS_INTERVAL = 10
MAX_MASS_ACTION_TARGETS = 300

PRUNE_MODE_CHOICES = [
    app_commands.Choice(name="default", value="default"),
    app_commands.Choice(name="specific", value="specific"),
    app_commands.Choice(name="exclude", value="exclude"),
]
ROLE_ACTION_CHOICES = [
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
]
AUTOROLE_ACTION_CHOICES = [
    app_commands.Choice(name="add", value="add"),
    app_commands.Choice(name="remove", value="remove"),
    app_commands.Choice(name="apply", value="apply"),
]


class ConfirmView(discord.ui.View):
    """Generic Confirm/Cancel view for destructive actions - replaces the old
    bot's "type yes to confirm" plain-message prompts, which don't map to
    slash commands well."""

    def __init__(self, *, author_id: int, timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.value: bool | None = None
        self.message: discord.Message | None = None

        confirm_button = discord.ui.Button(label="Confirm", style=discord.ButtonStyle.danger)
        confirm_button.callback = self._on_confirm
        cancel_button = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
        cancel_button.callback = self._on_cancel
        self.add_item(confirm_button)
        self.add_item(cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This confirmation isn't yours.", ephemeral=True)
            return False
        return True

    async def _finish(self, interaction: discord.Interaction, value: bool) -> None:
        self.value = value
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, True)

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        await self._finish(interaction, False)

    async def on_timeout(self) -> None:
        self.value = False
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


POLL_NUMBER_EMOJIS = ("1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣")


class PollView(discord.ui.View):
    """Button-based poll with live tallies - replaces the old bot's
    thumbsup/thumbsdown reaction poll (no vote count visible without counting
    reactions, and nothing stopped someone from reacting to both). Supports
    2-5 custom options; with none given it's a plain 👍/👎 yes-no poll. One
    vote per person - voting again switches it, voting the same option
    retracts it."""

    def __init__(
        self,
        *,
        bot: commands.Bot,
        prompt: str,
        author: discord.abc.User,
        options: list[str],
        emojis: tuple[str, ...],
        timeout: float = 300.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.bot = bot
        self.prompt = prompt
        self.author = author
        self.options = options
        self.emojis = emojis
        self.votes: dict[int, int] = {}  # voter id -> option index
        self.message: discord.Message | None = None

        for index in range(len(options)):
            button = discord.ui.Button(label="0", emoji=emojis[index], style=discord.ButtonStyle.secondary)
            button.callback = self._make_vote_callback(index)
            self.add_item(button)

    def _make_vote_callback(self, index: int):
        async def _on_vote(interaction: discord.Interaction) -> None:
            if self.votes.get(interaction.user.id) == index:
                del self.votes[interaction.user.id]
            else:
                self.votes[interaction.user.id] = index
            await self._refresh(interaction)

        return _on_vote

    def _counts(self) -> list[int]:
        counts = [0] * len(self.options)
        for option_index in self.votes.values():
            counts[option_index] += 1
        return counts

    def build_embed(self, *, closed: bool = False) -> discord.Embed:
        counts = self._counts()
        lines = [f"{self.emojis[i]} **{option}** - {counts[i]}" for i, option in enumerate(self.options)]
        footer = f"{len(self.votes)} vote(s)"
        if closed:
            footer += " • Poll closed"
        return self.bot.embeds.create(
            title="📊 Poll",
            description=self.prompt + "\n\n" + "\n".join(lines),
            color=discord.Color.blurple(),
            author_name=str(self.author),
            author_icon_url=self.author.display_avatar.url,
            footer=footer,
        )

    async def _refresh(self, interaction: discord.Interaction) -> None:
        counts = self._counts()
        for index, item in enumerate(self.children):
            item.label = str(counts[index])
        await interaction.response.edit_message(embed=self.build_embed(), view=self)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(embed=self.build_embed(closed=True), view=self)
            except discord.HTTPException:
                pass


class Moderation(commands.Cog):
    """Moderation commands: member actions, channel/message cleanup, and mass
    role management."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._process_due_tempbans.start()

    async def cog_unload(self) -> None:
        self._process_due_tempbans.cancel()

    @tasks.loop(seconds=60)
    async def _process_due_tempbans(self) -> None:
        for entry in due_tempbans(self.bot.storage_path):
            guild = self.bot.get_guild(entry["guild_id"])
            if guild is None:
                # Bot is no longer in that guild - the schedule can't be honored.
                remove_tempban(self.bot.storage_path, entry["guild_id"], entry["user_id"])
                continue

            try:
                await guild.unban(
                    discord.Object(id=entry["user_id"]),
                    reason=f"Tempban expired (banned by moderator {entry['moderator_id']})",
                )
            except discord.NotFound:
                pass  # already unbanned manually - nothing left to do
            except discord.Forbidden:
                # Retrying every minute won't grant us the permission; drop the
                # row and leave a trail instead of spinning forever.
                LOGGER.warning("Cannot unban user %s in guild %s (missing permissions)", entry["user_id"], guild.id)
            except discord.HTTPException as exc:
                LOGGER.warning("Transient error unbanning user %s in guild %s: %s", entry["user_id"], guild.id, exc)
                continue  # keep the row; retry next tick

            remove_tempban(self.bot.storage_path, entry["guild_id"], entry["user_id"])

            target = await self._fetch_user_quiet(entry["user_id"])
            if target is not None:
                await self._log_mod_action(
                    guild, event="ban", title="🔓 Tempban Expired", color=self.bot.embeds.success_color,
                    target=target, moderator=self.bot.user, reason=entry["reason"] or None,
                )

    @_process_due_tempbans.before_loop
    async def _before_tempban_loop(self) -> None:
        await self.bot.wait_until_ready()

    def _invoker_outranks(self, interaction: discord.Interaction, target: discord.Member) -> bool:
        """True if the invoking moderator is allowed to act on `target` by role
        hierarchy: the server owner outranks everyone, otherwise the invoker's
        top role must be strictly above the target's. Discord's own moderation
        UI enforces the same rule; without this, anyone with e.g. Kick Members
        could kick mods ranked above them as long as the target sat below the
        *bot's* top role."""
        if interaction.guild.owner_id == interaction.user.id:
            return True
        if not isinstance(interaction.user, discord.Member):
            return False
        return interaction.user.top_role > target.top_role

    async def _try_dm(self, user: discord.abc.User, embed: discord.Embed, *, guild_id: int | None = None) -> None:
        # Guild-scoped DMs (kick/ban/mute/warn notices) can be turned off per
        # guild; pass no guild_id for DMs that aren't moderation notices.
        if guild_id is not None and not dm_on_mod_action_enabled(self.bot.storage_path, guild_id):
            return
        try:
            await user.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _log_mod_action(
        self,
        guild: discord.Guild,
        *,
        event: str,
        title: str,
        color: discord.Color,
        target: discord.abc.User,
        moderator: discord.abc.User,
        reason: str | None,
        duration: str | None = None,
    ) -> None:
        """Send a moderation action to the configured mod-log channel, if enabled.
        Deliberately separate from cogs/logging.py's activity log - that's a
        general audit trail admins opt into per-channel/per-user; this is
        specifically for kick/ban/mute/warn actions, which nothing previously
        recorded anywhere persistent. `event` is the filterable action kind
        ("warn"/"mute"/"kick"/"ban") admins can toggle from the dashboard."""
        if not is_mod_log_enabled(self.bot.storage_path, guild.id):
            return
        if not is_mod_log_event_enabled(self.bot.storage_path, guild.id, event):
            return
        channel_id = get_mod_log_channel_id(self.bot.storage_path, guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if not isinstance(channel, discord.TextChannel):
            return

        description = f"**Member:** {target.mention} (`{target.id}`)\n**Moderator:** {moderator.mention}"
        if duration:
            description += f"\n**Duration:** {duration}"
        description += f"\n**Reason:** {reason or 'No reason provided.'}"

        try:
            await channel.send(embed=self.bot.embeds.create(title=title, description=description, color=color, timestamp=True))
        except (discord.Forbidden, discord.HTTPException):
            pass

    async def _maybe_escalate_warning(self, interaction: discord.Interaction, user: discord.Member, warning_count: int) -> None:
        """Automatically mute/kick/ban a member once their warning count reaches
        this guild's configured threshold. A no-op unless an admin has opted in
        via the settings dashboard (warn_threshold > 0 and warn_action != "none")."""
        settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        threshold = int(settings.get("warn_threshold", 0) or 0)
        action = settings.get("warn_action", "none")
        if threshold <= 0 or action == "none":
            return
        # With a window configured, escalate on recent warnings only - old
        # infractions age out instead of counting against the member forever.
        window_days = int(settings.get("warn_window_days", 0) or 0)
        if window_days > 0:
            warning_count = count_warnings(self.bot.storage_path, interaction.guild.id, user.id, within_days=window_days)
        if warning_count < threshold:
            return
        if user.top_role >= interaction.guild.me.top_role:
            return

        reason = f"Automatic action: reached {warning_count} warning(s) (threshold {threshold})"

        if action == "mute":
            delta = parse_duration(str(settings.get("warn_mute_duration", "1h"))) or timedelta(hours=1)
            if delta > MAX_TIMEOUT:
                delta = MAX_TIMEOUT
            try:
                await user.timeout(delta, reason=reason)
            except discord.Forbidden:
                return
            await self._try_dm(
                user,
                self.bot.embeds.warning_embed(
                    "You Were Muted", f"You were automatically muted in **{interaction.guild.name}** after reaching {warning_count} warnings."
                ),
                guild_id=interaction.guild.id,
            )
            await self.bot.embeds.info_interaction(
                interaction, "Auto-Escalation", f"{user.mention} reached {warning_count} warning(s) and has been automatically muted."
            )
            await self._log_mod_action(
                interaction.guild, event="mute", title="🔇 Auto-Muted (Warning Threshold)", color=self.bot.embeds.warning_color,
                target=user, moderator=self.bot.user, reason=reason,
            )
        elif action == "kick":
            try:
                await user.kick(reason=reason)
            except discord.Forbidden:
                return
            await self.bot.embeds.info_interaction(
                interaction, "Auto-Escalation", f"{user.mention} reached {warning_count} warning(s) and has been automatically kicked."
            )
            await self._log_mod_action(
                interaction.guild, event="kick", title="👢 Auto-Kicked (Warning Threshold)", color=self.bot.embeds.warning_color,
                target=user, moderator=self.bot.user, reason=reason,
            )
        elif action == "ban":
            try:
                await user.ban(reason=reason)
            except discord.Forbidden:
                return
            await self.bot.embeds.info_interaction(
                interaction, "Auto-Escalation", f"{user.mention} reached {warning_count} warning(s) and has been automatically banned."
            )
            await self._log_mod_action(
                interaction.guild, event="ban", title="🔨 Auto-Banned (Warning Threshold)", color=self.bot.embeds.error_color,
                target=user, moderator=self.bot.user, reason=reason,
            )

    # ---------- basic member actions ----------

    @app_commands.command(name="say", description="Send an embed containing your message.")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def say(self, interaction: discord.Interaction, message: str) -> None:
        embed = self.bot.embeds.create(
            title="Message",
            description=message,
            author_name=str(interaction.user),
            author_icon_url=interaction.user.display_avatar.url,
        )
        await self.bot.embeds.respond(interaction, embed=embed)

    @app_commands.command(name="kick", description="Kick a member from the server.")
    @app_commands.describe(user="Member to kick", reason="Reason (shown to the member and in the audit log)")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def kick(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None) -> None:
        if user.id == interaction.user.id:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "You can't kick yourself.", ephemeral=True)
            return
        if user.top_role >= interaction.guild.me.top_role:
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) mine.", ephemeral=True
            )
            return
        if not self._invoker_outranks(interaction, user):
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) yours.", ephemeral=True
            )
            return

        await self._try_dm(
            user,
            self.bot.embeds.warning_embed("You Were Kicked", f"You were kicked from **{interaction.guild.name}**.\nReason: {reason or 'No reason provided.'}"),
            guild_id=interaction.guild.id,
        )

        try:
            await user.kick(reason=f"By {interaction.user} ({interaction.user.id}): {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Kick Failed", "I don't have permission to kick that member.", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(interaction, "Member Kicked", f"{user.mention} has been kicked.\nReason: {reason or 'No reason provided.'}")
        await self._log_mod_action(
            interaction.guild, event="kick", title="👢 Member Kicked", color=self.bot.embeds.warning_color,
            target=user, moderator=interaction.user, reason=reason,
        )

    @app_commands.command(name="ban", description="Ban a member from the server.")
    @app_commands.describe(
        user="Member to ban",
        reason="Reason (shown to the member and in the audit log)",
        delete_message_days="Delete this many days of their recent messages (0-7, default 0)",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def ban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        if user.id == interaction.user.id:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "You can't ban yourself.", ephemeral=True)
            return
        if user.top_role >= interaction.guild.me.top_role:
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) mine.", ephemeral=True
            )
            return
        if not self._invoker_outranks(interaction, user):
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) yours.", ephemeral=True
            )
            return

        await self._try_dm(
            user,
            self.bot.embeds.error_embed("You Were Banned", f"You were banned from **{interaction.guild.name}**.\nReason: {reason or 'No reason provided.'}"),
            guild_id=interaction.guild.id,
        )

        try:
            await user.ban(
                reason=f"By {interaction.user} ({interaction.user.id}): {reason or 'No reason provided.'}",
                delete_message_seconds=delete_message_days * 86400,
            )
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Ban Failed", "I don't have permission to ban that member.", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(interaction, "Member Banned", f"{user.mention} has been banned.\nReason: {reason or 'No reason provided.'}")
        await self._log_mod_action(
            interaction.guild, event="ban", title="🔨 Member Banned", color=self.bot.embeds.error_color,
            target=user, moderator=interaction.user, reason=reason,
        )

    @app_commands.command(name="tempban", description="Ban a member and automatically unban them after a duration.")
    @app_commands.describe(
        user="Member to ban",
        duration="e.g. 12h, 3d, 1w",
        reason="Reason (shown to the member and in the audit log)",
        delete_message_days="Delete this many days of their recent messages (0-7, default 0)",
    )
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def tempban(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
        duration: str,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] = 0,
    ) -> None:
        delta = parse_duration(duration)
        if delta is None:
            await self.bot.embeds.error_interaction(
                interaction, "Invalid Duration", "Use something like `12h`, `3d`, `1w`, or `1d12h`.", ephemeral=True
            )
            return
        if user.id == interaction.user.id:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "You can't ban yourself.", ephemeral=True)
            return
        if user.top_role >= interaction.guild.me.top_role:
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) mine.", ephemeral=True
            )
            return
        if not self._invoker_outranks(interaction, user):
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) yours.", ephemeral=True
            )
            return

        unban_at = discord.utils.utcnow() + delta
        await self._try_dm(
            user,
            self.bot.embeds.error_embed(
                "You Were Banned",
                f"You were banned from **{interaction.guild.name}** until {discord.utils.format_dt(unban_at, 'F')}.\n"
                f"Reason: {reason or 'No reason provided.'}",
            ),
            guild_id=interaction.guild.id,
        )

        try:
            await user.ban(
                reason=f"Tempban ({duration}) by {interaction.user} ({interaction.user.id}): {reason or 'No reason provided.'}",
                delete_message_seconds=delete_message_days * 86400,
            )
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Ban Failed", "I don't have permission to ban that member.", ephemeral=True)
            return

        add_tempban(self.bot.storage_path, interaction.guild.id, user.id, interaction.user.id, reason or "", unban_at)

        await self.bot.embeds.success_interaction(
            interaction,
            "Member Temp-Banned",
            f"{user.mention} has been banned until {discord.utils.format_dt(unban_at, 'F')} "
            f"({discord.utils.format_dt(unban_at, 'R')}).\nReason: {reason or 'No reason provided.'}",
        )
        await self._log_mod_action(
            interaction.guild, event="ban", title="🔨 Member Temp-Banned", color=self.bot.embeds.error_color,
            target=user, moderator=interaction.user, reason=reason, duration=duration,
        )

    @app_commands.command(name="unban", description="Unban a user by their ID.")
    @app_commands.describe(user_id="The banned user's ID", reason="Optional reason")
    @app_commands.checks.has_permissions(ban_members=True)
    @app_commands.checks.bot_has_permissions(ban_members=True)
    @app_commands.guild_only()
    async def unban(self, interaction: discord.Interaction, user_id: str, reason: str | None = None) -> None:
        if not user_id.isdigit():
            await self.bot.embeds.error_interaction(interaction, "Invalid ID", "That doesn't look like a valid user ID.", ephemeral=True)
            return

        try:
            user = await self.bot.fetch_user(int(user_id))
        except discord.NotFound:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "No Discord user has that ID.", ephemeral=True)
            return

        try:
            await interaction.guild.unban(user, reason=reason)
        except discord.NotFound:
            await self.bot.embeds.error_interaction(interaction, "Not Banned", f"{user} isn't banned here.", ephemeral=True)
            return
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Unban Failed", "I don't have permission to unban.", ephemeral=True)
            return

        # Cancel any pending automatic unban so the loop doesn't try again later.
        remove_tempban(self.bot.storage_path, interaction.guild.id, user.id)

        await self.bot.embeds.success_interaction(interaction, "Member Unbanned", f"{user} has been unbanned.")
        await self._log_mod_action(
            interaction.guild, event="ban", title="🔓 Member Unbanned", color=self.bot.embeds.success_color,
            target=user, moderator=interaction.user, reason=reason,
        )

    @app_commands.command(name="mute", description="Temporarily prevent a member from sending messages or speaking.")
    @app_commands.describe(user="Member to mute", duration="e.g. 10m, 2h, 1d (max 28 days)", reason="Optional reason")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def mute(self, interaction: discord.Interaction, user: discord.Member, duration: str, reason: str | None = None) -> None:
        delta = parse_duration(duration)
        if delta is None:
            await self.bot.embeds.error_interaction(
                interaction, "Invalid Duration", "Use something like `10m`, `2h`, `1d`, or `1d12h`.", ephemeral=True
            )
            return
        if delta > MAX_TIMEOUT:
            await self.bot.embeds.error_interaction(interaction, "Too Long", "Discord timeouts can't exceed 28 days.", ephemeral=True)
            return
        if user.top_role >= interaction.guild.me.top_role:
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) mine.", ephemeral=True
            )
            return
        if not self._invoker_outranks(interaction, user):
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above (or equal to) yours.", ephemeral=True
            )
            return

        try:
            await user.timeout(delta, reason=f"By {interaction.user} ({interaction.user.id}): {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Mute Failed", "I don't have permission to time out that member.", ephemeral=True)
            return

        await self._try_dm(
            user,
            self.bot.embeds.warning_embed(
                "You Were Muted", f"You were muted in **{interaction.guild.name}** for {duration}.\nReason: {reason or 'No reason provided.'}"
            ),
            guild_id=interaction.guild.id,
        )
        await self.bot.embeds.success_interaction(interaction, "Member Muted", f"{user.mention} has been muted for {duration}.")
        await self._log_mod_action(
            interaction.guild, event="mute", title="🔇 Member Muted", color=self.bot.embeds.warning_color,
            target=user, moderator=interaction.user, reason=reason, duration=duration,
        )

    @app_commands.command(name="unmute", description="Remove an active mute from a member.")
    @app_commands.describe(user="Member to unmute", reason="Optional reason")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.checks.bot_has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def unmute(self, interaction: discord.Interaction, user: discord.Member, reason: str | None = None) -> None:
        if not user.is_timed_out():
            await self.bot.embeds.info_interaction(interaction, "Not Muted", f"{user.mention} isn't currently muted.")
            return

        try:
            await user.timeout(None, reason=f"By {interaction.user} ({interaction.user.id}): {reason or 'No reason provided.'}")
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Unmute Failed", "I don't have permission to do that.", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(interaction, "Member Unmuted", f"{user.mention} has been unmuted.")
        await self._log_mod_action(
            interaction.guild, event="mute", title="🔊 Member Unmuted", color=self.bot.embeds.success_color,
            target=user, moderator=interaction.user, reason=reason,
        )

    @app_commands.command(name="warn", description="Issue a warning to a member.")
    @app_commands.describe(user="Member to warn", reason="Reason for the warning")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def warn(self, interaction: discord.Interaction, user: discord.Member, reason: str) -> None:
        if user.id == interaction.user.id:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "You can't warn yourself.", ephemeral=True)
            return
        if user.bot:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "Bots can't be warned.", ephemeral=True)
            return
        # Same hierarchy rule as kick/ban/mute - a warning can auto-escalate
        # into a mute/kick/ban, so letting a junior mod warn a senior one would
        # bypass the hierarchy checks on those actions.
        if not self._invoker_outranks(interaction, user):
            await self.bot.embeds.error_interaction(
                interaction, "Not Allowed", "That member's highest role is above or equal to yours.", ephemeral=True
            )
            return
        add_warning(self.bot.storage_path, interaction.guild.id, user.id, interaction.user.id, reason)
        warning_count = count_warnings(self.bot.storage_path, interaction.guild.id, user.id)

        await self._try_dm(
            user,
            self.bot.embeds.warning_embed("You Were Warned", f"You were warned in **{interaction.guild.name}**.\nReason: {reason}"),
            guild_id=interaction.guild.id,
        )
        await self.bot.embeds.success_interaction(
            interaction, "Member Warned", f"{user.mention} has been warned.\nReason: {reason}\n\n({warning_count} total warning(s))"
        )
        await self._log_mod_action(
            interaction.guild, event="warn", title="⚠️ Member Warned", color=self.bot.embeds.warning_color,
            target=user, moderator=interaction.user, reason=reason,
        )
        await self._maybe_escalate_warning(interaction, user, warning_count)

    warnings_group = app_commands.Group(
        name="warnings",
        description="View and manage member warnings.",
        guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )

    @warnings_group.command(name="view", description="Show a member's warning history.")
    @app_commands.describe(user="Member to look up")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings_view(self, interaction: discord.Interaction, user: discord.Member) -> None:
        entries = list_warnings(self.bot.storage_path, interaction.guild.id, user.id)
        if not entries:
            await self.bot.embeds.info_interaction(interaction, "No Warnings", f"{user.mention} has no warnings on record.", ephemeral=True)
            return

        lines = []
        for entry in entries[:15]:
            moderator = interaction.guild.get_member(entry["moderator_id"])
            mod_label = moderator.mention if moderator else f"<@{entry['moderator_id']}>"
            # created_at is SQLite's UTC "YYYY-MM-DD HH:MM:SS"; render it as a
            # Discord timestamp so it shows in each viewer's local time.
            try:
                created = datetime.strptime(entry["created_at"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                when = discord.utils.format_dt(created, style="f")
            except (TypeError, ValueError):
                when = str(entry["created_at"])
            lines.append(f"**#{entry['id']}** by {mod_label} - {entry['reason']}\n{when}")

        embed = self.bot.embeds.info_embed(f"Warnings for {user} ({len(entries)} total)", "\n\n".join(lines))
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    @warnings_group.command(name="remove", description="Delete a single warning by its ID.")
    @app_commands.describe(warning_id="The warning's ID (shown as #N in /warnings view)")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings_remove(self, interaction: discord.Interaction, warning_id: int) -> None:
        removed = remove_warning(self.bot.storage_path, interaction.guild.id, warning_id)
        if removed is None:
            await self.bot.embeds.error_interaction(
                interaction, "Not Found", f"No warning with ID #{warning_id} exists in this server.", ephemeral=True
            )
            return

        await self.bot.embeds.success_interaction(
            interaction,
            "Warning Removed",
            f"Removed warning **#{warning_id}** from <@{removed['user_id']}>.\nOriginal reason: {removed['reason'] or 'No reason provided.'}",
            ephemeral=True,
        )
        target = interaction.guild.get_member(removed["user_id"]) or await self._fetch_user_quiet(removed["user_id"])
        if target is not None:
            await self._log_mod_action(
                interaction.guild, event="warn", title="🧹 Warning Removed", color=self.bot.embeds.success_color,
                target=target, moderator=interaction.user, reason=f"Removed warning #{warning_id}: {removed['reason'] or 'No reason provided.'}",
            )

    @warnings_group.command(name="clear", description="Delete all of a member's warnings.")
    @app_commands.describe(user="Member whose warnings should be cleared")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warnings_clear(self, interaction: discord.Interaction, user: discord.Member) -> None:
        removed_count = clear_warnings(self.bot.storage_path, interaction.guild.id, user.id)
        if removed_count == 0:
            await self.bot.embeds.info_interaction(interaction, "No Warnings", f"{user.mention} has no warnings on record.", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(
            interaction, "Warnings Cleared", f"Removed all {removed_count} warning(s) from {user.mention}.", ephemeral=True
        )
        await self._log_mod_action(
            interaction.guild, event="warn", title="🧹 Warnings Cleared", color=self.bot.embeds.success_color,
            target=user, moderator=interaction.user, reason=f"Cleared {removed_count} warning(s)",
        )

    async def _fetch_user_quiet(self, user_id: int) -> discord.User | None:
        try:
            return await self.bot.fetch_user(user_id)
        except (discord.NotFound, discord.HTTPException):
            return None

    @app_commands.command(name="nick", description="Change the bot's nickname in this server.")
    @app_commands.describe(nickname="New nickname (leave empty to reset)")
    @app_commands.checks.has_permissions(manage_nicknames=True)
    @app_commands.checks.bot_has_permissions(change_nickname=True)
    @app_commands.guild_only()
    async def nick(self, interaction: discord.Interaction, nickname: app_commands.Range[str, 0, 32] | None = None) -> None:
        await interaction.guild.me.edit(nick=nickname or None)
        message = f'My nickname is now "{nickname}".' if nickname else "My nickname has been reset."
        await self.bot.embeds.success_interaction(interaction, "Nickname Updated", message, ephemeral=True)

    @app_commands.command(name="poll", description="Create a poll - yes/no by default, or up to 5 custom options.")
    @app_commands.describe(
        prompt="The poll question",
        option1="First choice (leave all options empty for a yes/no poll)",
        option2="Second choice",
        option3="Third choice",
        option4="Fourth choice",
        option5="Fifth choice",
    )
    @app_commands.guild_only()
    async def poll(
        self,
        interaction: discord.Interaction,
        prompt: app_commands.Range[str, 1, 250],
        option1: app_commands.Range[str, 1, 80] | None = None,
        option2: app_commands.Range[str, 1, 80] | None = None,
        option3: app_commands.Range[str, 1, 80] | None = None,
        option4: app_commands.Range[str, 1, 80] | None = None,
        option5: app_commands.Range[str, 1, 80] | None = None,
    ) -> None:
        custom = [opt.strip() for opt in (option1, option2, option3, option4, option5) if opt and opt.strip()]
        if len(custom) == 1:
            await self.bot.embeds.error_interaction(
                interaction, "Not Enough Options", "Give at least two options, or none for a yes/no poll.", ephemeral=True
            )
            return

        if custom:
            options, emojis = custom, POLL_NUMBER_EMOJIS[: len(custom)]
        else:
            options, emojis = ["Yes", "No"], ("👍", "👎")

        view = PollView(bot=self.bot, prompt=prompt, author=interaction.user, options=options, emojis=emojis)
        await interaction.response.send_message(embed=view.build_embed(), view=view)
        view.message = await interaction.original_response()

    # ---------- channel / message cleanup ----------

    @app_commands.command(name="purge", description="Bulk-delete recent messages in this channel.")
    @app_commands.describe(count="How many messages to delete (max 100)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def purge(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 100]) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        # Discord's bulk delete rejects messages older than 14 days; scope the
        # scan to that window so old channels don't turn into "Command Failed".
        cutoff = discord.utils.utcnow() - timedelta(days=13, hours=23)
        deleted = await interaction.channel.purge(limit=count, after=cutoff, oldest_first=False)
        note = "" if deleted else "\nMessages older than 14 days can't be bulk-deleted."
        await interaction.followup.send(embed=self.bot.embeds.success_embed("Purged", f"Deleted {len(deleted)} message(s).{note}"))

    @app_commands.command(name="clean", description="Delete this bot's own recent messages in this channel.")
    @app_commands.describe(count="How many of the bot's messages to look through (max 200)")
    @app_commands.checks.has_permissions(manage_messages=True)
    @app_commands.checks.bot_has_permissions(manage_messages=True)
    @app_commands.guild_only()
    async def clean(self, interaction: discord.Interaction, count: app_commands.Range[int, 1, 200] = 50) -> None:
        await interaction.response.defer(thinking=True, ephemeral=True)
        cutoff = discord.utils.utcnow() - timedelta(days=13, hours=23)
        deleted = await interaction.channel.purge(
            limit=count, check=lambda m: m.author.id == self.bot.user.id, after=cutoff, oldest_first=False
        )
        await interaction.followup.send(embed=self.bot.embeds.success_embed("Cleaned", f"Deleted {len(deleted)} of my own message(s)."))

    @app_commands.command(name="lock", description="Prevent @everyone from sending messages in a channel.")
    @app_commands.describe(channel="Channel to lock (defaults to the current channel)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await self.bot.embeds.error_interaction(
                interaction, "Not Lockable", "This only works on regular text channels - run it there or pass one explicitly.", ephemeral=True
            )
            return
        everyone = interaction.guild.default_role
        overwrite = target.overwrites_for(everyone)

        if overwrite.send_messages is False:
            await self.bot.embeds.info_interaction(interaction, "Already Locked", f"{target.mention} is already locked.", ephemeral=True)
            return

        overwrite.send_messages = False
        await target.set_permissions(everyone, overwrite=overwrite, reason=f"Locked by {interaction.user}")
        await self.bot.embeds.success_interaction(interaction, "Channel Locked", f"{target.mention} is now locked.")

    @app_commands.command(name="unlock", description="Allow @everyone to send messages in a channel again.")
    @app_commands.describe(channel="Channel to unlock (defaults to the current channel)")
    @app_commands.checks.has_permissions(manage_channels=True)
    @app_commands.checks.bot_has_permissions(manage_channels=True)
    @app_commands.guild_only()
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await self.bot.embeds.error_interaction(
                interaction, "Not Unlockable", "This only works on regular text channels - run it there or pass one explicitly.", ephemeral=True
            )
            return
        everyone = interaction.guild.default_role
        overwrite = target.overwrites_for(everyone)

        if overwrite.send_messages is not False:
            await self.bot.embeds.info_interaction(interaction, "Already Unlocked", f"{target.mention} isn't locked.", ephemeral=True)
            return

        overwrite.send_messages = None
        await target.set_permissions(everyone, overwrite=overwrite, reason=f"Unlocked by {interaction.user}")
        await self.bot.embeds.success_interaction(interaction, "Channel Unlocked", f"{target.mention} is now unlocked.")

    # ---------- mass actions ----------

    async def _mass_apply(
        self,
        interaction: discord.Interaction,
        members: list[discord.Member],
        action,
        *,
        verb: str,
    ) -> int:
        """Run `action(member)` over a capped list of members with rate-limit-friendly
        pacing and periodic progress updates. Returns how many succeeded."""
        capped = members[:MAX_MASS_ACTION_TARGETS]
        done = 0
        for processed, member in enumerate(capped, start=1):
            try:
                await action(member)
                done += 1
            except discord.HTTPException:
                pass
            # Progress is keyed to how many we've *attempted*, not how many
            # succeeded - keying on `done` would edit the message on every
            # iteration during a stretch of failures (0 % N == 0).
            if processed % MASS_ACTION_PROGRESS_INTERVAL == 0 and processed < len(capped):
                await interaction.edit_original_response(
                    embed=self.bot.embeds.info_embed(f"{verb}...", f"{processed}/{len(capped)} processed so far.")
                )
            await asyncio.sleep(MASS_ACTION_DELAY_SECONDS)
        return done

    @app_commands.command(name="role", description="Add or remove a role for every non-bot member.")
    @app_commands.describe(action="add or remove", role="The role to apply")
    @app_commands.choices(action=ROLE_ACTION_CHOICES)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def role(self, interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role) -> None:
        if role >= interaction.guild.me.top_role:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "That role is above (or equal to) my highest role.", ephemeral=True)
            return

        adding = action.value == "add"
        targets = [m for m in interaction.guild.members if not m.bot and (role not in m.roles if adding else role in m.roles)]

        if not targets:
            await self.bot.embeds.info_interaction(interaction, "Nothing To Do", "No members matched.", ephemeral=True)
            return
        description = f"{'Adding' if adding else 'Removing'} {role.mention} for {min(len(targets), MAX_MASS_ACTION_TARGETS)} member(s)."
        if len(targets) > MAX_MASS_ACTION_TARGETS:
            description += (
                f"\n⚠️ {len(targets)} members match, only the first {MAX_MASS_ACTION_TARGETS}"
                " will be processed in this run. Re-run to continue."
            )

        await interaction.response.send_message(embed=self.bot.embeds.info_embed("Starting...", description))

        reason = f"Mass role {'add' if adding else 'remove'} by {interaction.user}"
        action_fn = (lambda m: m.add_roles(role, reason=reason)) if adding else (lambda m: m.remove_roles(role, reason=reason))
        done = await self._mass_apply(interaction, targets, action_fn, verb="Updating roles")

        await interaction.edit_original_response(
            embed=self.bot.embeds.success_embed("Done", f"{'Added' if adding else 'Removed'} {role.mention} for {done} member(s).")
        )

    @app_commands.command(name="autorole", description="Manage roles automatically assigned to new members.")
    @app_commands.describe(action="add, remove, or apply to everyone right now", role="The role (not needed for 'apply')")
    @app_commands.choices(action=AUTOROLE_ACTION_CHOICES)
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def autorole(self, interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role | None = None) -> None:
        settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        autoroles = settings.get("autoroles", []) or []

        if action.value == "add":
            if role is None:
                await self.bot.embeds.error_interaction(interaction, "Missing Role", "Provide a role to add.", ephemeral=True)
                return
            if role.id in autoroles:
                await self.bot.embeds.warning_interaction(interaction, "Already An Autorole", f"{role.mention} is already an autorole.", ephemeral=True)
                return
            autoroles.append(role.id)
            settings["autoroles"] = autoroles
            save_guild_settings(self.bot.storage_path, interaction.guild.id, settings)
            await self.bot.embeds.success_interaction(interaction, "Autorole Added", f"{role.mention} will now be given to new members.", ephemeral=True)
            return

        if action.value == "remove":
            if role is None:
                await self.bot.embeds.error_interaction(interaction, "Missing Role", "Provide a role to remove.", ephemeral=True)
                return
            if role.id not in autoroles:
                await self.bot.embeds.warning_interaction(interaction, "Not An Autorole", f"{role.mention} isn't currently an autorole.", ephemeral=True)
                return
            autoroles.remove(role.id)
            settings["autoroles"] = autoroles
            save_guild_settings(self.bot.storage_path, interaction.guild.id, settings)
            await self.bot.embeds.success_interaction(interaction, "Autorole Removed", f"{role.mention} will no longer be given to new members.", ephemeral=True)
            return

        # apply
        if not autoroles:
            await self.bot.embeds.info_interaction(interaction, "No Autoroles Configured", "Add one with `/autorole add` first.", ephemeral=True)
            return

        roles = [interaction.guild.get_role(rid) for rid in autoroles]
        roles = [r for r in roles if r is not None and r < interaction.guild.me.top_role]
        if not roles:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", "None of the configured autoroles are below my highest role.", ephemeral=True)
            return

        targets = [m for m in interaction.guild.members if not m.bot and any(r not in m.roles for r in roles)]
        if not targets:
            await self.bot.embeds.info_interaction(interaction, "Nothing To Do", "Everyone already has the configured autoroles.", ephemeral=True)
            return

        await interaction.response.send_message(embed=self.bot.embeds.info_embed("Starting...", f"Applying autoroles to {min(len(targets), MAX_MASS_ACTION_TARGETS)} member(s)."))

        async def _apply(member: discord.Member) -> None:
            missing = [r for r in roles if r not in member.roles]
            if missing:
                await member.add_roles(*missing, reason="Autorole apply")

        done = await self._mass_apply(interaction, targets, _apply, verb="Applying autoroles")
        await interaction.edit_original_response(embed=self.bot.embeds.success_embed("Done", f"Applied autoroles to {done} member(s)."))

    @app_commands.command(name="prune", description="Mass-kick members matching a role criteria.")
    @app_commands.describe(
        mode="default = only-default-role members, specific = has this role, exclude = lacks this role",
        role="Required for 'specific' and 'exclude'",
    )
    @app_commands.choices(mode=PRUNE_MODE_CHOICES)
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    @app_commands.guild_only()
    async def prune(self, interaction: discord.Interaction, mode: app_commands.Choice[str], role: discord.Role | None = None) -> None:
        mode_value = mode.value
        if mode_value in ("specific", "exclude") and role is None:
            await self.bot.embeds.error_interaction(interaction, "Missing Role", "That mode requires a role.", ephemeral=True)
            return

        non_bot_members = [m for m in interaction.guild.members if not m.bot]

        if mode_value == "default":
            settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
            default_role_ids = set(settings.get("autoroles", []) or [])
            kick_list = [m for m in non_bot_members if all(r.is_default() or r.id in default_role_ids for r in m.roles)]
            reason_label = "having only default roles"
        elif mode_value == "specific":
            kick_list = [m for m in non_bot_members if role in m.roles]
            reason_label = f"having the role {role.name}"
        else:
            kick_list = [m for m in non_bot_members if role not in m.roles]
            reason_label = f"not having the role {role.name}"

        if not kick_list:
            await self.bot.embeds.info_interaction(interaction, "Nothing To Do", "No members matched that criteria.", ephemeral=True)
            return

        capped = kick_list[:MAX_MASS_ACTION_TARGETS]
        view = ConfirmView(author_id=interaction.user.id)
        await interaction.response.send_message(
            embed=self.bot.embeds.warning_embed(
                "Confirm Prune",
                f"About to kick **{len(capped)}** member(s) for {reason_label}"
                + (f" (of {len(kick_list)} matched, capped at {MAX_MASS_ACTION_TARGETS})" if len(kick_list) > MAX_MASS_ACTION_TARGETS else "")
                + ".\n\nThis cannot be undone.",
            ),
            view=view,
        )
        view.message = await interaction.original_response()
        await view.wait()

        if not view.value:
            await interaction.edit_original_response(embed=self.bot.embeds.info_embed("Cancelled", "No members were kicked."), view=None)
            return

        reason = f"Prune ({reason_label}) by {interaction.user}"
        done = await self._mass_apply(interaction, capped, lambda m: m.kick(reason=reason), verb="Pruning")
        await interaction.edit_original_response(embed=self.bot.embeds.success_embed("Prune Complete", f"Kicked {done} member(s) for {reason_label}."), view=None)

    # ---------- listeners ----------

    @staticmethod
    def _role_is_elevated(role: discord.Role) -> bool:
        """Roles carrying any 2FA-gated moderation permission (admin, manage
        roles, kick/ban, ...). Sticky roles must not hand these back on rejoin:
        a mod demoted by kick would regain their mod role just by rejoining."""
        return (role.permissions.value & discord.Permissions.elevated().value) != 0

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        if member.bot:
            return

        settings = get_guild_settings(self.bot.storage_path, member.guild.id)
        roles_to_add: list[discord.Role] = []

        if settings.get("sticky_roles", False):
            stored_role_ids = pop_sticky_roles(self.bot.storage_path, member.guild.id, member.id)
            for role_id in stored_role_ids:
                role = member.guild.get_role(role_id)
                if (
                    role is not None
                    and role < member.guild.me.top_role
                    and not self._role_is_elevated(role)  # covers rows stored before this filter existed
                    and role not in roles_to_add
                ):
                    roles_to_add.append(role)

        for role_id in settings.get("autoroles", []) or []:
            role = member.guild.get_role(role_id)
            if role is not None and role < member.guild.me.top_role and role not in roles_to_add:
                roles_to_add.append(role)

        if not roles_to_add:
            return

        try:
            await member.add_roles(*roles_to_add, reason="Autorole / sticky roles on join")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        if member.bot:
            return

        settings = get_guild_settings(self.bot.storage_path, member.guild.id)
        if not settings.get("sticky_roles", False):
            return

        role_ids = [r.id for r in member.roles if not r.is_default() and not r.managed and not self._role_is_elevated(r)]
        if role_ids:
            save_sticky_roles(self.bot.storage_path, member.guild.id, member.id, role_ids)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Moderation(bot))
