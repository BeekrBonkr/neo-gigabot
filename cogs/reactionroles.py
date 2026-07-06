from __future__ import annotations

import asyncio
import re

import discord
from discord import app_commands
from discord.ext import commands

from utils.discord_links import fetch_message_from_link
from utils.reactionroles import (
    ReactionRoleError,
    add_mapping,
    clear_message,
    get_role_for_reaction,
    list_for_guild,
    list_for_message,
    remove_mapping,
)

MAX_BATCH_PAIRS = 10
BUILDER_REACTION_TIMEOUT_SECONDS = 30.0
# "emoji=@Role" pairs, comma-separated. Using '=' (not ':') as the separator
# specifically because custom emoji already look like <:name:id> - a ':'
# separator would be ambiguous with that syntax.
BATCH_PAIR_RE = re.compile(r"([^,=]+?)\s*=\s*<@&(\d+)>")


def _parse_emoji(text: str) -> discord.PartialEmoji:
    return discord.PartialEmoji.from_str(text.strip())


class ReactionRoleBuilderView(discord.ui.View):
    """Guided flow for /reactionrole create: press Add Mapping, react to the
    message with the emoji you want, then pick the role from a dropdown -
    no message link or hand-typed emoji syntax needed. Reuses the cog's own
    _add_one() so it shares the exact same validation/error path as the
    manual /reactionrole add command."""

    def __init__(self, cog: "ReactionRoles", guild: discord.Guild, target_message: discord.Message, author_id: int) -> None:
        super().__init__(timeout=300.0)
        self.cog = cog
        self.guild = guild
        self.target_message = target_message
        self.author_id = author_id
        self.message: discord.Message | None = None
        self.mappings_added = 0
        self._build_idle_items()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This builder isn't yours.", ephemeral=True)
            return False
        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass

    def _build_idle_items(self) -> None:
        self.clear_items()
        add_btn = discord.ui.Button(label="Add Mapping", style=discord.ButtonStyle.primary)
        add_btn.callback = self._on_add_mapping
        done_btn = discord.ui.Button(label="Done", style=discord.ButtonStyle.secondary)
        done_btn.callback = self._on_done
        self.add_item(add_btn)
        self.add_item(done_btn)

    async def _on_done(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            embed=self.cog.bot.embeds.success_embed("Builder Closed", f"Added {self.mappings_added} mapping(s)."),
            view=self,
        )
        self.stop()

    async def _on_add_mapping(self, interaction: discord.Interaction) -> None:
        self.clear_items()
        await interaction.response.edit_message(
            embed=self.cog.bot.embeds.info_embed(
                "Waiting For Reaction",
                f"React to [the message]({self.target_message.jump_url}) with the emoji you want to map "
                f"({int(BUILDER_REACTION_TIMEOUT_SECONDS)}s)...",
            ),
            view=self,
        )

        def check(payload: discord.RawReactionActionEvent) -> bool:
            return payload.message_id == self.target_message.id and payload.user_id == self.author_id

        try:
            payload = await self.cog.bot.wait_for("raw_reaction_add", check=check, timeout=BUILDER_REACTION_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            self._build_idle_items()
            await interaction.edit_original_response(
                embed=self.cog.bot.embeds.warning_embed(
                    "Timed Out", "No reaction seen in time. Press **Add Mapping** to try again."
                ),
                view=self,
            )
            return

        emoji = payload.emoji
        try:
            await self.target_message.remove_reaction(emoji, discord.Object(id=self.author_id))
        except discord.HTTPException:
            pass

        role_select = discord.ui.RoleSelect(placeholder="Pick the role to grant for that emoji", min_values=1, max_values=1)

        async def _on_role(role_interaction: discord.Interaction) -> None:
            role = role_select.values[0]
            result = await self.cog._add_one(self.target_message, self.guild, str(emoji), role)
            if result.startswith("✅"):
                self.mappings_added += 1
            self._build_idle_items()
            await role_interaction.response.edit_message(
                embed=self.cog.bot.embeds.info_embed(
                    "Reaction Role Builder", f"{result}\n\nPress **Add Mapping** to add another, or **Done** to finish."
                ),
                view=self,
            )

        role_select.callback = _on_role
        self.clear_items()
        self.add_item(role_select)

        cancel_btn = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)

        async def _on_cancel(cancel_interaction: discord.Interaction) -> None:
            self._build_idle_items()
            await cancel_interaction.response.edit_message(
                embed=self.cog.bot.embeds.info_embed(
                    "Reaction Role Builder", "Cancelled that mapping. Press **Add Mapping** to try again, or **Done** to finish."
                ),
                view=self,
            )

        cancel_btn.callback = _on_cancel
        self.add_item(cancel_btn)

        await interaction.edit_original_response(
            embed=self.cog.bot.embeds.info_embed("Pick A Role", f"Got {emoji} - now pick the role to grant for it."),
            view=self,
        )


class ReactionRoles(
    commands.GroupCog,
    group_name="reactionrole",
    group_description="Set up roles members can self-assign by reacting to a message.",
):
    """Reaction roles, keyed off a message link rather than the old bot's
    interactive "react and I'll ask you which role" flow, which doesn't map
    to slash commands well."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _resolve_target_message(self, interaction: discord.Interaction, message_link: str) -> discord.Message | None:
        try:
            message = await fetch_message_from_link(self.bot, message_link, hint_guild=interaction.guild)
        except ValueError as exc:
            await self.bot.embeds.error_interaction(interaction, "Invalid Message Link", str(exc), ephemeral=True)
            return None

        if message.guild is None or message.guild.id != interaction.guild.id:
            await self.bot.embeds.error_interaction(
                interaction, "Wrong Server", "That message needs to be in this server.", ephemeral=True
            )
            return None
        return message

    def _role_usable(self, guild: discord.Guild, role: discord.Role) -> bool:
        return not role.is_default() and role < guild.me.top_role

    async def _add_one(self, message: discord.Message, guild: discord.Guild, emoji_text: str, role: discord.Role) -> str:
        """Add a single mapping to `message`. Returns a one-line result summary."""
        if not self._role_usable(guild, role):
            return f"❌ `{emoji_text}` → {role.mention}: role is @everyone or above my highest role."

        parsed_emoji = _parse_emoji(emoji_text)
        try:
            await message.add_reaction(parsed_emoji)
        except discord.HTTPException:
            return f"❌ `{emoji_text}`: not a valid/usable emoji."

        try:
            add_mapping(self.bot.storage_path, guild.id, message.id, message.channel.id, str(parsed_emoji), role.id)
        except ReactionRoleError as exc:
            try:
                await message.remove_reaction(parsed_emoji, self.bot.user)
            except discord.HTTPException:
                pass
            return f"❌ `{emoji_text}` → {role.mention}: {exc}"

        return f"✅ {parsed_emoji} → {role.mention}"

    @app_commands.command(name="create", description="Post a new reaction-role message and configure it step by step.")
    @app_commands.describe(
        title="Embed title",
        description="Embed description",
        color="Optional hex color, e.g. #5865F2",
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, add_reactions=True)
    @app_commands.guild_only()
    async def create(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 256],
        description: app_commands.Range[str, 1, 2000],
        color: str | None = None,
    ) -> None:
        parsed_color: discord.Color | None = None
        if color:
            try:
                parsed_color = discord.Color(int(color.lstrip("#"), 16))
            except ValueError:
                await self.bot.embeds.error_interaction(interaction, "Invalid Color", "Use a hex color like `#5865F2`.", ephemeral=True)
                return

        embed = self.bot.embeds.create(title=title, description=description, color=parsed_color)
        try:
            message = await interaction.channel.send(embed=embed)
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Missing Permission", "I can't send messages in this channel.", ephemeral=True)
            return

        view = ReactionRoleBuilderView(self, interaction.guild, message, interaction.user.id)
        await interaction.response.send_message(
            embed=self.bot.embeds.info_embed(
                "Reaction Role Builder",
                "Posted the message above. Press **Add Mapping**, then react to it with the emoji you want - "
                "you'll be asked which role to grant for it.",
            ),
            view=view,
            ephemeral=True,
        )
        view.message = await interaction.original_response()

    @app_commands.command(name="add", description="Map an emoji reaction on a message to a role.")
    @app_commands.describe(message_link="Link to the message", emoji="The emoji to react with", role="Role to grant")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, add_reactions=True)
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, message_link: str, emoji: str, role: discord.Role) -> None:
        message = await self._resolve_target_message(interaction, message_link)
        if message is None:
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        result = await self._add_one(message, interaction.guild, emoji, role)
        await interaction.followup.send(embed=self.bot.embeds.info_embed("Reaction Role", result))

    @app_commands.command(name="batch", description="Add multiple emoji->role mappings to one message at once.")
    @app_commands.describe(message_link="Link to the message", pairs="e.g. 👍=@Role1, 🎮=@Role2 (max 10, comma-separated)")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True, add_reactions=True)
    @app_commands.guild_only()
    async def batch(self, interaction: discord.Interaction, message_link: str, pairs: str) -> None:
        parsed_pairs = BATCH_PAIR_RE.findall(pairs)
        if not parsed_pairs:
            await self.bot.embeds.error_interaction(
                interaction, "Nothing Parsed", "Use the format `emoji=@Role, emoji=@Role`.", ephemeral=True
            )
            return
        if len(parsed_pairs) > MAX_BATCH_PAIRS:
            await self.bot.embeds.error_interaction(
                interaction, "Too Many Pairs", f"Max {MAX_BATCH_PAIRS} pairs per batch call.", ephemeral=True
            )
            return

        message = await self._resolve_target_message(interaction, message_link)
        if message is None:
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        results = []
        for emoji_text, role_id_text in parsed_pairs:
            role = interaction.guild.get_role(int(role_id_text))
            if role is None:
                results.append(f"❌ `{emoji_text.strip()}`: role not found.")
                continue
            results.append(await self._add_one(message, interaction.guild, emoji_text.strip(), role))

        await interaction.followup.send(embed=self.bot.embeds.info_embed("Reaction Roles (Batch)", "\n".join(results)))

    @app_commands.command(name="remove", description="Remove one emoji->role mapping from a message.")
    @app_commands.describe(message_link="Link to the message", emoji="The emoji to unmap")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, message_link: str, emoji: str) -> None:
        message = await self._resolve_target_message(interaction, message_link)
        if message is None:
            return

        parsed_emoji = _parse_emoji(emoji)
        removed = remove_mapping(self.bot.storage_path, message.id, str(parsed_emoji))
        if not removed:
            await self.bot.embeds.warning_interaction(interaction, "Not Mapped", f"`{emoji}` isn't mapped on that message.", ephemeral=True)
            return

        try:
            await message.clear_reaction(parsed_emoji)
        except discord.HTTPException:
            pass

        await self.bot.embeds.success_interaction(interaction, "Mapping Removed", f"Removed the mapping for {parsed_emoji}.", ephemeral=True)

    @app_commands.command(name="clear", description="Remove every reaction-role mapping from a message.")
    @app_commands.describe(message_link="Link to the message")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.checks.bot_has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def clear(self, interaction: discord.Interaction, message_link: str) -> None:
        message = await self._resolve_target_message(interaction, message_link)
        if message is None:
            return

        removed = clear_message(self.bot.storage_path, message.id)
        if not removed:
            await self.bot.embeds.warning_interaction(interaction, "Nothing To Clear", "That message has no mappings.", ephemeral=True)
            return

        for entry in removed:
            try:
                await message.clear_reaction(discord.PartialEmoji.from_str(entry["emoji"]))
            except discord.HTTPException:
                pass

        await self.bot.embeds.success_interaction(interaction, "Cleared", f"Removed {len(removed)} mapping(s) from that message.", ephemeral=True)

    @app_commands.command(name="list", description="List reaction-role mappings for this server, or one message.")
    @app_commands.describe(message_link="Optional: limit to one message")
    @app_commands.checks.has_permissions(manage_roles=True)
    @app_commands.guild_only()
    async def list_mappings(self, interaction: discord.Interaction, message_link: str | None = None) -> None:
        if message_link:
            message = await self._resolve_target_message(interaction, message_link)
            if message is None:
                return
            entries = list_for_message(self.bot.storage_path, message.id)
        else:
            entries = list_for_guild(self.bot.storage_path, interaction.guild.id)

        if not entries:
            await self.bot.embeds.info_interaction(interaction, "No Mappings", "No reaction roles are configured.", ephemeral=True)
            return

        lines = [
            f"`{entry['emoji']}` → <@&{entry['role_id']}> (message `{entry['message_id']}` in <#{entry['channel_id']}>)"
            for entry in entries[:25]
        ]
        embed = self.bot.embeds.info_embed(f"Reaction Roles ({len(entries)})", "\n".join(lines))
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    # ---------- listeners ----------

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None or payload.member is None or payload.member.bot:
            return

        role_id = get_role_for_reaction(self.bot.storage_path, payload.message_id, str(payload.emoji))
        if role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        role = guild.get_role(role_id)
        if role is None or role >= guild.me.top_role:
            return

        try:
            await payload.member.add_roles(role, reason="Reaction role")
        except discord.HTTPException:
            pass

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        if payload.guild_id is None:
            return

        role_id = get_role_for_reaction(self.bot.storage_path, payload.message_id, str(payload.emoji))
        if role_id is None:
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            return
        member = guild.get_member(payload.user_id)
        if member is None or member.bot:
            return
        role = guild.get_role(role_id)
        if role is None:
            return

        try:
            await member.remove_roles(role, reason="Reaction role removed")
        except discord.HTTPException:
            pass


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ReactionRoles(bot))
