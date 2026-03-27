from __future__ import annotations

from discord.ext import commands

from utils.settings import get_guild_settings, update_guild_settings


class Settings(commands.Cog):
    """Per-guild settings and configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.group(name="settings", invoke_without_command=True)
    async def settings_group(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        settings = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        await ctx.send(f"Current prefix: `{settings['prefix']}`")

    @settings_group.command(name="prefix")
    @commands.has_permissions(administrator=True)
    async def set_prefix(self, ctx: commands.Context, *, prefix: str) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        update_guild_settings(self.bot.storage_path, ctx.guild.id, {"prefix": prefix})
        await ctx.send(f"Prefix updated to `{prefix}`")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
