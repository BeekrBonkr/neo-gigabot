from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import cases as case_store
from utils.website import site_link


def _when(created_at: str) -> str:
    """A stored UTC timestamp as a Discord relative time."""
    try:
        moment = discord.utils.parse_time(created_at.replace(" ", "T") + "+00:00")
    except (ValueError, AttributeError):
        return created_at
    return discord.utils.format_dt(moment, "R")


class Cases(commands.Cog):
    """A member's moderation history, and private notes moderators leave on them."""

    note = app_commands.Group(
        name="note", description="Private moderator notes on a member.", guild_only=True,
        default_permissions=discord.Permissions(moderate_members=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="modhistory", description="Show a member's moderation history and moderator notes.")
    @app_commands.describe(user="Whose history to show")
    @app_commands.checks.has_permissions(moderate_members=True)
    @app_commands.guild_only()
    async def modhistory(self, interaction: discord.Interaction, user: discord.User) -> None:
        storage, guild_id = self.bot.storage_path, interaction.guild.id
        cases = case_store.list_cases(storage, guild_id, user.id, limit=15)
        notes = case_store.list_notes(storage, guild_id, user.id)
        total = case_store.count_cases(storage, guild_id, user.id)

        case_lines = [
            f"`#{case['id']}` **{case['action']}** {_when(case['created_at'])} by <@{case['moderator_id']}>"
            + (f" ({case['duration']})" if case["duration"] else "")
            + (f"\n> {case['reason'][:150]}" if case["reason"] else "")
            for case in cases
        ]
        note_lines = [f"`#{note['id']}` {_when(note['created_at'])} by <@{note['author_id']}>\n> {note['note'][:200]}" for note in notes[:8]]
        fields = [self.bot.embeds.field(f"Cases ({total})", "\n".join(case_lines)[:1024] or "No moderation actions on record.")]
        fields.append(self.bot.embeds.field(f"Notes ({len(notes)})", "\n".join(note_lines)[:1024] or "No notes. Add one with `/note add`."))
        website = site_link(self.bot, "website", f"/app/{guild_id}/moderation?member={user.id}")
        embed = self.bot.embeds.info_embed(
            f"Moderation history - {user}", f"The full history is on the {website}." if website else "", fields=fields,
            thumbnail_url=user.display_avatar.url,
        )
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    @note.command(name="add", description="Leave a private note on a member for other moderators.")
    @app_commands.describe(user="Who the note is about", text="The note (members never see it)")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def note_add(self, interaction: discord.Interaction, user: discord.User, text: app_commands.Range[str, 1, case_store.MAX_NOTE_LENGTH]) -> None:
        try:
            note_id = case_store.add_note(self.bot.storage_path, interaction.guild.id, user.id, interaction.user.id, text)
        except case_store.CaseError as exc:
            await self.bot.embeds.error_interaction(interaction, "Note Not Saved", str(exc), ephemeral=True)
            return
        await self.bot.embeds.success_interaction(interaction, "Note Saved", f"Note `#{note_id}` added for {user.mention}.", ephemeral=True)

    @note.command(name="remove", description="Remove a moderator note by its number.")
    @app_commands.describe(note_id="The note's number, shown in /modhistory")
    @app_commands.checks.has_permissions(moderate_members=True)
    async def note_remove(self, interaction: discord.Interaction, note_id: int) -> None:
        removed = case_store.remove_note(self.bot.storage_path, interaction.guild.id, note_id)
        if removed is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "There is no note with that number in this server.", ephemeral=True)
            return
        await self.bot.embeds.success_interaction(interaction, "Note Removed", f"Removed note `#{note_id}`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Cases(bot))
