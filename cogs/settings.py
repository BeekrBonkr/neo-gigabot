from __future__ import annotations

import discord
from discord.ext import commands

from utils.settings import (
    ALLOWED_PREFIX_CHARS,
    create_guild_settings,
    get_guild_settings,
    save_guild_settings,
    update_guild_settings,
)


class Settings(commands.Cog):
    """Per-guild settings and configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        create_guild_settings(self.bot.storage_path, guild.id)

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or self.bot.user is None:
            return

        mention_variants = {f"<@{self.bot.user.id}>", f"<@!{self.bot.user.id}>"}
        if message.content.strip() in mention_variants:
            settings = get_guild_settings(self.bot.storage_path, message.guild.id)
            prefix = settings.get("prefix", self.bot.config.default_prefix)
            await self.bot.embeds.info(
                message.channel,
                "Prefix Information",
                f"My prefix for this server is `{prefix}`.\nAdmins can change it with `{prefix}prefix <new_prefix>`.",
            )

    @commands.command(usage="on/off #channel")
    @commands.has_permissions(administrator=True)
    async def suggestion(
        self,
        ctx: commands.Context,
        option: str,
        *channels: discord.TextChannel,
    ) -> None:
        """Set or unset the suggestion channel."""
        if ctx.guild is None:
            await self.bot.embeds.error(ctx, "Server Only", "This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        option = option.lower()

        if option == "on":
            if not channels:
                await self.bot.embeds.error(
                    ctx,
                    "Missing Channels",
                    "Provide one or more channels to set as suggestion channels.",
                )
                return

            channel_ids = [str(channel.id) for channel in channels]
            server_data["suggestion_channel_ids"] = channel_ids
            save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
            await self.bot.embeds.success(
                ctx,
                "Suggestion Channels Updated",
                f"Set the suggestion channel(s) to {', '.join(channel.mention for channel in channels)}.",
            )
            return

        if option == "off":
            if server_data.get("suggestion_channel_ids"):
                server_data["suggestion_channel_ids"] = []
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await self.bot.embeds.success(
                    ctx,
                    "Suggestion Channels Cleared",
                    "The suggestion channel(s) have been unset.",
                )
            else:
                await self.bot.embeds.warning(
                    ctx,
                    "Nothing To Clear",
                    "There are no suggestion channels to unset.",
                )
            return

        await self.bot.embeds.error(
            ctx,
            "Invalid Option",
            "Please use `on` or `off`.",
        )

    @commands.command(usage="<new_prefix>")
    @commands.has_permissions(administrator=True)
    async def prefix(self, ctx: commands.Context, new_prefix: str) -> None:
        if ctx.guild is None:
            await self.bot.embeds.error(ctx, "Server Only", "This command can only be used in a server.")
            return

        if not new_prefix or not all(c in ALLOWED_PREFIX_CHARS for c in new_prefix):
            await self.bot.embeds.error(
                ctx,
                "Invalid Prefix",
                f"Only the following characters are allowed: {ALLOWED_PREFIX_CHARS}",
            )
            return

        update_guild_settings(self.bot.storage_path, ctx.guild.id, {"prefix": new_prefix})
        await self.bot.embeds.success(
            ctx,
            "Prefix Updated",
            f"Prefix updated to `{new_prefix}`.",
        )

    @commands.command(name="command", usage="block/allow <command_name>")
    @commands.has_permissions(administrator=True)
    async def command_permission(
        self,
        ctx: commands.Context,
        permission_type: str,
        command_name: str,
    ) -> None:
        """Allow or block a command in this server."""
        if ctx.guild is None:
            await self.bot.embeds.error(ctx, "Server Only", "This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        blocked_commands = server_data.get("blocked_commands", []) or []

        if command_name.startswith("!"):
            await self.bot.embeds.error(
                ctx,
                "Invalid Command Name",
                'Command name cannot start with the `!` character.',
            )
            return

        permission_type = permission_type.lower()
        if permission_type == "block":
            if command_name not in blocked_commands:
                blocked_commands.append(command_name)
                server_data["blocked_commands"] = blocked_commands
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await self.bot.embeds.success(
                    ctx,
                    "Command Blocked",
                    f"The `{command_name}` command has been blocked.",
                )
            else:
                await self.bot.embeds.warning(
                    ctx,
                    "Already Blocked",
                    f"The `{command_name}` command is already blocked.",
                )
            return

        if permission_type == "allow":
            if command_name in blocked_commands:
                blocked_commands.remove(command_name)
                server_data["blocked_commands"] = blocked_commands
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await self.bot.embeds.success(
                    ctx,
                    "Command Allowed",
                    f"The `{command_name}` command has been allowed.",
                )
            else:
                await self.bot.embeds.warning(
                    ctx,
                    "Not Blocked",
                    f"The `{command_name}` command is not blocked.",
                )
            return

        await self.bot.embeds.error(
            ctx,
            "Invalid Permission Type",
            'Please use `block` or `allow`.',
        )

    @commands.command(name="botchannel", usage="add/remove #channel")
    @commands.has_permissions(administrator=True)
    async def manage_bot_channel(
        self,
        ctx: commands.Context,
        action: str,
        channel: discord.TextChannel,
    ) -> None:
        if ctx.guild is None:
            await self.bot.embeds.error(ctx, "Server Only", "This command can only be used in a server.")
            return

        server_data = get_guild_settings(self.bot.storage_path, ctx.guild.id)
        bot_channels = server_data.get("bot_channels", []) or []
        action = action.lower()

        if action == "add":
            if channel.id not in bot_channels:
                bot_channels.append(channel.id)
                server_data["bot_channels"] = bot_channels
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await self.bot.embeds.success(
                    ctx,
                    "Bot Channel Added",
                    f"Added {channel.mention} to the bot channels list.",
                )
            else:
                await self.bot.embeds.warning(
                    ctx,
                    "Already Added",
                    f"{channel.mention} is already a bot channel.",
                )
            return

        if action == "remove":
            if channel.id in bot_channels:
                bot_channels.remove(channel.id)
                server_data["bot_channels"] = bot_channels
                save_guild_settings(self.bot.storage_path, ctx.guild.id, server_data)
                await self.bot.embeds.success(
                    ctx,
                    "Bot Channel Removed",
                    f"Removed {channel.mention} from the bot channels list.",
                )
            else:
                await self.bot.embeds.warning(
                    ctx,
                    "Not Present",
                    f"{channel.mention} is not currently a bot channel.",
                )
            return

        await self.bot.embeds.error(
            ctx,
            "Invalid Action",
            "Please use `add` or `remove`.",
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
