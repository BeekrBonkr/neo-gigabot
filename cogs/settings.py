from __future__ import annotations

from typing import Any, Callable

import discord
from discord import app_commands
from discord.ext import commands

from utils.durations import parse_duration
from utils.settings import (
    MOD_LOG_EVENT_KINDS,
    create_guild_settings,
    get_guild_settings,
    guild_settings_exists,
    parse_hex_color,
    update_guild_settings,
)

CATEGORIES: list[tuple[str, str, str, str]] = [
    # (value, label, emoji, blurb shown at the top of the category's embed)
    ("general", "General", "⚙️", "Where the bot listens, who it listens to, and how it looks."),
    ("commands", "Commands", "🧩", "Disable commands you don't want on this server."),
    ("logging", "Activity Log", "📝", "Post message, member, and server changes to a log channel."),
    ("moderation", "Moderation", "🛡️", "Autoroles, sticky roles, and automatic warning escalation."),
    ("modlog", "Mod Log", "📋", "Where moderation actions get posted, which ones, and member DMs."),
    ("welcome", "Welcome", "👋", "Greet new members and announce departures."),
    ("starboard", "Starboard", "🌟", "Repost popular messages that collect enough star reactions."),
    ("music", "Music", "🎵", "DJ restrictions, playback defaults, and queue limits."),
]

MOD_LOG_EVENT_LABELS = {
    "warn": ("Warnings", "⚠️"),
    "mute": ("Mutes / unmutes", "🔇"),
    "kick": ("Kicks", "👢"),
    "ban": ("Bans / unbans / tempbans", "🔨"),
}

# Commands that must never be disableable from the dashboard: /settings would
# lock a server out of its own dashboard, and Owner commands are the bot
# owner's escape hatches.
UNBLOCKABLE_COMMAND_NAMES = {"settings"}
UNBLOCKABLE_BUCKETS = {"Owner", "Settings"}
MAX_SELECT_OPTIONS = 25


def _iter_leaf_commands(
    commands_iter,
) -> "list[app_commands.Command]":
    leaves: list[app_commands.Command] = []
    for command in commands_iter:
        if isinstance(command, app_commands.Group):
            leaves.extend(_iter_leaf_commands(command.commands))
        elif isinstance(command, app_commands.Command):
            leaves.append(command)
    return leaves


def _blockable_command_pages(bot: commands.Bot) -> list[tuple[str, list[app_commands.Command]]]:
    """The bot's slash commands as (page label, commands) pairs for the disable
    dropdown, bucketed by cog (or by group for GroupCogs like /image) and split
    into chunks of 25 because that's Discord's per-select option cap."""
    buckets: dict[str, list[app_commands.Command]] = {}
    for top in bot.tree.get_commands():
        if isinstance(top, app_commands.Group):
            label = top.name.title()
            leaves = _iter_leaf_commands(top.commands)
        elif isinstance(top, app_commands.Command):
            label = getattr(top.binding, "qualified_name", None) or "Other"
            leaves = [top]
        else:  # context menus can't be gated by the blocked-command list
            continue
        if label in UNBLOCKABLE_BUCKETS:
            continue
        buckets.setdefault(label, []).extend(c for c in leaves if c.name not in UNBLOCKABLE_COMMAND_NAMES)

    pages: list[tuple[str, list[app_commands.Command]]] = []
    for label in sorted(buckets):
        seen: set[str] = set()
        unique = [c for c in sorted(buckets[label], key=lambda c: c.qualified_name) if not (c.qualified_name in seen or seen.add(c.qualified_name))]
        chunks = [unique[i : i + MAX_SELECT_OPTIONS] for i in range(0, len(unique), MAX_SELECT_OPTIONS)]
        for index, chunk in enumerate(chunks):
            page_label = label if len(chunks) == 1 else f"{label} ({index + 1}/{len(chunks)})"
            pages.append((page_label, chunk))
    return pages


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
    Covers join / leave message / DJ role name / default volume -
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
        required: bool = True,
    ) -> None:
        super().__init__(title=title)
        self.parent_view = parent_view
        self.key = key
        self.validator = validator
        self.transform = transform
        self.field = discord.ui.TextInput(label=label, default=default, max_length=max_length, style=style, required=required)
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


class WarnEscalationModal(discord.ui.Modal, title="Warning Escalation"):
    def __init__(self, parent_view: "SettingsDashboardView", threshold: int, mute_duration: str, window_days: int) -> None:
        super().__init__()
        self.parent_view = parent_view
        self.threshold_input = discord.ui.TextInput(
            label="Warning threshold (0 disables auto-action)", default=str(threshold), max_length=3, required=True
        )
        self.duration_input = discord.ui.TextInput(
            label="Auto-mute duration (e.g. 1h, 1d12h)", default=mute_duration, max_length=20, required=True
        )
        self.window_input = discord.ui.TextInput(
            label="Only count warnings from last N days (0=all)", default=str(window_days), max_length=4, required=True
        )
        self.add_item(self.threshold_input)
        self.add_item(self.duration_input)
        self.add_item(self.window_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        threshold_text = self.threshold_input.value.strip()
        if not threshold_text.isdigit():
            await interaction.response.send_message("Threshold must be a whole number (0 or more).", ephemeral=True)
            return

        duration_text = self.duration_input.value.strip()
        if parse_duration(duration_text) is None:
            await interaction.response.send_message("Use something like `10m`, `2h`, `1d`, or `1d12h`.", ephemeral=True)
            return

        window_text = self.window_input.value.strip()
        if not window_text.isdigit():
            await interaction.response.send_message("The warning window must be a whole number of days (0 or more).", ephemeral=True)
            return

        update_guild_settings(
            self.parent_view.bot.storage_path,
            self.parent_view.guild_id,
            {
                "warn_threshold": int(threshold_text),
                "warn_mute_duration": duration_text,
                "warn_window_days": int(window_text),
            },
        )
        await self.parent_view.refresh(interaction)


class StarboardThresholdModal(discord.ui.Modal, title="Starboard Threshold"):
    def __init__(self, parent_view: "SettingsDashboardView", threshold: int) -> None:
        super().__init__()
        self.parent_view = parent_view
        self.threshold_input = discord.ui.TextInput(
            label="Stars needed to reach the starboard", default=str(threshold), max_length=3, required=True
        )
        self.add_item(self.threshold_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = self.threshold_input.value.strip()
        if not text.isdigit() or int(text) < 1:
            await interaction.response.send_message("The threshold must be a whole number of 1 or more.", ephemeral=True)
            return

        update_guild_settings(self.parent_view.bot.storage_path, self.parent_view.guild_id, {"starboard_threshold": int(text)})
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
        self.command_page = 0
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
            "commands": self._build_commands_items,
            "logging": self._build_logging_items,
            "moderation": self._build_moderation_items,
            "modlog": self._build_modlog_items,
            "welcome": self._build_welcome_items,
            "starboard": self._build_starboard_items,
            "music": self._build_music_items,
        }
        builders[self.category](settings)

    def _build_category_select(self) -> discord.ui.Select:
        options = [
            discord.SelectOption(label=label, value=value, emoji=emoji, description=blurb, default=self.category == value)
            for value, label, emoji, blurb in CATEGORIES
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

        listen_enabled = bool(settings.get("listen_to_bots"))
        listen_btn = discord.ui.Button(
            label=f"Listen To Bots: {'On' if listen_enabled else 'Off'}",
            emoji="🤖",
            style=discord.ButtonStyle.success if listen_enabled else discord.ButtonStyle.secondary,
        )
        listen_btn.callback = self._on_toggle_listen_to_bots
        self.add_item(listen_btn)

        embed_color = str(settings.get("embed_color") or "")
        color_btn = discord.ui.Button(
            label=f"Embed Color ({embed_color.upper() if embed_color else 'Default'})",
            emoji="🎨",
            style=discord.ButtonStyle.secondary,
        )
        color_btn.callback = self._on_edit_embed_color
        self.add_item(color_btn)

        reset_btn = discord.ui.Button(label="Reset To Defaults", emoji="♻️", style=discord.ButtonStyle.danger)
        reset_btn.callback = self._on_reset_clicked
        self.add_item(reset_btn)

    def _build_commands_items(self, settings: dict) -> None:
        pages = _blockable_command_pages(self.bot)
        if not pages:
            return
        self.command_page = min(self.command_page, len(pages) - 1)

        page_select = discord.ui.Select(
            placeholder="Pick a command group...",
            options=[
                discord.SelectOption(
                    label=label,
                    value=str(index),
                    description=f"{len(chunk)} command(s)",
                    default=index == self.command_page,
                )
                for index, (label, chunk) in enumerate(pages)
            ],
        )

        async def _on_page(interaction: discord.Interaction) -> None:
            self.command_page = int(page_select.values[0])
            await self.refresh(interaction)

        page_select.callback = _on_page
        self.add_item(page_select)

        _, chunk = pages[self.command_page]
        blocked = {str(name).lower() for name in (settings.get("blocked_commands") or [])}
        # Blocks are stored and matched by qualified name ("playlist add"),
        # never the leaf name - leaf names collide across groups (disabling
        # "playlist add" used to also disable "reactionrole add").
        command_select = discord.ui.Select(
            placeholder="Select the commands to disable (deselect to re-enable)",
            min_values=0,
            max_values=len(chunk),
            options=[
                discord.SelectOption(
                    label=f"/{command.qualified_name}"[:100],
                    value=command.qualified_name[:100],
                    description=(command.description or "")[:100] or None,
                    default=command.qualified_name.lower() in blocked,
                )
                for command in chunk
            ],
        )

        async def _on_commands(interaction: discord.Interaction) -> None:
            current = get_guild_settings(self.bot.storage_path, self.guild_id)
            # Rebuild the list from scratch for this page only, keeping blocks
            # that belong to other pages (or legacy names) untouched.
            page_names = {command.qualified_name.lower() for command in chunk}
            kept = [str(name).lower() for name in (current.get("blocked_commands") or []) if str(name).lower() not in page_names]
            kept.extend(value.lower() for value in command_select.values)
            update_guild_settings(self.bot.storage_path, self.guild_id, {"blocked_commands": sorted(set(kept))})
            await self.refresh(interaction)

        command_select.callback = _on_commands
        self.add_item(command_select)

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
            emoji="📌",
            style=discord.ButtonStyle.success if sticky_enabled else discord.ButtonStyle.secondary,
        )
        sticky_btn.callback = self._on_toggle_sticky_roles
        self.add_item(sticky_btn)

        warn_settings_btn = discord.ui.Button(
            label=f"Warn Threshold/Duration ({settings.get('warn_threshold', 0)} / {settings.get('warn_mute_duration', '1h')})",
            emoji="⚖️",
            style=discord.ButtonStyle.secondary,
        )
        warn_settings_btn.callback = self._on_edit_warn_settings
        self.add_item(warn_settings_btn)

    def _build_modlog_items(self, settings: dict) -> None:
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

        enabled_events = settings.get("mod_log_events")
        enabled_set = (
            {str(v).lower() for v in enabled_events} if isinstance(enabled_events, list) else set(MOD_LOG_EVENT_KINDS)
        )
        events_select = discord.ui.Select(
            placeholder="Which actions get posted to the mod log",
            min_values=0,
            max_values=len(MOD_LOG_EVENT_KINDS),
            options=[
                discord.SelectOption(
                    label=MOD_LOG_EVENT_LABELS[kind][0],
                    value=kind,
                    emoji=MOD_LOG_EVENT_LABELS[kind][1],
                    default=kind in enabled_set,
                )
                for kind in MOD_LOG_EVENT_KINDS
            ],
        )

        async def _on_events(interaction: discord.Interaction) -> None:
            update_guild_settings(self.bot.storage_path, self.guild_id, {"mod_log_events": list(events_select.values)})
            await self.refresh(interaction)

        events_select.callback = _on_events
        self.add_item(events_select)

        dm_enabled = bool(settings.get("dm_on_mod_action", True))
        dm_btn = discord.ui.Button(
            label=f"DM Members On Action: {'On' if dm_enabled else 'Off'}",
            emoji="✉️",
            style=discord.ButtonStyle.success if dm_enabled else discord.ButtonStyle.secondary,
        )
        dm_btn.callback = self._on_toggle_dm_on_mod_action
        self.add_item(dm_btn)

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

        leave_ids = settings.get("leave_channel_ids") or []
        current_leave_channel = leave_ids[0] if leave_ids else None
        leave_channel_select = discord.ui.ChannelSelect(
            placeholder="Leave-message channel (empty = same as welcome)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=int(current_leave_channel))] if current_leave_channel else [],
        )

        async def _on_leave_channel(interaction: discord.Interaction) -> None:
            values = leave_channel_select.values
            update_guild_settings(
                self.bot.storage_path, self.guild_id, {"leave_channel_ids": [str(values[0].id)] if values else []}
            )
            await self.refresh(interaction)

        leave_channel_select.callback = _on_leave_channel
        self.add_item(leave_channel_select)

        join_btn = discord.ui.Button(label="Edit Join Message", emoji="📥", style=discord.ButtonStyle.secondary)
        join_btn.callback = self._on_edit_join_message
        self.add_item(join_btn)

        leave_btn = discord.ui.Button(label="Edit Leave Message", emoji="📤", style=discord.ButtonStyle.secondary)
        leave_btn.callback = self._on_edit_leave_message
        self.add_item(leave_btn)

        delete_after = int(settings.get("welcome_delete_after", 0) or 0)
        delete_btn = discord.ui.Button(
            label=f"Auto-Delete ({f'{delete_after}s' if delete_after else 'Off'})",
            emoji="🧹",
            style=discord.ButtonStyle.secondary,
        )
        delete_btn.callback = self._on_edit_welcome_delete_after
        self.add_item(delete_btn)

    def _build_starboard_items(self, settings: dict) -> None:
        current_channel = settings.get("starboard_channel_id")
        channel_select = discord.ui.ChannelSelect(
            placeholder="Starboard channel (empty = off)",
            channel_types=[discord.ChannelType.text],
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=int(current_channel))] if current_channel else [],
        )

        async def _on_starboard_channel(interaction: discord.Interaction) -> None:
            values = channel_select.values
            update_guild_settings(
                self.bot.storage_path, self.guild_id, {"starboard_channel_id": str(values[0].id) if values else ""}
            )
            await self.refresh(interaction)

        channel_select.callback = _on_starboard_channel
        self.add_item(channel_select)

        threshold_btn = discord.ui.Button(
            label=f"Star Threshold ({settings.get('starboard_threshold', 3)})", emoji="⭐", style=discord.ButtonStyle.secondary
        )
        threshold_btn.callback = self._on_edit_starboard_threshold
        self.add_item(threshold_btn)

        emoji_btn = discord.ui.Button(
            label=f"Star Emoji ({settings.get('starboard_emoji') or '⭐'})", emoji="✨", style=discord.ButtonStyle.secondary
        )
        emoji_btn.callback = self._on_edit_starboard_emoji
        self.add_item(emoji_btn)

        selfstar_allowed = bool(settings.get("starboard_allow_selfstar", True))
        selfstar_btn = discord.ui.Button(
            label=f"Self-Stars: {'Count' if selfstar_allowed else 'Ignored'}",
            emoji="🙋",
            style=discord.ButtonStyle.success if selfstar_allowed else discord.ButtonStyle.secondary,
        )
        selfstar_btn.callback = self._on_toggle_selfstar
        self.add_item(selfstar_btn)

        ignore_nsfw = bool(settings.get("starboard_ignore_nsfw", False))
        nsfw_btn = discord.ui.Button(
            label=f"NSFW Channels: {'Ignored' if ignore_nsfw else 'Included'}",
            emoji="🔞",
            style=discord.ButtonStyle.success if ignore_nsfw else discord.ButtonStyle.secondary,
        )
        nsfw_btn.callback = self._on_toggle_starboard_nsfw
        self.add_item(nsfw_btn)

    def _build_music_items(self, settings: dict) -> None:
        dj_enabled = bool(settings.get("music_dj_enabled"))
        dj_btn = discord.ui.Button(
            label=f"DJ Mode: {'On' if dj_enabled else 'Off'}",
            emoji="🎧",
            style=discord.ButtonStyle.success if dj_enabled else discord.ButtonStyle.secondary,
        )
        dj_btn.callback = self._on_toggle_dj_mode
        self.add_item(dj_btn)

        dj_role_display = str(settings.get("music_dj_role_name", "dj"))
        if len(dj_role_display) > 40:
            dj_role_display = dj_role_display[:37] + "..."
        dj_role_btn = discord.ui.Button(
            label=f"DJ Role Name ({dj_role_display})", emoji="🏷️", style=discord.ButtonStyle.secondary
        )
        dj_role_btn.callback = self._on_edit_dj_role_name
        self.add_item(dj_role_btn)

        djadmin_id = settings.get("music_djadmin_role_id")
        djadmin_select = discord.ui.RoleSelect(
            placeholder="DJ Admin role (always allowed to control music)",
            min_values=0,
            max_values=1,
            default_values=[discord.Object(id=int(djadmin_id))] if djadmin_id else [],
        )

        async def _on_djadmin_role(interaction: discord.Interaction) -> None:
            values = djadmin_select.values
            update_guild_settings(
                self.bot.storage_path, self.guild_id, {"music_djadmin_role_id": str(values[0].id) if values else ""}
            )
            await self.refresh(interaction)

        djadmin_select.callback = _on_djadmin_role
        self.add_item(djadmin_select)

        volume_btn = discord.ui.Button(
            label=f"Default Volume ({settings.get('music_default_volume', 50)}%)", emoji="🔊", style=discord.ButtonStyle.secondary
        )
        volume_btn.callback = self._on_edit_default_volume
        self.add_item(volume_btn)

        max_queue = int(settings.get("music_max_queue_length", 0) or 0)
        queue_btn = discord.ui.Button(
            label=f"Max Queue ({max_queue if max_queue else 'Default'})", emoji="📃", style=discord.ButtonStyle.secondary
        )
        queue_btn.callback = self._on_edit_max_queue_length
        self.add_item(queue_btn)

        max_minutes = int(settings.get("music_max_track_minutes", 0) or 0)
        length_btn = discord.ui.Button(
            label=f"Max Track Length ({f'{max_minutes}m' if max_minutes else 'Default'})",
            emoji="⏱️",
            style=discord.ButtonStyle.secondary,
        )
        length_btn.callback = self._on_edit_max_track_minutes
        self.add_item(length_btn)

    # ---------- button/modal callbacks ----------

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
                int(settings.get("warn_window_days", 0) or 0),
            )
        )

    async def _on_edit_starboard_threshold(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        await interaction.response.send_modal(
            StarboardThresholdModal(self, int(settings.get("starboard_threshold", 3) or 3))
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

    async def _on_toggle_dm_on_mod_action(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        update_guild_settings(
            self.bot.storage_path, self.guild_id, {"dm_on_mod_action": not settings.get("dm_on_mod_action", True)}
        )
        await self.refresh(interaction)

    async def _on_edit_starboard_emoji(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if not value or len(value) > 64 or any(ch.isspace() for ch in value):
                return "Give a single emoji (a standard one like ⭐, or a custom one as `<:name:id>`)."
            return None

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Starboard Emoji",
                label="Emoji that counts toward the starboard",
                key="starboard_emoji",
                default=str(settings.get("starboard_emoji") or "⭐"),
                max_length=64,
                validator=_validate,
            )
        )

    async def _on_toggle_selfstar(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        update_guild_settings(
            self.bot.storage_path, self.guild_id, {"starboard_allow_selfstar": not settings.get("starboard_allow_selfstar", True)}
        )
        await self.refresh(interaction)

    async def _on_toggle_starboard_nsfw(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)
        update_guild_settings(
            self.bot.storage_path, self.guild_id, {"starboard_ignore_nsfw": not settings.get("starboard_ignore_nsfw", False)}
        )
        await self.refresh(interaction)

    async def _on_edit_welcome_delete_after(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if not value.isdigit() or not (0 <= int(value) <= 3600):
                return "Give a whole number of seconds between 0 and 3600 (0 keeps messages forever)."
            return None

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Auto-Delete Greetings",
                label="Delete after N seconds (0 = keep)",
                key="welcome_delete_after",
                default=str(settings.get("welcome_delete_after", 0) or 0),
                max_length=4,
                validator=_validate,
                transform=int,
            )
        )

    async def _on_edit_embed_color(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if value and parse_hex_color(value) is None:
                return "Use a hex color like `#5865F2`, or leave the field empty for the default."
            return None

        def _transform(value: str) -> str:
            return f"#{value.lstrip('#').upper()}" if value else ""

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Embed Color",
                label="Hex color, e.g. #5865F2 (empty = default)",
                key="embed_color",
                default=str(settings.get("embed_color") or ""),
                max_length=7,
                validator=_validate,
                transform=_transform,
                required=False,
            )
        )

    async def _on_edit_max_queue_length(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if not value.isdigit() or not (0 <= int(value) <= 100):
                return "Give a whole number between 0 and 100 (0 = the default cap of 100)."
            return None

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Max Queue Length",
                label="Tracks allowed in the queue (0 = default)",
                key="music_max_queue_length",
                default=str(settings.get("music_max_queue_length", 0) or 0),
                max_length=3,
                validator=_validate,
                transform=int,
            )
        )

    async def _on_edit_max_track_minutes(self, interaction: discord.Interaction) -> None:
        settings = get_guild_settings(self.bot.storage_path, self.guild_id)

        def _validate(value: str) -> str | None:
            if not value.isdigit() or not (0 <= int(value) <= 180):
                return "Give a whole number of minutes between 0 and 180 (0 = the default cap of 3 hours)."
            return None

        await interaction.response.send_modal(
            SingleFieldModal(
                self,
                title="Max Track Length",
                label="Longest allowed track in minutes (0 = default)",
                key="music_max_track_minutes",
                default=str(settings.get("music_max_track_minutes", 0) or 0),
                max_length=3,
                validator=_validate,
                transform=int,
            )
        )

    # ---------- embed ----------

    @staticmethod
    def _on_off(enabled: bool, detail: str | None = None) -> str:
        if enabled:
            return f"🟢 On — {detail}" if detail else "🟢 On"
        return "⚫ Off"

    def _build_embed(self, settings: dict) -> discord.Embed:
        if self._confirming_reset:
            return self.bot.embeds.warning_embed(
                "Reset To Defaults?",
                "This will erase every setting for this server and restore the defaults. This cannot be undone.",
            )

        label, emoji, blurb = next(
            (label, emoji, blurb) for value, label, emoji, blurb in CATEGORIES if value == self.category
        )
        embed = self.bot.embeds.create(title=f"{emoji} Server Settings — {label}", color=self.bot.embeds.default_color)
        embed.description = blurb
        embed.set_footer(text="Changes save instantly • Only you can see this panel")

        if self.category == "general":
            embed.add_field(name="Listen To Bots", value=self._on_off(bool(settings.get("listen_to_bots"))), inline=True)
            embed_color = str(settings.get("embed_color") or "")
            embed.add_field(name="Embed Color", value=f"`{embed_color.upper()}`" if embed_color else "Default", inline=True)
            bot_channels = settings.get("bot_channels") or []
            embed.add_field(
                name="Bot Channels",
                value=_format_channels(bot_channels) if bot_channels else "All channels",
                inline=False,
            )
        elif self.category == "commands":
            blocked = sorted(settings.get("blocked_commands") or [])
            value = _format_commands(blocked)
            while len(value) > 1024 and blocked:
                blocked.pop()
                value = _format_commands(blocked) + " …"
            embed.add_field(name=f"Disabled Commands ({len(settings.get('blocked_commands') or [])})", value=value, inline=False)
        elif self.category == "logging":
            logging_enabled = bool(settings.get("logging_enabled")) and bool(settings.get("logging_channel_id"))
            detail = f"<#{int(settings['logging_channel_id'])}>" if logging_enabled else None
            embed.add_field(name="Activity Logging", value=self._on_off(logging_enabled, detail), inline=False)
            embed.add_field(name="Ignored Channels", value=_format_channels(settings.get("ignored_log_channels") or []), inline=True)
            embed.add_field(name="Ignored Users", value=_format_users(settings.get("ignored_users") or []), inline=True)
        elif self.category == "moderation":
            embed.add_field(name="Sticky Roles", value=self._on_off(bool(settings.get("sticky_roles"))), inline=True)
            embed.add_field(name="Autoroles", value=_format_roles(settings.get("autoroles") or []), inline=False)
            threshold = int(settings.get("warn_threshold", 0) or 0)
            if threshold > 0:
                escalation = f"After **{threshold}** warning(s): **{settings.get('warn_action', 'none')}**" + (
                    f" ({settings.get('warn_mute_duration', '1h')})" if settings.get("warn_action") == "mute" else ""
                )
                if int(settings.get("warn_window_days", 0) or 0) > 0:
                    escalation += f"\nOnly counting warnings from the last **{settings.get('warn_window_days')}** day(s)"
            else:
                escalation = "⚫ Off — no automatic action on warnings"
            embed.add_field(name="Warning Escalation", value=escalation, inline=False)
        elif self.category == "modlog":
            mod_log_enabled = bool(settings.get("mod_log_enabled")) and bool(settings.get("mod_log_channel_id"))
            detail = f"<#{int(settings['mod_log_channel_id'])}>" if mod_log_enabled else None
            embed.add_field(name="Moderation Log", value=self._on_off(mod_log_enabled, detail), inline=False)
            enabled_events = settings.get("mod_log_events")
            enabled_set = (
                {str(v).lower() for v in enabled_events} if isinstance(enabled_events, list) else set(MOD_LOG_EVENT_KINDS)
            )
            logged = [MOD_LOG_EVENT_LABELS[kind][0] for kind in MOD_LOG_EVENT_KINDS if kind in enabled_set]
            embed.add_field(name="Logged Actions", value=", ".join(logged) if logged else "None", inline=True)
            embed.add_field(name="DM Members On Action", value=self._on_off(bool(settings.get("dm_on_mod_action", True))), inline=True)
        elif self.category == "welcome":
            welcome_ids = settings.get("welcome_channel_ids") or []
            embed.add_field(
                name="Welcome Channel",
                value=self._on_off(bool(welcome_ids), _format_channels(welcome_ids) if welcome_ids else None),
                inline=True,
            )
            leave_ids = settings.get("leave_channel_ids") or []
            embed.add_field(
                name="Leave Channel",
                value=_format_channels(leave_ids) if leave_ids else "Same as welcome",
                inline=True,
            )
            delete_after = int(settings.get("welcome_delete_after", 0) or 0)
            embed.add_field(
                name="Auto-Delete",
                value=f"After {delete_after} second(s)" if delete_after else "⚫ Off",
                inline=True,
            )
            embed.add_field(name="Join Message", value=settings.get("join_message") or "*(not set)*", inline=False)
            embed.add_field(name="Leave Message", value=settings.get("leave_message") or "*(not set)*", inline=False)
        elif self.category == "starboard":
            channel_id = settings.get("starboard_channel_id")
            detail = f"<#{int(channel_id)}>" if channel_id else None
            star_emoji = str(settings.get("starboard_emoji") or "⭐")
            embed.add_field(name="Starboard", value=self._on_off(bool(channel_id), detail), inline=False)
            embed.add_field(
                name="Star Threshold",
                value=f"Messages need **{settings.get('starboard_threshold', 3)}** {star_emoji} reaction(s) to be posted.",
                inline=False,
            )
            embed.add_field(name="Emoji", value=star_emoji, inline=True)
            embed.add_field(
                name="Self-Stars",
                value="Count" if settings.get("starboard_allow_selfstar", True) else "Ignored",
                inline=True,
            )
            embed.add_field(
                name="NSFW Channels",
                value="Ignored" if settings.get("starboard_ignore_nsfw", False) else "Included",
                inline=True,
            )
        elif self.category == "music":
            embed.add_field(name="DJ Mode", value=self._on_off(bool(settings.get("music_dj_enabled"))), inline=True)
            embed.add_field(name="DJ Role Name", value=f"`{settings.get('music_dj_role_name', 'dj')}`", inline=True)
            djadmin_id = settings.get("music_djadmin_role_id")
            embed.add_field(name="DJ Admin Role", value=f"<@&{int(djadmin_id)}>" if djadmin_id else "None", inline=True)
            embed.add_field(name="Default Volume", value=f"{settings.get('music_default_volume', 50)}%", inline=True)
            max_queue = int(settings.get("music_max_queue_length", 0) or 0)
            embed.add_field(name="Max Queue", value=f"{max_queue} track(s)" if max_queue else "Default (100)", inline=True)
            max_minutes = int(settings.get("music_max_track_minutes", 0) or 0)
            embed.add_field(
                name="Max Track Length", value=f"{max_minutes} minute(s)" if max_minutes else "Default (3h)", inline=True
            )

        return embed


class Settings(commands.Cog):
    """Server settings: a single interactive dashboard rather than a list of
    separate slash commands admins have to already know the names of."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        # A kicked-and-reinvited guild (or a replayed join event after an
        # outage) must keep its existing config - only seed defaults for
        # guilds we've truly never stored.
        if guild_settings_exists(self.bot.storage_path, guild.id):
            return
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
