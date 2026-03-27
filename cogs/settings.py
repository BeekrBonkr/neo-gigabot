from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import (
    ALLOWED_PREFIX_CHARS,
    create_guild_settings,
    get_guild_settings,
    save_guild_settings,
    update_guild_settings,
)


class Settings(commands.GroupCog, group_name="settings", group_description="Per-server configuration commands"):
    """Per-guild settings and configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        create_guild_settings(self.bot.storage_path, guild.id)

    @app_commands.command(name="show", description="Show this server's current bot settings.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def show(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        server_data = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        suggestion_ids = server_data.get("suggestion_channel_ids", []) or []
        bot_channel_ids = server_data.get("bot_channels", []) or []
        blocked_commands = server_data.get("blocked_commands", []) or []

        def format_channels(channel_ids: list[str] | list[int]) -> str:
            if not channel_ids:
                return "None"
            return ", ".join(f"<#{int(channel_id)}>" for channel_id in channel_ids)

        fields = [
            self.bot.embeds.field("Prefix", f"`{server_data.get('prefix', self.bot.config.default_prefix)}`", True),
            self.bot.embeds.field("Suggestion Channels", format_channels(suggestion_ids)),
            self.bot.embeds.field("Bot Channels", format_channels(bot_channel_ids)),
            self.bot.embeds.field(
                "Blocked Commands",
                ", ".join(f"`{name}`" for name in blocked_commands) if blocked_commands else "None",
            ),
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Server Settings",
            description=f"Settings for **{interaction.guild.name}**.",
            fields=fields,
            ephemeral=True,
        )

    @app_commands.command(name="suggestion", description="Configure suggestion channels for this server.")
    @app_commands.describe(option="Turn suggestion channels on or off", channels="One or more channels to use when turning this on")
    @app_commands.choices(option=[
        app_commands.Choice(name="on", value="on"),
        app_commands.Choice(name="off", value="off"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def suggestion(
        self,
        interaction: discord.Interaction,
        option: app_commands.Choice[str],
        channels: str | None = None,
    ) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Server Only", "This command can only be used in a server.", ephemeral=True)
            return

        server_data = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        selected = option.value.lower()

        if selected == "on":
            if not channels:
                await self.bot.embeds.error_interaction(
                    interaction,
                    "Missing Channels",
                    "Provide one or more channels, like `#suggestions #staff-suggestions`.",
                    ephemeral=True,
                )
                return

            resolved_channels = []
            for token in channels.split():
                channel_id = token.strip().removeprefix("<#").removesuffix(">")
                if channel_id.isdigit():
                    channel = interaction.guild.get_channel(int(channel_id))
                    if isinstance(channel, discord.TextChannel):
                        resolved_channels.append(channel)

            if not resolved_channels:
                await self.bot.embeds.error_interaction(
                    interaction,
                    "No Valid Channels",
                    "I could not resolve any valid text channels from that input.",
                    ephemeral=True,
                )
                return

            channel_ids = [str(channel.id) for channel in resolved_channels]
            server_data["suggestion_channel_ids"] = channel_ids
            save_guild_settings(self.bot.storage_path, interaction.guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Suggestion Channels Updated",
                f"Set the suggestion channel(s) to {', '.join(channel.mention for channel in resolved_channels)}.",
                ephemeral=True,
            )
            return

        if server_data.get("suggestion_channel_ids"):
            server_data["suggestion_channel_ids"] = []
            save_guild_settings(self.bot.storage_path, interaction.guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Suggestion Channels Cleared",
                "The suggestion channel(s) have been unset.",
                ephemeral=True,
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nothing To Clear",
                "There are no suggestion channels to unset.",
                ephemeral=True,
            )

    @app_commands.command(name="prefix", description="Update this server's stored prefix setting.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def prefix(self, interaction: discord.Interaction, new_prefix: str) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Server Only", "This command can only be used in a server.", ephemeral=True)
            return

        if not new_prefix or not all(c in ALLOWED_PREFIX_CHARS for c in new_prefix):
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Prefix",
                f"Only the following characters are allowed: {ALLOWED_PREFIX_CHARS}",
                ephemeral=True,
            )
            return

        update_guild_settings(self.bot.storage_path, interaction.guild.id, {"prefix": new_prefix})
        await self.bot.embeds.success_interaction(
            interaction,
            "Prefix Updated",
            f"Stored prefix updated to `{new_prefix}`. This bot now uses slash commands only, so this is mainly for legacy compatibility.",
            ephemeral=True,
        )

    @app_commands.command(name="command", description="Allow or block a command in this server.")
    @app_commands.choices(permission_type=[
        app_commands.Choice(name="block", value="block"),
        app_commands.Choice(name="allow", value="allow"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def command_permission(
        self,
        interaction: discord.Interaction,
        permission_type: app_commands.Choice[str],
        command_name: str,
    ) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Server Only", "This command can only be used in a server.", ephemeral=True)
            return

        server_data = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        blocked_commands = server_data.get("blocked_commands", []) or []

        if command_name.startswith("/") or command_name.startswith("!"):
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Command Name",
                "Command name should not start with `/` or `!`.",
                ephemeral=True,
            )
            return

        selected = permission_type.value.lower()
        if selected == "block":
            if command_name not in blocked_commands:
                blocked_commands.append(command_name)
                server_data["blocked_commands"] = blocked_commands
                save_guild_settings(self.bot.storage_path, interaction.guild.id, server_data)
                await self.bot.embeds.success_interaction(
                    interaction,
                    "Command Blocked",
                    f"The `{command_name}` command has been blocked.",
                    ephemeral=True,
                )
            else:
                await self.bot.embeds.warning_interaction(
                    interaction,
                    "Already Blocked",
                    f"The `{command_name}` command is already blocked.",
                    ephemeral=True,
                )
            return

        if command_name in blocked_commands:
            blocked_commands.remove(command_name)
            server_data["blocked_commands"] = blocked_commands
            save_guild_settings(self.bot.storage_path, interaction.guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Command Allowed",
                f"The `{command_name}` command has been allowed.",
                ephemeral=True,
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Not Blocked",
                f"The `{command_name}` command is not blocked.",
                ephemeral=True,
            )

    @app_commands.command(name="botchannel", description="Add or remove a bot channel for this server.")
    @app_commands.choices(action=[
        app_commands.Choice(name="add", value="add"),
        app_commands.Choice(name="remove", value="remove"),
    ])
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_bot_channel(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel,
    ) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Server Only", "This command can only be used in a server.", ephemeral=True)
            return

        server_data = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        bot_channels = server_data.get("bot_channels", []) or []
        selected = action.value.lower()

        if selected == "add":
            if channel.id not in bot_channels:
                bot_channels.append(channel.id)
                server_data["bot_channels"] = bot_channels
                save_guild_settings(self.bot.storage_path, interaction.guild.id, server_data)
                await self.bot.embeds.success_interaction(
                    interaction,
                    "Bot Channel Added",
                    f"Added {channel.mention} to the bot channels list.",
                    ephemeral=True,
                )
            else:
                await self.bot.embeds.warning_interaction(
                    interaction,
                    "Already Added",
                    f"{channel.mention} is already a bot channel.",
                    ephemeral=True,
                )
            return

        if channel.id in bot_channels:
            bot_channels.remove(channel.id)
            server_data["bot_channels"] = bot_channels
            save_guild_settings(self.bot.storage_path, interaction.guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Bot Channel Removed",
                f"Removed {channel.mention} from the bot channels list.",
                ephemeral=True,
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Not Present",
                f"{channel.mention} is not currently a bot channel.",
                ephemeral=True,
            )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
