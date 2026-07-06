from __future__ import annotations

from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from utils.durations import parse_duration
from utils.settings import (
    ALLOWED_PREFIX_CHARS,
    create_guild_settings,
    get_guild_settings,
    save_guild_settings,
    update_guild_settings,
)

CATEGORIES: list[tuple[str, str, str]] = [
    # (value, label, emoji)
    ("general", "General", "⚙️"),
    ("logging", "Activity Log", "📝"),
    ("moderation", "Moderation", "🛡️"),
    ("welcome", "Welcome", "👋"),
    ("music", "Music", "🎵"),
]


def _format_channels(channel_ids: list) -> str:
    if not channel_ids:
        return "None"
    return ", ".join(f"<#{int(cid)}>" for cid in channel_ids)


def _format_roles(role_ids: list) -> str:
    if not role_ids:
        return "None"
    return ", ".join(f"<@&{int(rid)}>" for rid in role_ids)


def _format_users(user_ids: list) -> str:
    if not user_ids:
        return "None"
    return ", ".join(f"<@{int(uid)}>" for uid in user_ids)


def _format_commands(names: list) -> str:
    if not names:
        return "None"
    return ", ".join(f"`{name}`" for name in names)


class SingleFieldModal(discord.ui.Modal):
    """Generic "edit one text field, save it under one settings key" modal.
    Covers prefix / join / leave message / DJ role name / default volume -
    the toggle-a-list (blocked commands) and two-field (warn escalation)
    cases below need their own logic and get their own small modal classes."""

    def __init__(
        self,
        parent_view: "SettingsDashboardView",
        *,
        title: str,
        label: str,
        key: str,
        default: str,
        max_length: int = 200,
        style: discord.TextStyle = discord.TextStyle.short,
        validator: Callable[[str], str | None] | None = None,
        transform: Callable[[str], Any] | None = None,
    ) -> None:
        super().__init__(title=title)
        self.parent_view = parent_view
        self.key = key
        self.validator = validator
        self.transform = transform
        self.field = discord.ui.TextInput(label=label, default=default, max_length=max_length, style=style, required=True)
        self.add_item(self.field)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        value = self.field.value.strip()
        if self.validator:
            error = self.validator(value)
            if error:
                await interaction.response.send_message(error, ephemeral=True)
                return

        final_value = self.transform(value) if self.transform else value
        update_guild_settings(self.parent_view.bot.storage_path, self.parent_view.guild_id, {self.key: final_value})
        await self.parent_view.refresh(interaction)


class ToggleBlockedCommandModal(discord.ui.Modal, title="Toggle Blocked Command"):
    def __init__(self, parent_view: "SettingsDashboardView") -> None:
        super().__init__()
        self.parent_view = parent_view
        self.command_input = discord.ui.TextInput(label="Command name (no / or !)", max_length=32, required=True)
        self.add_item(self.command_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = self.command_input.value.strip()
        if not name or name.startswith("/") or name.startswith("!"):
            await interaction.response.send_message("Command name should not start with `/` or `!`.", ephemeral=True)
            return

        storage_path = self.parent_view.bot.storage_path
        guild_id = self.parent_view.guild_id
        settings = get_guild_settings(storage_path, guild_id)
        blocked = settings.get("blocked_commands", []) or []
        if name in blocked:
            blocked.remove(name)
        else:
            blocked.append(name)
        settings["blocked_commands"] = blocked
        save_guild_settings(storage_path, guild_id, settings)
        await self.parent_view.refresh(interaction)


class WarnEscalationModal(discord.ui.Modal, title="Warning Escalation"):
    def __init__(self, parent_view: "SettingsDashboardView", threshold: int, mute_duration: str) -> None:
        super().__init__()
        self.parent_view = parent_view
        self.threshold_input = discord.ui.TextInput(
            label="Warning threshold (0 disables auto-action)", default=str(threshold), max_length=3, required=True
        )
        self.duration_input = discord.ui.TextInput(
            label="Auto-mute duration (e.g. 1h, 1d12h)", default=mute_duration, max_length=20, required=True
        )
        self.add_item(self.threshold_input)
        self.add_item(self.duration_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        threshold_text = self.threshold_input.value.strip()
        if not threshold_text.isdigit():
            await interaction.response.send_message("Threshold must be a whole number (0 or more).", ephemeral=True)
            return

        duration_text = self.duration_input.value.strip()
        if parse_duration(duration_text) is None:
            await interaction.response.send_message("Use something like `10m`, `2h`, `1d`, or `1d12h`.", ephemeral=True)
            return

        update_guild_settings(
            self.parent_view.bot.storage_path,
            self.parent_view.guild_id,
            {"warn_threshold": int(threshold_text), "warn_mute_duration": duration_text},
        )
        await self.parent_view.refresh(interaction)


class SettingsDashboardView(discord.ui.View):
    """Interactive, categorized settings editor - replaces the old flat list
    of ~10 separate /settings subcommands admins had to already know the name
    and syntax of. Everything here is ephemeral and locked to the admin who
    opened it."""

    def __init__(self, bot: commands.Bot, guild_id: int, author_id: int, category: str = "general") -> None:
        super().__init__(timeout=300.0)
        self.bot = bot
        self.guild_id = guild_id
        self.author_id = author_id
        self.category = category
        self.message: discord.InteractionMessage | None = None
        self._confirming_reset = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("This settings panel isn't yours. Run `/settings` yourself.", ephemeral=True)
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

    async def refresh(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        self._rebuild_items(settings)
        embed = self._build_embed(settings)
        if interaction.response.is_done():
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.response.edit_message(embed=embed, view=self)

    # ---------- item construction ----------

    def _rebuild_items(self, settings: dict) -> None:
        self.clear_items()

        if self._confirming_reset:
            confirm = discord.ui.Button(label="Confirm Reset", style=discord.ButtonStyle.danger)
            confirm.callback = self._on_confirm_reset
            cancel = discord.ui.Button(label="Cancel", style=discord.ButtonStyle.secondary)
            cancel.callback = self._on_cancel_reset
            self.add_item(confirm)
            self.add_item(cancel)
            return

        self.add_item(self._build_category_select())

        builders: dict[str, Callable[[dict], None]] = {
            "general": self._build_general_items,
            "logging": self._build_logging_items,
            "moderation": self._build_moderation_items,
            "welcome": self._build_welcome_items,
            "music": self._build_music_items,
        }
        builders[self.category](settings)

    def _build_category_select(self) -> discord.ui.Select:
        options = [
            discord.SelectOption(label=label, value=value, emoji=emoji, default=self.category == value)
            for value, label, emoji in CATEGORIES
        ]
        select = discord.ui.Select(placeholder="Choose a settings category...", options=options)

        async def _callback(interaction: discord.Interaction) -> None:
            self.category = select.values[0]
            await self.refresh(interaction)

        select.callback = _callback
        return select

    def _build_general_items(self, settings: dict) -> None:
        bot_channel_ids = [int(c) for c in (settings.get("bot_channels") or [])]
        channel_select = discord.ui.ChannelSelect(
            placeholder="Bot channels (commands limited to these; none = all channels)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=cid) for cid in bot_channel_ids],
        )

        async def _on_channels(interaction: discord.Interaction) -> None:
            update_guild_settings(self.bot.storage_path, self.guild_id, {"bot_channels": [c.id for c in channel_select.values]})
            await self.refresh(interaction)

        channel_select.callback = _on_channels
        self.add_item(channel_select)

        prefix_btn = discord.ui.Button(label=f"Edit Prefix ({settings.get('prefix', '!')})", style=discord.ButtonStyle.secondary)
        prefix_btn.callback = self._on_edit_prefix
        self.add_item(prefix_btn)

        blocked_btn = discord.ui.Button(label="Toggle Blocked Command", style=discord.ButtonStyle.secondary)
        blocked_btn.callback = self._on_edit_blocked_command
        self.add_item(blocked_btn)

        listen_enabled = bool(settings.get("listen_to_bots"))
        listen_btn = discord.ui.Button(
            label=f"Listen To Bots: {'On' if listen_enabled else 'Off'}",
            style=discord.ButtonStyle.success if listen_enabled else discord.ButtonStyle.secondary,
        )
        listen_btn.callback = self._on_toggle_listen_to_bots
        self.add_item(listen_btn)

        reset_btn = discord.ui.Button(label="Reset To Defaults", style=discord.ButtonStyle.danger)
        reset_btn.callback = self._on_reset_clicked
        self.add_item(reset_btn)

    def _build_logging_items(self, settings: dict) -> None:
        current_log_channel = settings.get("logging_channel_id")
        log_channel_select = discord.ui.ChannelSelect(
            placeholder="Activity log channel (empty = off)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=int(current_log_channel))] if current_log_channel else [],
        )

        async def _on_log_channel(interaction: discord.Interaction) -> None:
            values = log_channel_select.values
            if values:
                update_guild_settings(
                    self.bot.storage_path, self.guild_id, {"logging_enabled": True, "logging_channel_id": str(values[0].id)}
                )
            else:
                update_guild_settings(self.bot.storage_path, self.guild_id, {"logging_enabled": False, "logging_channel_id": ""})
            await self.refresh(interaction)

        log_channel_select.callback = _on_log_channel
        self.add_item(log_channel_select)

        ignored_channel_ids = [int(c) for c in (settings.get("ignored_log_channels") or [])]
        ignored_channels_select = discord.ui.ChannelSelect(
            placeholder="Channels ignored by the activity log",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=cid) for cid in ignored_channel_ids],
        )

        async def _on_ignored_channels(interaction: discord.Interaction) -> None:
            update_guild_settings(
                self.bot.storage_path, self.guild_id, {"ignored_log_channels": [c.id for c in ignored_channels_select.values]}
            )
            await self.refresh(interaction)

        ignored_channels_select.callback = _on_ignored_channels
        self.add_item(ignored_channels_select)

        ignored_user_ids = [int(u) for u in (settings.get("ignored_users") or [])]
        ignored_users_select = discord.ui.UserSelect(
            placeholder="Users ignored by the activity log",
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=uid) for uid in ignored_user_ids],
        )

        async def _on_ignored_users(interaction: discord.Interaction) -> None:
            update_guild_settings(self.bot.storage_path, self.guild_id, {"ignored_users": [u.id for u in ignored_users_select.values]})
            await self.refresh(interaction)

        ignored_users_select.callback = _on_ignored_users
        self.add_item(ignored_users_select)

    def _build_moderation_items(self, settings: dict) -> None:
        current_mod_log_channel = settings.get("mod_log_channel_id")
        mod_log_select = discord.ui.ChannelSelect(
            placeholder="Moderation log channel (empty = off)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=int(current_mod_log_channel))] if current_mod_log_channel else [],
        )

        async def _on_mod_log(interaction: discord.Interaction) -> None:
            values = mod_log_select.values
            if values:
                update_guild_settings(
                    self.bot.storage_path, self.guild_id, {"mod_log_enabled": True, "mod_log_channel_id": str(values[0].id)}
                )
            else:
                update_guild_settings(self.bot.storage_path, self.guild_id, {"mod_log_enabled": False, "mod_log_channel_id": ""})
            await self.refresh(interaction)

        mod_log_select.callback = _on_mod_log
        self.add_item(mod_log_select)

        autorole_ids = [int(r) for r in (settings.get("autoroles") or [])]
        autorole_select = discord.ui.RoleSelect(
            placeholder="Autoroles (given to new members on join)",
            min_values=0,
            max_values=25,
            default_values=[discord.Object(id=rid) for rid in autorole_ids],
        )

        async def _on_autoroles(interaction: discord.Interaction) -> None:
            update_guild_settings(self.bot.storage_path, self.guild_id, {"autoroles": [r.id for r in autorole_select.values]})
            await self.refresh(interaction)

        autorole_select.callback = _on_autoroles
        self.add_item(autorole_select)

        warn_action = settings.get("warn_action", "none")
        action_select = discord.ui.Select(
            placeholder="Action when a member hits the warning threshold",
            options=[
                discord.SelectOption(label="No automatic action", value="none", default=warn_action == "none"),
                discord.SelectOption(label="Auto-mute", value="mute", default=warn_action == "mute"),
                discord.SelectOption(label="Auto-kick", value="kick", default=warn_action == "kick"),
                discord.SelectOption(label="Auto-ban", value="ban", default=warn_action == "ban"),
            ],
        )

        async def _on_warn_action(interaction: discord.Interaction) -> None:
            update_guild_settings(self.bot.storage_path, self.guild_id, {"warn_action": action_select.values[0]})
            await self.refresh(interaction)

        action_select.callback = _on_warn_action
        self.add_item(action_select)

        sticky_enabled = bool(settings.get("sticky_roles"))
        sticky_btn = discord.ui.Button(
            label=f"Sticky Roles: {'On' if sticky_enabled else 'Off'}",
            style=discord.ButtonStyle.success if sticky_enabled else discord.ButtonStyle.secondary,
        )
        sticky_btn.callback = self._on_toggle_sticky_roles
        self.add_item(sticky_btn)

        warn_settings_btn = discord.ui.Button(
            label=f"Warn Threshold/Duration ({settings.get('warn_threshold', 0)} / {settings.get('warn_mute_duration', '1h')})",
            style=discord.ButtonStyle.secondary,
        )
        warn_settings_btn.callback = self._on_edit_warn_settings
        self.add_item(warn_settings_btn)

    def _build_welcome_items(self, settings: dict) -> None:
        welcome_ids = settings.get("welcome_channel_ids") or []
        current_welcome_channel = welcome_ids[0] if welcome_ids else None
        welcome_select = discord.ui.ChannelSelect(
            placeholder="Welcome channel (empty = off)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=int(current_welcome_channel))] if current_welcome_channel else [],
        )

        async def _on_welcome_channel(interaction: discord.Interaction) -> None:
            values = welcome_select.values
            update_guild_settings(
                self.bot.storage_path, self.guild_id, {"welcome_channel_ids": [str(values[0].id)] if values else []}
            )
            await self.refresh(interaction)

        welcome_select.callback = _on_welcome_channel
        self.add_item(welcome_select)

        join_btn = discord.ui.Button(label="Edit Join Message", style=discord.ButtonStyle.secondary)
        join_btn.callback = self._on_edit_join_message
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(label="Edit Leave Message", style=discord.ButtonStyle.secondary)
        leave_btn.callback = self._on_edit_leave_message
        self.add_item(leave_btn)

    def _build_music_items(self, settings: dict) -> None:
        dj_enabled = bool(settings.get("music_dj_enabled"))
        dj_btn = discord.ui.Button(
            label=f"DJ Mode: {'On' if dj_enabled else 'Off'}",
            style=discord.ButtonStyle.success if dj_enabled else discord.ButtonStyle.secondary,
        )
        dj_btn.callback = self._on_toggle_dj_mode
        self.add_item(dj_btn)

        dj_role_display = str(settings.get("music_dj_role_name", "dj"))
        if len(dj_role_display) > 40:
            dj_role_display = dj_role_display[:37] + "..."
        dj_role_btn = discord.ui.Button(
            label=f"DJ Role Name ({dj_role_display})", style=discord.ButtonStyle.secondary
        )
        dj_role_btn.callback = self._on_edit_dj_role_name
        self.add_item(dj_role_btn)

        volume_btn = discord.ui.Button(
            label=f"Default Volume ({settings.get('music_default_volume', 50)})", style=discord.ButtonStyle.secondary
        )
        volume_btn.callback = self._on_edit_default_volume
        self.add_item(volume_btn)

    # ---------- button/modal callbacks ----------

    async def _on_edit_prefix(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if not value or not all(c in ALLOWED_PREFIX_CHARS for c in value):
                return f"Only these characters are allowed: {ALLOWED_PREFIX_CHARS}"
            return None

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Edit Prefix",
                label="Prefix",
                key="prefix",
                default=str(settings.get("prefix", "!")),
                max_length=5,
                validator=_validate,
            )
        )

    async def _on_edit_blocked_command(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ToggleBlockedCommandModal(self))

    async def _on_toggle_listen_to_bots(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        update_guild_settings(self.bot.storage_path, self.guild_id, {"listen_to_bots": not settings.get("listen_to_bots", False)})
        await self.refresh(interaction)

    async def _on_reset_clicked(self, interaction: discord.Interaction) -> None:
        self._confirming_reset = True
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        self._rebuild_items(settings)
        embed = self.bot.embeds.warning_embed(
            "Reset To Defaults?",
            "This will erase every setting for this server (logging, welcome, autoroles, moderation - everything) "
            "and restore the defaults. This cannot be undone.",
        )
        await interaction.response.edit_message(embed=embed, view=self)

    async def _on_confirm_reset(self, interaction: discord.Interaction) -> None:
        self._confirming_reset = False
        self.category = "general"
        create_guild_settings(self.bot.storage_path, self.guild_id)
        await self.refresh(interaction)

    async def _on_cancel_reset(self, interaction: discord.Interaction) -> None:
        self._confirming_reset = False
        await self.refresh(interaction)

    async def _on_edit_warn_settings(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        await interaction.response.send_modal(
            WarnEscalationModal(
                self,
                int(settings.get("warn_threshold", 0) or 0),
                str(settings.get("warn_mute_duration", "1h")),
            )
        )

    async def _on_toggle_sticky_roles(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        update_guild_settings(self.bot.storage_path, self.guild_id, {"sticky_roles": not settings.get("sticky_roles", False)})
        await self.refresh(interaction)

    async def _on_edit_join_message(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Edit Join Message",
                label="Placeholders: {mention} {user} {server}",
                key="join_message",
                default=str(settings.get("join_message", "")),
                max_length=500,
                style=discord.TextStyle.paragraph,
            )
        )

    async def _on_edit_leave_message(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Edit Leave Message",
                label="Placeholders: {mention} {user} {server}",
                key="leave_message",
                default=str(settings.get("leave_message", "")),
                max_length=500,
                style=discord.TextStyle.paragraph,
            )
        )

    async def _on_toggle_dj_mode(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        update_guild_settings(self.bot.storage_path, self.guild_id, {"music_dj_enabled": not settings.get("music_dj_enabled", False)})
        await self.refresh(interaction)

    async def _on_edit_dj_role_name(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            return None if value else "Role name can't be empty."

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="DJ Role Name",
                label="Exact server role name",
                key="music_dj_role_name",
                default=str(settings.get("music_dj_role_name", "dj")),
                max_length=100,
                validator=_validate,
            )
        )

    async def _on_edit_default_volume(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if not value.isdigit() or not (0 <= int(value) <= 100):
                return "Volume must be a whole number between 0 and 100."
            return None

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Default Volume",
                label="Percent (0-100)",
                key="music_default_volume",
                default=str(settings.get("music_default_volume", 50)),
                max_length=3,
                validator=_validate,
                transform=int,
            )
        )

    # ---------- embed ----------

    def _build_embed(self, settings: dict) -> discord.Embed:
        if self._confirming_reset:
            return self.bot.embeds.warning_embed(
                "Reset To Defaults?",
                "This will erase every setting for this server and restore the defaults. This cannot be undone.",
            )

        label = next(label for value, label, _ in CATEGORIES if value == self.category)
        embed = self.bot.embeds.create(title=f"Server Settings - {label}", color=self.bot.embeds.default_color)

        if self.category == "general":
            embed.description = "Use the dropdown to switch categories. Selects and buttons below edit a setting immediately."
            embed.add_field(name="Prefix", value=f"`{settings.get('prefix', '!')}`", inline=True)
            embed.add_field(name="Listen To Bots", value="Yes" if settings.get("listen_to_bots") else "No", inline=True)
            embed.add_field(name="Bot Channels", value=_format_channels(settings.get("bot_channels") or []), inline=False)
            embed.add_field(name="Blocked Commands", value=_format_commands(settings.get("blocked_commands") or []), inline=False)
        elif self.category == "logging":
            logging_enabled = bool(settings.get("logging_enabled")) and bool(settings.get("logging_channel_id"))
            status = f"On - <#{int(settings['logging_channel_id'])}>" if logging_enabled else "Off"
            embed.add_field(name="Activity Logging", value=status, inline=False)
            embed.add_field(name="Ignored Channels", value=_format_channels(settings.get("ignored_log_channels") or []), inline=False)
            embed.add_field(name="Ignored Users", value=_format_users(settings.get("ignored_users") or []), inline=False)
        elif self.category == "moderation":
            mod_log_enabled = bool(settings.get("mod_log_enabled")) and bool(settings.get("mod_log_channel_id"))
            status = f"On - <#{int(settings['mod_log_channel_id'])}>" if mod_log_enabled else "Off"
            embed.add_field(name="Moderation Log", value=status, inline=False)
            embed.add_field(name="Autoroles", value=_format_roles(settings.get("autoroles") or []), inline=False)
            embed.add_field(name="Sticky Roles", value="On" if settings.get("sticky_roles") else "Off", inline=True)
            embed.add_field(
                name="Warning Escalation",
                value=(
                    f"After **{settings.get('warn_threshold', 0)}** warning(s): **{settings.get('warn_action', 'none')}**"
                    + (f" ({settings.get('warn_mute_duration', '1h')})" if settings.get("warn_action") == "mute" else "")
                ),
                inline=False,
            )
        elif self.category == "welcome":
            welcome_ids = settings.get("welcome_channel_ids") or []
            status = _format_channels(welcome_ids) if welcome_ids else "Off"
            embed.add_field(name="Welcome Channel", value=status, inline=False)
            embed.add_field(name="Join Message", value=settings.get("join_message") or "*(not set)*", inline=False)
            embed.add_field(name="Leave Message", value=settings.get("leave_message") or "*(not set)*", inline=False)
        elif self.category == "music":
            embed.add_field(name="DJ Mode", value="On" if settings.get("music_dj_enabled") else "Off", inline=True)
            embed.add_field(name="DJ Role Name", value=f"`{settings.get('music_dj_role_name', 'dj')}`", inline=True)
            embed.add_field(name="Default Volume", value=f"{settings.get('music_default_volume', 50)}%", inline=True)

        return embed


class Settings(commands.Cog):
    """Server settings: a single interactive dashboard rather than a list of
    separate slash commands admins have to already know the names of."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        create_guild_settings(self.bot.storage_path, guild.id)

    @app_commands.command(name="settings", description="Open the interactive server settings dashboard.")
    @app_commands.checks.has_permissions(administrator=True)
    @app_commands.guild_only()
    async def settings_dashboard(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(
                interaction, "Server Only", "This command can only be used in a server.", ephemeral=True
            )
            return

        view = SettingsDashboardView(self.bot, interaction.guild.id, interaction.user.id)
        settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
        view._rebuild_items(settings)
        embed = view._build_embed(settings)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.message = await interaction.original_response()


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
