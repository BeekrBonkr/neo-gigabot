from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import discord
from discord import app_commands
from discord.ext import commands

from utils.settings import get_guild_settings, normalize_command_name, normalize_id_list

CATEGORY_LABELS: dict[str, str] = {
    "fun": "Fun",
    "images": "Images",
    "moderation": "Moderation",
    "music": "Music",
    "settings": "Settings",
    "owner": "Owner",
    "help": "Help",
    "other": "Other",
}

CATEGORY_EMOJIS: dict[str, str] = {
    "fun": "🎉",
    "images": "🖼️",
    "moderation": "🛡️",
    "music": "🎵",
    "settings": "⚙️",
    "owner": "🧰",
    "help": "📘",
    "other": "📚",
}

PAGE_SIZE = 6


@dataclass(slots=True)
class HelpEntry:
    category_key: str
    category_label: str
    command_path: str
    summary: str
    details: list[str]
    sort_key: str


class HelpCategorySelect(discord.ui.Select):
    def __init__(self, view: "HelpMenuView") -> None:
        options = [
            discord.SelectOption(
                label=label,
                value=key,
                emoji=CATEGORY_EMOJIS.get(key, "📘"),
                default=(key == view.category_key),
            )
            for key, label in view.category_options
        ]

        super().__init__(
            placeholder="Choose a category...",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.help_view = view

    async def callback(self, interaction: discord.Interaction) -> None:
        self.help_view.category_key = self.values[0]
        self.help_view.page_index = 0
        self.help_view.refresh_items()
        await self.help_view.update_message(interaction)


class HelpPreviousButton(discord.ui.Button["HelpMenuView"]):
    def __init__(self) -> None:
        super().__init__(label="Previous", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view.page_index = max(0, view.page_index - 1)
        view.refresh_items()
        await view.update_message(interaction)


class HelpNextButton(discord.ui.Button["HelpMenuView"]):
    def __init__(self) -> None:
        super().__init__(label="Next", style=discord.ButtonStyle.secondary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None
        view.page_index = min(view.total_pages() - 1, view.page_index + 1)
        view.refresh_items()
        await view.update_message(interaction)


class HelpRefreshButton(discord.ui.Button["HelpMenuView"]):
    def __init__(self) -> None:
        super().__init__(label="Refresh", style=discord.ButtonStyle.primary, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = self.view
        assert view is not None

        refreshed_entries = await view.cog.collect_visible_entries(interaction)
        view.entries = refreshed_entries
        view.category_options = view._build_category_options(refreshed_entries)

        if not any(key == view.category_key for key, _label in view.category_options):
            view.category_key = view.category_options[0][0]
            view.page_index = 0
        else:
            view.page_index = min(view.page_index, view.total_pages() - 1)

        view.refresh_items()
        await view.update_message(interaction)


class HelpMenuView(discord.ui.View):
    def __init__(
        self,
        cog: "Help",
        interaction: discord.Interaction,
        entries: list[HelpEntry],
    ) -> None:
        super().__init__(timeout=180)
        self.cog = cog
        self.owner_id = interaction.user.id
        self.entries = entries
        self.category_options = self._build_category_options(entries)
        self.category_key = self.category_options[0][0]
        self.page_index = 0
        self.message: discord.Message | None = None
        self.refresh_items()

    def _build_category_options(self, entries: Iterable[HelpEntry]) -> list[tuple[str, str]]:
        seen: set[str] = set()
        ordered: list[tuple[str, str]] = []

        for entry in entries:
            if entry.category_key in seen:
                continue
            seen.add(entry.category_key)
            ordered.append((entry.category_key, entry.category_label))

        return ordered or [("other", "Other")]

    def refresh_items(self) -> None:
        self.clear_items()

        if len(self.category_options) > 1:
            self.add_item(HelpCategorySelect(self))

        prev_button = HelpPreviousButton()
        next_button = HelpNextButton()
        refresh_button = HelpRefreshButton()

        prev_button.disabled = self.page_index <= 0
        next_button.disabled = self.page_index >= (self.total_pages() - 1)

        self.add_item(prev_button)
        self.add_item(next_button)
        self.add_item(refresh_button)

    def current_entries(self) -> list[HelpEntry]:
        filtered = [entry for entry in self.entries if entry.category_key == self.category_key]
        start = self.page_index * PAGE_SIZE
        end = start + PAGE_SIZE
        return filtered[start:end]

    def category_entry_count(self) -> int:
        return sum(1 for entry in self.entries if entry.category_key == self.category_key)

    def total_pages(self) -> int:
        filtered_count = self.category_entry_count()
        return max(1, ((filtered_count - 1) // PAGE_SIZE) + 1)

    async def update_message(self, interaction: discord.Interaction) -> None:
        embed = self.cog.build_help_embed(self)
        await interaction.response.edit_message(embed=embed, view=self)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            if hasattr(self.cog.bot, "embeds"):
                await self.cog.bot.embeds.error_interaction(
                    interaction,
                    "Not Your Help Menu",
                    "Run `/help` yourself to open your own help menu.",
                    ephemeral=True,
                )
            else:
                await interaction.response.send_message(
                    "Run `/help` yourself to open your own help menu.",
                    ephemeral=True,
                )
            return False
        return True

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True

        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Help(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @app_commands.command(name="help", description="Browse the bot's commands.")
    async def help_menu(self, interaction: discord.Interaction) -> None:
        entries = await self.collect_visible_entries(interaction)

        if not entries:
            embed = discord.Embed(
                title="Help",
                description="I couldn't find any commands to show right now.",
                color=discord.Color.orange(),
            )
            await interaction.response.send_message(embed=embed, ephemeral=True)
            return

        view = HelpMenuView(self, interaction, entries)
        embed = self.build_help_embed(view)

        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()

    async def collect_visible_entries(self, interaction: discord.Interaction) -> list[HelpEntry]:
        entries: list[HelpEntry] = []

        global_commands = list(self.bot.tree.get_commands())
        guild_commands = (
            list(self.bot.tree.get_commands(guild=interaction.guild))
            if interaction.guild is not None
            else []
        )

        commands_to_scan: list[
            app_commands.Command | app_commands.Group | app_commands.ContextMenu
        ] = []
        seen: set[tuple[str, type]] = set()

        for command in [*global_commands, *guild_commands]:
            qualified_name = getattr(command, "qualified_name", command.name)
            key = (qualified_name, type(command))
            if key in seen:
                continue
            seen.add(key)
            commands_to_scan.append(command)

        for command in commands_to_scan:
            entries.extend(await self._flatten_visible_commands(interaction, command))

        entries.sort(key=lambda entry: (entry.category_label.lower(), entry.sort_key))
        return entries

    async def _flatten_visible_commands(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.Group | app_commands.ContextMenu,
        parent_path: str = "",
    ) -> list[HelpEntry]:
        visible_entries: list[HelpEntry] = []
        current_path = getattr(command, "qualified_name", f"{parent_path} {command.name}".strip())

        allowed = await self._can_list_command(interaction, command, current_path)
        if not allowed:
            return visible_entries

        if isinstance(command, app_commands.Group):
            for child in command.commands:
                visible_entries.extend(
                    await self._flatten_visible_commands(
                        interaction,
                        child,
                        current_path,
                    )
                )
            return visible_entries

        category_key = self._category_key_for_command(command)
        visible_entries.append(
            HelpEntry(
                category_key=category_key,
                category_label=CATEGORY_LABELS.get(category_key, "Other"),
                command_path=self._format_command_path(command, current_path),
                summary=self._command_summary(command),
                details=self._build_command_details(interaction, command, current_path),
                sort_key=current_path.lower(),
            )
        )
        return visible_entries

    async def _can_list_command(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.Group | app_commands.ContextMenu,
        command_path: str,
    ) -> bool:
        if command.name == "help":
            return True

        if self._is_owner_command(command_path) and not self._is_owner(interaction):
            return False

        if getattr(command, "guild_only", False) and interaction.guild is None:
            return False

        if interaction.guild is not None:
            server_data = get_guild_settings(self.bot.storage_path, interaction.guild.id)
            blocked_commands = {
                normalize_command_name(name)
                for name in (server_data.get("blocked_commands", []) or [])
            }

            normalized_path = normalize_command_name(command_path)
            if normalized_path in blocked_commands:
                return False

        if not self._passes_default_permissions(interaction, command):
            return False

        return True

    def _passes_default_permissions(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.Group | app_commands.ContextMenu,
    ) -> bool:
        if interaction.guild is None:
            return not getattr(command, "guild_only", False)

        if interaction.user.id == self._configured_owner_id():
            return True

        member = (
            interaction.user
            if isinstance(interaction.user, discord.Member)
            else interaction.guild.get_member(interaction.user.id)
        )
        if member is None:
            return True

        if member.guild_permissions.administrator:
            return True

        permissions = getattr(command, "default_permissions", None)
        if permissions is None:
            return True

        return member.guild_permissions.is_superset(permissions)

    def _build_command_details(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
        command_path: str,
    ) -> list[str]:
        details: list[str] = []

        if isinstance(command, app_commands.ContextMenu):
            if command.type is discord.AppCommandType.user:
                details.append("User menu")
            elif command.type is discord.AppCommandType.message:
                details.append("Message menu")

        if getattr(command, "guild_only", False):
            details.append("Server only")
        else:
            details.append("Works in DMs")

        permissions = getattr(command, "default_permissions", None)
        if permissions is not None:
            required = [name.replace("_", " ").title() for name, value in permissions if value]
            if required:
                details.append(f"Needs {', '.join(required)}")

        if self._is_owner_command(command_path):
            details.append("Owner only")

        if interaction.guild is not None:
            server_data = get_guild_settings(self.bot.storage_path, interaction.guild.id)
            bot_channels = normalize_id_list(server_data.get("bot_channels", []) or [])

            if bot_channels and interaction.channel_id not in bot_channels and not self._is_owner(interaction):
                normalized_path = normalize_command_name(command_path)
                bypass_prefixes = {
                    "help",
                    "settings botchannel",
                    "settings permissions",
                    "owner",
                }
                if not any(normalized_path.startswith(prefix) for prefix in bypass_prefixes):
                    details.append("Bot channel only")

        return details

    def _command_summary(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> str:
        if isinstance(command, app_commands.ContextMenu):
            if command.type is discord.AppCommandType.user:
                return "Use this from a user's context menu."
            if command.type is discord.AppCommandType.message:
                return "Use this from a message's context menu."
            return "Context menu command."

        return getattr(command, "description", None) or "No description provided."

    def _format_command_path(
        self,
        command: app_commands.Command | app_commands.ContextMenu,
        command_path: str,
    ) -> str:
        if isinstance(command, app_commands.ContextMenu):
            if command.type is discord.AppCommandType.user:
                return f"👤 {command.name}"
            if command.type is discord.AppCommandType.message:
                return f"💬 {command.name}"
            return f"📎 {command.name}"

        return f"/{command_path}"

    def _configured_owner_id(self) -> int | None:
        owner_id = getattr(self.bot.config, "owner_id", None)
        try:
            return int(owner_id)
        except (TypeError, ValueError):
            return None

    def _is_owner(self, interaction: discord.Interaction) -> bool:
        owner_id = self._configured_owner_id()
        return owner_id is not None and interaction.user.id == owner_id

    def _is_owner_command(self, command_path: str) -> bool:
        normalized = normalize_command_name(command_path)
        return normalized == "owner" or normalized.startswith("owner ")

    def _category_key_for_command(
        self,
        command: app_commands.Command | app_commands.Group | app_commands.ContextMenu,
    ) -> str:
        module_name = getattr(command, "module", "") or ""

        if module_name.startswith("cogs."):
            return module_name.split(".", 1)[1].split(".", 1)[0]

        binding = getattr(command, "binding", None)
        if binding is not None:
            cog_name = binding.__class__.__name__.lower()
            for key in CATEGORY_LABELS:
                if key != "other" and key in cog_name:
                    return key

        return "other"

    def build_help_embed(self, view: HelpMenuView) -> discord.Embed:
        category_label = dict(view.category_options).get(view.category_key, "Other")
        category_emoji = CATEGORY_EMOJIS.get(view.category_key, "📘")
        page_entries = view.current_entries()
        total_in_category = view.category_entry_count()

        embed = discord.Embed(
            title=f"{category_emoji} {category_label} Commands",
            description=(
                "Browse commands available to you right now.\n"
                "Only commands you can currently access are shown."
            ),
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Overview",
            value=(
                f"Category: **{category_label}**\n"
                f"Commands here: **{total_in_category}**\n"
                f"Page: **{view.page_index + 1}/{view.total_pages()}**"
            ),
            inline=False,
        )

        if not page_entries:
            embed.add_field(
                name="No Commands",
                value="Try another category or press **Refresh**.",
                inline=False,
            )
        else:
            for entry in page_entries:
                value_lines = [entry.summary]

                if entry.details:
                    value_lines.append("")
                    value_lines.append("`" + " • ".join(entry.details) + "`")

                embed.add_field(
                    name=entry.command_path,
                    value="\n".join(value_lines),
                    inline=False,
                )

        embed.set_footer(text="Use the menu below to switch categories.")
        return embed


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Help(bot))