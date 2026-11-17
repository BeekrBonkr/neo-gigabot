from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import tags as store
from utils.website import site_link

NO_MENTIONS = discord.AllowedMentions.none()


async def tag_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    if interaction.guild is None:
        return []
    names = [tag["name"] for tag in store.list_tags(interaction.client.storage_path, interaction.guild.id)]
    needle = current.lower()
    return [app_commands.Choice(name=name, value=name) for name in names if needle in name][:25]


class Tags(commands.Cog):
    """Custom commands: admins save a reply under a name, and anyone can post
    it with /tag. Good for rules, FAQs and links people keep asking for."""

    tags = app_commands.Group(
        name="tags", description="Create and manage this server's custom commands.", guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="tag", description="Post one of this server's saved replies.")
    @app_commands.describe(name="Which tag to post")
    @app_commands.autocomplete(name=tag_autocomplete)
    @app_commands.checks.cooldown(2, 5.0)
    @app_commands.guild_only()
    async def tag(self, interaction: discord.Interaction, name: str) -> None:
        found = store.get_tag(self.bot.storage_path, interaction.guild.id, name, count_use=True)
        if found is None:
            names = [t["name"] for t in store.list_tags(self.bot.storage_path, interaction.guild.id)]
            hint = "Available: " + ", ".join(f"`{n}`" for n in names[:30]) if names else "This server has no tags yet."
            await self.bot.embeds.error_interaction(interaction, "No Such Tag", hint, ephemeral=True)
            return
        # Tags are written by admins but posted by anyone, so they never ping.
        await interaction.response.send_message(found["content"], allowed_mentions=NO_MENTIONS)

    @tags.command(name="list", description="List this server's tags.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_tags(self, interaction: discord.Interaction) -> None:
        found = store.list_tags(self.bot.storage_path, interaction.guild.id)
        lines = [f"`{t['name']}` · used {t['uses']} time(s)" for t in found] or ["No tags yet. Create one with `/tags set`."]
        website = site_link(self.bot, "website", f"/app/{interaction.guild.id}/tags")
        if website:
            lines.append(f"\n🌐 Tags are easier to write and edit on the {website}.")
        await self.bot.embeds.respond(interaction, embed=self.bot.embeds.info_embed(f"Tags ({len(found)}/{store.MAX_TAGS_PER_GUILD})", "\n".join(lines)), ephemeral=True)

    @tags.command(name="set", description="Create a tag, or replace the text of an existing one.")
    @app_commands.describe(name="Short name, like rules or faq", content="What the bot posts for this tag")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def set_tag(self, interaction: discord.Interaction, name: str, content: app_commands.Range[str, 1, store.MAX_CONTENT_LENGTH]) -> None:
        try:
            saved, created = store.set_tag(self.bot.storage_path, interaction.guild.id, name, content.replace("\\n", "\n"), interaction.user.id)
        except store.TagError as exc:
            await self.bot.embeds.error_interaction(interaction, "Tag Not Saved", str(exc), ephemeral=True)
            return
        await self.bot.embeds.success_interaction(
            interaction, "Tag Created" if created else "Tag Updated", f"Members can post it with `/tag {saved['name']}`.", ephemeral=True
        )

    @tags.command(name="delete", description="Delete a tag.")
    @app_commands.describe(name="The tag to delete")
    @app_commands.autocomplete(name=tag_autocomplete)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def delete_tag(self, interaction: discord.Interaction, name: str) -> None:
        removed = store.delete_tag(self.bot.storage_path, interaction.guild.id, name)
        if removed is None:
            await self.bot.embeds.error_interaction(interaction, "No Such Tag", "There is no tag with that name.", ephemeral=True)
            return
        await self.bot.embeds.success_interaction(interaction, "Tag Deleted", f"Deleted `{removed['name']}`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Tags(bot))
