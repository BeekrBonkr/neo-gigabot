from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord.ext import commands

from utils import automod as rules
from utils.durations import parse_duration
from utils.settings import get_guild_settings
from utils.warnings import add_warning, count_warnings

LOGGER = logging.getLogger(__name__)
MAX_TIMEOUT = timedelta(days=28)
NOTICE_SECONDS = 8


class Automod(commands.Cog):
    """Deletes messages that break a server's automod rules and, depending on
    the rule's action, warns or mutes the author. Off until a rule is turned
    on in the settings."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.spam = rules.SpamTracker()

    def _exempt(self, message: discord.Message, settings: dict) -> bool:
        author = message.author
        if not isinstance(author, discord.Member) or author.bot:
            return True
        if author.guild_permissions.manage_messages or author.id == message.guild.owner_id:
            return True
        exempt_roles = {int(r) for r in settings.get("automod_exempt_roles") or [] if str(r).isdigit()}
        if exempt_roles and any(role.id in exempt_roles for role in author.roles):
            return True
        exempt_channels = {int(c) for c in settings.get("automod_exempt_channels") or [] if str(c).isdigit()}
        channel = message.channel
        return channel.id in exempt_channels or getattr(channel, "parent_id", None) in exempt_channels

    async def check(self, message: discord.Message, *, edited: bool = False) -> str | None:
        """Apply the rules to one message. Returns the rule that fired."""
        if message.guild is None:
            return None
        settings = get_guild_settings(self.bot.storage_path, message.guild.id)
        if not rules.any_rule_enabled(settings) or self._exempt(message, settings):
            return None
        mention_count = len(message.raw_mentions) + len(message.raw_role_mentions) + (1 if message.mention_everyone else 0)
        rule = rules.content_violation(message.content, mention_count, settings)
        if rule is None and not edited and rules.rule_action(settings, "spam") != "off":
            if self.spam.hit(message.guild.id, message.author.id, int(settings.get("automod_spam_limit", 6) or 6)):
                rule = "spam"
        if rule is not None:
            await self._enforce(message, rule, rules.rule_action(settings, rule), settings)
        return rule

    async def _enforce(self, message: discord.Message, rule: str, action: str, settings: dict) -> None:
        guild, member = message.guild, message.author
        label = rules.RULE_LABELS[rule]
        try:
            await message.delete()
        except discord.HTTPException:
            pass  # already gone, or the bot can't delete here; the warn or mute still applies
        try:
            await message.channel.send(f"{member.mention}, {label} isn't allowed here.", delete_after=NOTICE_SECONDS,
                                       allowed_mentions=discord.AllowedMentions(users=[member]))
        except discord.HTTPException:
            pass

        moderation = self.bot.get_cog("Moderation")
        reason = f"Automod: {label}"
        if action == "warn":
            add_warning(self.bot.storage_path, guild.id, member.id, self.bot.user.id, reason)
            total = count_warnings(self.bot.storage_path, guild.id, member.id)
            if moderation is not None:
                await moderation._try_dm(
                    member, self.bot.embeds.warning_embed("You Were Warned", f"You were warned in **{guild.name}**.\nReason: {reason}"), guild_id=guild.id
                )
                await moderation._log_mod_action(
                    guild, event="warn", title="⚠️ Automod Warning", color=self.bot.embeds.warning_color,
                    target=member, moderator=self.bot.user, reason=reason,
                )
                await moderation.escalate_warning(guild, member, total)
        elif action == "mute":
            duration_text = str(settings.get("automod_mute_duration", "10m"))
            delta = min(parse_duration(duration_text) or timedelta(minutes=10), MAX_TIMEOUT)
            if member.top_role >= guild.me.top_role:
                return
            try:
                await member.timeout(delta, reason=reason)
            except discord.HTTPException:
                return
            if moderation is not None:
                await moderation._try_dm(
                    member, self.bot.embeds.warning_embed("You Were Muted", f"You were muted in **{guild.name}** for {duration_text}.\nReason: {reason}"), guild_id=guild.id
                )
                await moderation._log_mod_action(
                    guild, event="mute", title="🔇 Automod Mute", color=self.bot.embeds.warning_color,
                    target=member, moderator=self.bot.user, reason=reason, duration=duration_text,
                )

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        try:
            await self.check(message)
        except Exception:  # noqa: BLE001 - one bad message must never stop the listener
            LOGGER.exception("Automod failed on a message in guild %s", getattr(message.guild, "id", None))

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message) -> None:
        if before.content == after.content:
            return
        try:
            await self.check(after, edited=True)
        except Exception:  # noqa: BLE001
            LOGGER.exception("Automod failed on an edit in guild %s", getattr(after.guild, "id", None))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Automod(bot))
