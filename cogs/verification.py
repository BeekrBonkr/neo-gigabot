from __future__ import annotations

import logging
from datetime import timedelta

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import get_guild_settings, update_guild_settings
from utils.website import site_link

LOGGER = logging.getLogger(__name__)
VERIFY_BUTTON_ID = "verification:verify"


def verification_problem(settings: dict, member_created_at, now=None) -> str | None:
    """Why this account can't verify yet, or None if it can. The role itself
    is checked separately, against the live server."""
    min_days = int(settings.get("verify_min_account_age_days", 0) or 0)
    if min_days > 0 and (now or discord.utils.utcnow()) - member_created_at < timedelta(days=min_days):
        return f"Your Discord account has to be at least {min_days} day(s) old to verify here. Try again once it is."
    return None


class VerifyView(discord.ui.View):
    """The one button under a verification message. It has a fixed custom_id
    and no timeout, so it keeps working after the bot restarts."""

    def __init__(self, cog: "Verification") -> None:
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Verify", emoji="✅", style=discord.ButtonStyle.success, custom_id=VERIFY_BUTTON_ID)
    async def verify(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await self.cog.handle_verify(interaction)


class Verification(commands.Cog):
    """A "click to verify" gate: a posted button gives members a role, with an
    optional minimum account age. Give that role access to your channels and
    take it away from @everyone to close the server to unverified accounts."""

    verification = app_commands.Group(
        name="verification", description="Set up the click-to-verify gate.", guild_only=True,
        default_permissions=discord.Permissions(administrator=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self.bot.add_view(VerifyView(self))

    def _role(self, guild: discord.Guild, settings: dict) -> tuple[discord.Role | None, str]:
        """The role verification grants, or None and what is wrong with the setup."""
        raw = str(settings.get("verify_role_id") or "")
        role = guild.get_role(int(raw)) if raw.isdigit() else None
        if role is None:
            return None, "Verification isn't set up yet. An admin needs to choose the role with `/verification role`."
        if role.managed or role.is_default():
            return None, f"The verification role ({role.name}) can't be given out by a bot."
        if guild.me is None or not guild.me.guild_permissions.manage_roles or role >= guild.me.top_role:
            return None, f"I can't give out {role.name}: it has to be below my highest role, and I need Manage Roles."
        return role, ""

    async def handle_verify(self, interaction: discord.Interaction) -> None:
        guild, member = interaction.guild, interaction.user
        if guild is None or not isinstance(member, discord.Member):
            return
        settings = get_guild_settings(self.bot.storage_path, guild.id)
        role, problem = self._role(guild, settings)
        problem = problem or verification_problem(settings, member.created_at) or ""
        if problem:
            await interaction.response.send_message(problem, ephemeral=True)
            return
        if role in member.roles:
            await interaction.response.send_message("You're already verified.", ephemeral=True)
            return
        try:
            await member.add_roles(role, reason="Passed the verification button")
        except discord.HTTPException:
            await interaction.response.send_message("I couldn't give you the role. Please tell a moderator.", ephemeral=True)
            return
        await interaction.response.send_message(f"You're verified. Welcome to **{guild.name}**!", ephemeral=True)

    @verification.command(name="role", description="Choose the role members get when they verify.")
    @app_commands.describe(role="The role to grant", min_account_age_days="Refuse accounts younger than this many days (0 for no limit)")
    @app_commands.checks.has_permissions(administrator=True)
    async def set_role(self, interaction: discord.Interaction, role: discord.Role, min_account_age_days: app_commands.Range[int, 0, 365] = 0) -> None:
        update_guild_settings(self.bot.storage_path, interaction.guild.id, {"verify_role_id": str(role.id), "verify_min_account_age_days": min_account_age_days})
        _, problem = self._role(interaction.guild, get_guild_settings(self.bot.storage_path, interaction.guild.id))
        text = f"Members who verify get {role.mention}." + (f" Accounts younger than {min_account_age_days} day(s) are refused." if min_account_age_days else "")
        if problem:
            text += f"\n⚠️ {problem}"
        text += "\nNow post the button with `/verification post`, and make sure only this role can see your channels."
        await self.bot.embeds.success_interaction(interaction, "Verification Role Set", text, ephemeral=True)

    @verification.command(name="post", description="Post the verification message with its button.")
    @app_commands.describe(channel="Where to post it (defaults to this channel)")
    @app_commands.checks.has_permissions(administrator=True)
    async def post(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None) -> None:
        target = channel or interaction.channel
        settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        _, problem = self._role(interaction.guild, settings)
        if problem:
            await self.bot.embeds.error_interaction(interaction, "Not Ready", problem, ephemeral=True)
            return
        embed = self.bot.embeds.info_embed("✅ Verification", str(settings.get("verify_message") or "Press the button below to verify."))
        try:
            await target.send(embed=embed, view=VerifyView(self))
        except (discord.HTTPException, AttributeError):
            await self.bot.embeds.error_interaction(interaction, "Could Not Post", "I can't send messages in that channel.", ephemeral=True)
            return
        website = site_link(self.bot, "website", f"/app/{interaction.guild.id}/settings")
        await self.bot.embeds.success_interaction(
            interaction, "Verification Posted",
            f"Posted in {target.mention}. The button keeps working for as long as the message exists."
            + (f"\nThe message text and account age limit can be changed on the {website}." if website else ""),
            ephemeral=True,
        )

    @verification.command(name="off", description="Turn verification off. Existing buttons stop working.")
    @app_commands.checks.has_permissions(administrator=True)
    async def off(self, interaction: discord.Interaction) -> None:
        update_guild_settings(self.bot.storage_path, interaction.guild.id, {"verify_role_id": ""})
        await self.bot.embeds.success_interaction(interaction, "Verification Off", "The verify button no longer grants a role. Members keep the roles they have.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Verification(bot))
