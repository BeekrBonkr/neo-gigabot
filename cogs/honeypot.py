from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils import honeypot as store
from utils.modules import enabled_in
from utils.settings import get_guild_settings, update_guild_settings

LOGGER = logging.getLogger(__name__)
ACTIONS = {
    "ban": ("banned", "🍯 Honeypot Ban", "ban"),
    "softban": ("kicked and their messages deleted", "🍯 Honeypot Softban", "kick"),
    "kick": ("kicked", "🍯 Honeypot Kick", "kick"),
}
NOTICE = (
    "**Do not post in this channel.**\n"
    "It exists to catch spam bots and hacked accounts, which post in every channel they can see. "
    "Anyone who sends a message here is {outcome} automatically."
)
NOTICE_OUTCOMES = {"ban": "banned from the server", "softban": "kicked from the server", "kick": "kicked from the server"}
COUNT_WORDS = {"ban": "banned", "softban": "removed", "kick": "kicked"}
DEFAULT_CHANNEL_NAME = "do-not-post-here"
CHANNEL_TOPIC = "Do not post here. This channel catches spam bots: anyone who sends a message is removed automatically."


def notice_text(action: str, caught: int) -> str:
    """The body of the notice message, with the running count."""
    action = action if action in ACTIONS else "ban"
    return (NOTICE.format(outcome=NOTICE_OUTCOMES[action])
            + f"\n\n🍯 **{caught}** account{'' if caught == 1 else 's'} {COUNT_WORDS[action]} by this channel so far.")


def clean_channel_name(name: str) -> str:
    """A name Discord accepts for a text channel, or the default."""
    cleaned = "-".join(str(name or "").lower().split())[:90].strip("-")
    return cleaned or DEFAULT_CHANNEL_NAME


class Honeypot(commands.Cog):
    """A trap channel for spam bots: real members have no reason to post in it,
    so anyone who does is banned (or softbanned, or kicked) on the spot and a
    day of their messages is cleaned up. Moderators and bots are exempt."""

    honeypot = app_commands.Group(
        name="honeypot", description="A trap channel that removes spam accounts.", guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    def _trapped(self, message: discord.Message, settings: dict) -> bool:
        raw = str(settings.get("honeypot_channel_id") or "")
        if not enabled_in(settings, "honeypot") or not raw.isdigit() or message.channel.id != int(raw):
            return False
        author = message.author
        if not isinstance(author, discord.Member) or author.bot:
            return False  # other bots are added by admins on purpose; webhooks aren't members at all
        if author.id == message.guild.owner_id:
            return False
        if author.id in {int(u) for u in settings.get("honeypot_exempt_users") or [] if str(u).isdigit()}:
            return False
        exempt_roles = {int(r) for r in settings.get("honeypot_exempt_roles") or [] if str(r).isdigit()}
        if exempt_roles and any(role.id in exempt_roles for role in getattr(author, "roles", [])):
            return False
        permissions = author.guild_permissions
        return not (permissions.administrator or permissions.manage_messages or permissions.ban_members)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None:
            return
        settings = get_guild_settings(self.bot.storage_path, message.guild.id)
        if not self._trapped(message, settings):
            return
        try:
            await self.spring(message, str(settings.get("honeypot_action", "ban")))
        except Exception:  # noqa: BLE001 - never let one message stop the listener
            LOGGER.exception("Honeypot failed in guild %s", message.guild.id)

    async def spring(self, message: discord.Message, action: str) -> None:
        guild, member = message.guild, message.author
        outcome, title, event = ACTIONS.get(action, ACTIONS["ban"])
        reason = f"Posted in the honeypot channel #{getattr(message.channel, 'name', message.channel.id)}"
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        if guild.me is None or member.top_role >= guild.me.top_role:
            LOGGER.warning("Honeypot could not remove %s in guild %s: their role is not below the bot's", member.id, guild.id)
            return
        try:
            if action == "kick":
                await member.kick(reason=reason)
            else:
                await guild.ban(member, reason=reason, delete_message_seconds=86400)
                if action == "softban":
                    await guild.unban(member, reason=reason)
        except discord.HTTPException as exc:
            LOGGER.warning("Honeypot could not remove %s in guild %s: %s", member.id, guild.id, exc)
            return
        LOGGER.info("Honeypot: %s was %s in guild %s", member.id, outcome, guild.id)
        store.record_catch(self.bot.storage_path, guild.id)
        try:
            await self.refresh_notice(guild, action)
        except discord.HTTPException as exc:
            LOGGER.warning("Honeypot could not update its notice in guild %s: %s", guild.id, exc)
        moderation = self.bot.get_cog("Moderation")
        if moderation is not None:
            await moderation._log_mod_action(
                guild, event=event, title=title, color=self.bot.embeds.error_color,
                target=member, moderator=self.bot.user, reason=reason,
            )

    def _notice_embed(self, guild_id: int, action: str) -> discord.Embed:
        caught = store.get_state(self.bot.storage_path, guild_id)["caught"]
        return self.bot.embeds.error_embed("⛔ Honeypot", notice_text(action, caught))

    async def post_notice(self, channel, action: str | None = None) -> None:
        """Post the warning (with the count) in the honeypot channel, replacing
        the previous one. Raises discord.HTTPException if it can't be posted."""
        guild = channel.guild
        action = action or str(get_guild_settings(self.bot.storage_path, guild.id).get("honeypot_action", "ban"))
        previous = store.get_state(self.bot.storage_path, guild.id)
        message = await channel.send(embed=self._notice_embed(guild.id, action))
        store.set_notice(self.bot.storage_path, guild.id, channel.id, message.id)
        old_channel = guild.get_channel(previous["channel_id"]) if previous["message_id"] else None
        if old_channel is not None:
            try:
                await old_channel.get_partial_message(previous["message_id"]).delete()
            except discord.HTTPException:
                pass  # already gone

    async def refresh_notice(self, guild, action: str) -> None:
        """Update the count on the notice. A notice someone deleted is posted again."""
        state = store.get_state(self.bot.storage_path, guild.id)
        channel = guild.get_channel(state["channel_id"]) if state["message_id"] else None
        if channel is None:
            return  # no notice was ever posted here; leave the channel as the admins set it up
        try:
            await channel.get_partial_message(state["message_id"]).edit(embed=self._notice_embed(guild.id, action))
        except discord.NotFound:
            await self.post_notice(channel, action)

    async def create_channel(self, guild, name: str, created_by: str) -> discord.TextChannel:
        """Make a honeypot channel at the top of the server, switch the honeypot
        on for it and post the notice. Raises discord.HTTPException on failure."""
        channel = await guild.create_text_channel(
            clean_channel_name(name), topic=CHANNEL_TOPIC, position=0, reason=f"Honeypot channel created by {created_by}",
        )
        update_guild_settings(self.bot.storage_path, guild.id, {"honeypot_channel_id": str(channel.id), "honeypot_enabled": True})
        await self.post_notice(channel)
        return channel

    @honeypot.command(name="create", description="Create a honeypot channel at the top of the server and turn the honeypot on.")
    @app_commands.describe(name="Name of the new channel (default: do-not-post-here)")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(ban_members=True, manage_messages=True, manage_channels=True)
    async def create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 90] = DEFAULT_CHANNEL_NAME) -> None:
        await interaction.response.defer(ephemeral=True)
        try:
            channel = await self.create_channel(interaction.guild, name, f"{interaction.user} ({interaction.user.id})")
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=self.bot.embeds.error_embed("Not Created", f"Discord refused: {exc}"), ephemeral=True)
            return
        await interaction.followup.send(embed=self.bot.embeds.success_embed(
            "Honeypot Created", f"{channel.mention} is the honeypot now, with a warning posted in it. Anyone who posts there is removed; "
            "moderators, admins and bots are exempt."), ephemeral=True)

    @honeypot.command(name="set", description="Make a channel the honeypot. Anyone who posts in it is removed.")
    @app_commands.describe(channel="The trap channel (use one nobody needs)", action="What happens to whoever posts there (default: ban)")
    @app_commands.choices(action=[
        app_commands.Choice(name="Ban", value="ban"),
        app_commands.Choice(name="Softban (kick and delete their messages)", value="softban"),
        app_commands.Choice(name="Kick", value="kick"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.checks.bot_has_permissions(ban_members=True, manage_messages=True)
    async def set_channel(self, interaction: discord.Interaction, channel: discord.TextChannel, action: app_commands.Choice[str] | None = None) -> None:
        chosen = action.value if action else "ban"
        update_guild_settings(self.bot.storage_path, interaction.guild.id, {"honeypot_channel_id": str(channel.id), "honeypot_action": chosen})
        posted = True
        try:
            await self.post_notice(channel, chosen)
        except discord.HTTPException:
            posted = False
        await self.bot.embeds.success_interaction(
            interaction, "Honeypot Set",
            f"Anyone who posts in {channel.mention} will be **{ACTIONS[chosen][0]}**. Moderators, admins and bots are exempt.\n"
            + ("I posted a warning there so real members know to stay out." if posted else "⚠️ I couldn't post the warning there. Add one yourself so real members stay out.")
            + "\nPut the channel near the top of the list: spam bots post in the first channels they see.",
            ephemeral=True,
        )

    @honeypot.command(name="off", description="Turn the honeypot off.")
    @app_commands.checks.has_permissions(administrator=True)
    async def off(self, interaction: discord.Interaction) -> None:
        update_guild_settings(self.bot.storage_path, interaction.guild.id, {"honeypot_enabled": False})
        await self.bot.embeds.success_interaction(interaction, "Honeypot Off", "Posting in the honeypot channel no longer does anything. `/honeypot set` turns it back on.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Honeypot(bot))
