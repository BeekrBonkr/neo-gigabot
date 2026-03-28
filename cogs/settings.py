from __future__ import annotations

from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import (
    ALLOWED_PREFIX_CHARS,
    create_guild_settings,
    get_guild_settings,
    normalize_command_name,
    normalize_id_list,
    reset_guild_settings,
    save_guild_settings,
    update_guild_settings,
)


class Settings(
    commands.GroupCog,
    group_name="settings",
    group_description="Per-server configuration commands",
):
    """Per-guild settings and configuration commands."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        create_guild_settings(self.bot.storage_path, guild.id)

    def _format_channels(self, channel_ids: Iterable[int | str]) -> str:
        normalized = normalize_id_list(channel_ids)
        if not normalized:
            return "None"
        return ", ".join(f"<#{channel_id}>" for channel_id in normalized)

    def _format_roles(self, role_ids: Iterable[int | str]) -> str:
        normalized = normalize_id_list(role_ids)
        if not normalized:
            return "None"
        return ", ".join(f"<@&{role_id}>" for role_id in normalized)

    def _format_commands(self, names: Iterable[str]) -> str:
        normalized = sorted({normalize_command_name(name) for name in names if str(name).strip()})
        if not normalized:
            return "None"
        return ", ".join(f"`/{name}`" for name in normalized)

    def _resolve_text_channels(
        self,
        guild: discord.Guild,
        raw_channels: str,
    ) -> list[discord.TextChannel]:
        resolved: list[discord.TextChannel] = []
        seen: set[int] = set()

        for token in raw_channels.split():
            channel_id = token.strip().removeprefix("<#").removesuffix(">")
            if not channel_id.isdigit():
                continue
            channel = guild.get_channel(int(channel_id))
            if not isinstance(channel, discord.TextChannel):
                continue
            if channel.id in seen:
                continue
            resolved.append(channel)
            seen.add(channel.id)

        return resolved

    @app_commands.command(name="show", description="Show this server's current bot settings.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def show(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        data = get_guild_settings(self.bot.storage_path, guild.id)
        fields = [
            self.bot.embeds.field(
                "Bot Channels",
                self._format_channels(data.get("bot_channels", [])),
                False,
            ),
            self.bot.embeds.field(
                "Suggestion Channels",
                self._format_channels(data.get("suggestion_channel_ids", [])),
                False,
            ),
            self.bot.embeds.field(
                "Blocked Commands",
                self._format_commands(data.get("blocked_commands", [])),
                False,
            ),
            self.bot.embeds.field(
                "Logging",
                "Enabled" if data.get("logging_enabled", False) else "Disabled",
                True,
            ),
            self.bot.embeds.field(
                "Logging Channel",
                self._format_channels([data.get("logging_channel_id", "")]),
                True,
            ),
            self.bot.embeds.field(
                "Welcome Channels",
                self._format_channels(data.get("welcome_channel_ids", [])),
                False,
            ),
            self.bot.embeds.field(
                "Autoroles",
                self._format_roles(data.get("autoroles", [])),
                False,
            ),
            self.bot.embeds.field(
                "Music DJ Mode",
                "Enabled" if data.get("music_dj_enabled", False) else "Disabled",
                True,
            ),
            self.bot.embeds.field(
                "Music DJ Role",
                f"`{data.get('music_dj_role_name', 'dj')}`",
                True,
            ),
            self.bot.embeds.field(
                "Music Default Volume",
                f"`{int(data.get('music_default_volume', 50))}%`",
                True,
            ),
            self.bot.embeds.field(
                "Legacy Prefix",
                f"`{data.get('prefix', '!')}`",
                True,
            ),
        ]

        await self.bot.embeds.respond(
            interaction,
            title="Server Settings",
            description=f"Settings for **{guild.name}**.",
            fields=fields,
            ephemeral=True,
        )

    @app_commands.command(name="reset", description="Reset this server's settings back to defaults.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def reset(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        reset_guild_settings(self.bot.storage_path, guild.id)
        await self.bot.embeds.success_interaction(
            interaction,
            "Settings Reset",
            "This server's settings were reset to defaults.",
            ephemeral=True,
        )

    @app_commands.command(name="suggestion", description="Configure suggestion channels for this server.")
    @app_commands.describe(
        option="Turn suggestion channels on or off",
        channels="One or more channels to use when turning this on",
    )
    @app_commands.choices(
        option=[
            app_commands.Choice(name="on", value="on"),
            app_commands.Choice(name="off", value="off"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def suggestion(
        self,
        interaction: discord.Interaction,
        option: app_commands.Choice[str],
        channels: str | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        server_data = get_guild_settings(self.bot.storage_path, guild.id)
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

            resolved_channels = self._resolve_text_channels(guild, channels)
            if not resolved_channels:
                await self.bot.embeds.error_interaction(
                    interaction,
                    "No Valid Channels",
                    "I could not resolve any valid text channels from that input.",
                    ephemeral=True,
                )
                return

            server_data["suggestion_channel_ids"] = [channel.id for channel in resolved_channels]
            save_guild_settings(self.bot.storage_path, guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Suggestion Channels Updated",
                f"Set the suggestion channel(s) to {', '.join(channel.mention for channel in resolved_channels)}.",
                ephemeral=True,
            )
            return

        if server_data.get("suggestion_channel_ids"):
            server_data["suggestion_channel_ids"] = []
            save_guild_settings(self.bot.storage_path, guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Suggestion Channels Cleared",
                "The suggestion channel(s) have been unset.",
                ephemeral=True,
            )
            return

        await self.bot.embeds.warning_interaction(
            interaction,
            "Nothing To Clear",
            "There are no suggestion channels to unset.",
            ephemeral=True,
        )

    @app_commands.command(name="prefix", description="Store a legacy text-command prefix for this server.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def prefix(self, interaction: discord.Interaction, new_prefix: str) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        if not new_prefix or not all(character in ALLOWED_PREFIX_CHARS for character in new_prefix):
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Prefix",
                f"Only the following characters are allowed: `{ALLOWED_PREFIX_CHARS}`",
                ephemeral=True,
            )
            return

        update_guild_settings(self.bot.storage_path, guild.id, {"prefix": new_prefix})
        await self.bot.embeds.success_interaction(
            interaction,
            "Prefix Updated",
            (
                f"Stored legacy prefix updated to `{new_prefix}`. "
                "The bot itself uses slash commands."
            ),
            ephemeral=True,
        )

    @app_commands.command(name="command", description="Allow or block a command in this server.")
    @app_commands.choices(
        permission_type=[
            app_commands.Choice(name="block", value="block"),
            app_commands.Choice(name="allow", value="allow"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def command_permission(
        self,
        interaction: discord.Interaction,
        permission_type: app_commands.Choice[str],
        command_name: str,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        normalized_name = normalize_command_name(command_name)
        if not normalized_name:
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Command Name",
                "Provide a real command name, like `ping` or `meme`.",
                ephemeral=True,
            )
            return

        server_data = get_guild_settings(self.bot.storage_path, guild.id)
        blocked_commands = {
            normalize_command_name(name) for name in server_data.get("blocked_commands", []) or []
        }

        if permission_type.value == "block":
            if normalized_name in blocked_commands:
                await self.bot.embeds.warning_interaction(
                    interaction,
                    "Already Blocked",
                    f"The `/{normalized_name}` command is already blocked.",
                    ephemeral=True,
                )
                return

            blocked_commands.add(normalized_name)
            server_data["blocked_commands"] = sorted(blocked_commands)
            save_guild_settings(self.bot.storage_path, guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Command Blocked",
                f"The `/{normalized_name}` command has been blocked.",
                ephemeral=True,
            )
            return

        if normalized_name not in blocked_commands:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Not Blocked",
                f"The `/{normalized_name}` command is not blocked.",
                ephemeral=True,
            )
            return

        blocked_commands.remove(normalized_name)
        server_data["blocked_commands"] = sorted(blocked_commands)
        save_guild_settings(self.bot.storage_path, guild.id, server_data)
        await self.bot.embeds.success_interaction(
            interaction,
            "Command Allowed",
            f"The `/{normalized_name}` command has been allowed.",
            ephemeral=True,
        )

    @app_commands.command(name="botchannel", description="Add or remove a bot channel for this server.")
    @app_commands.choices(
        action=[
            app_commands.Choice(name="add", value="add"),
            app_commands.Choice(name="remove", value="remove"),
            app_commands.Choice(name="clear", value="clear"),
        ]
    )
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def manage_bot_channel(
        self,
        interaction: discord.Interaction,
        action: app_commands.Choice[str],
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        server_data = get_guild_settings(self.bot.storage_path, guild.id)
        bot_channels = normalize_id_list(server_data.get("bot_channels", []) or [])

        if action.value == "clear":
            server_data["bot_channels"] = []
            save_guild_settings(self.bot.storage_path, guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Bot Channels Cleared",
                "Commands can now be used in any channel unless blocked another way.",
                ephemeral=True,
            )
            return

        if channel is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Missing Channel",
                "Select a text channel for add or remove.",
                ephemeral=True,
            )
            return

        if action.value == "add":
            if channel.id in bot_channels:
                await self.bot.embeds.warning_interaction(
                    interaction,
                    "Already Added",
                    f"{channel.mention} is already a bot channel.",
                    ephemeral=True,
                )
                return

            bot_channels.append(channel.id)
            server_data["bot_channels"] = bot_channels
            save_guild_settings(self.bot.storage_path, guild.id, server_data)
            await self.bot.embeds.success_interaction(
                interaction,
                "Bot Channel Added",
                f"Added {channel.mention} to the bot channels list.",
                ephemeral=True,
            )
            return

        if channel.id not in bot_channels:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Not Present",
                f"{channel.mention} is not currently a bot channel.",
                ephemeral=True,
            )
            return

        bot_channels.remove(channel.id)
        server_data["bot_channels"] = bot_channels
        save_guild_settings(self.bot.storage_path, guild.id, server_data)
        await self.bot.embeds.success_interaction(
            interaction,
            "Bot Channel Removed",
            f"Removed {channel.mention} from the bot channels list.",
            ephemeral=True,
        )

    @app_commands.command(name="logging", description="Configure server logging behavior.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def logging_settings(
        self,
        interaction: discord.Interaction,
        enabled: bool,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        patch: dict[str, object] = {"logging_enabled": enabled}
        if channel is not None:
            patch["logging_channel_id"] = channel.id
        elif not enabled:
            patch["logging_channel_id"] = ""

        update_guild_settings(self.bot.storage_path, guild.id, patch)
        target = channel.mention if channel else "no channel set"
        await self.bot.embeds.success_interaction(
            interaction,
            "Logging Updated",
            f"Logging is now {'enabled' if enabled else 'disabled'} with {target}.",
            ephemeral=True,
        )

    @app_commands.command(name="music", description="Update music-related server settings.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def music_settings(
        self,
        interaction: discord.Interaction,
        dj_mode: bool | None = None,
        dj_role_name: str | None = None,
        default_volume: app_commands.Range[int, 0, 100] | None = None,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        patch: dict[str, object] = {}
        if dj_mode is not None:
            patch["music_dj_enabled"] = bool(dj_mode)
        if dj_role_name is not None:
            cleaned_role_name = dj_role_name.strip()
            if not cleaned_role_name:
                await self.bot.embeds.error_interaction(
                    interaction,
                    "Invalid DJ Role",
                    "DJ role name cannot be blank.",
                    ephemeral=True,
                )
                return
            patch["music_dj_role_name"] = cleaned_role_name
        if default_volume is not None:
            patch["music_default_volume"] = int(default_volume)

        if not patch:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nothing To Update",
                "Provide at least one music setting to change.",
                ephemeral=True,
            )
            return

        update_guild_settings(self.bot.storage_path, guild.id, patch)
        updated = get_guild_settings(self.bot.storage_path, guild.id)
        fields = [
            self.bot.embeds.field(
                "DJ Mode",
                "Enabled" if updated.get("music_dj_enabled", False) else "Disabled",
                True,
            ),
            self.bot.embeds.field(
                "DJ Role",
                f"`{updated.get('music_dj_role_name', 'dj')}`",
                True,
            ),
            self.bot.embeds.field(
                "Default Volume",
                f"`{int(updated.get('music_default_volume', 50))}%`",
                True,
            ),
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Music Settings Updated",
            fields=fields,
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
