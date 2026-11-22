from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from utils import user_app
from utils.command_log import record_command
from utils.config import load_config
from utils.embeds import EmbedManager
from utils.imaging import ImageToolkit
from utils.modules import disabled_module_for
from utils.samples import SampleStore
from utils.settings import blocking_entry, get_guild_settings, migrate_blocked_command_names
from utils.storage import ensure_storage_layout
from utils.website import site_link

COGS = [
    "cogs.help",
    "cogs.info",
    "cogs.fun",
    "cogs.images",
    "cogs.image_filters",
    "cogs.image_animate",
    "cogs.image_memes",
    "cogs.image_edit",
    "cogs.audio",
    "cogs.image_menu",
    "cogs.logging",
    "cogs.moderation",
    "cogs.cases",
    "cogs.automod",
    "cogs.antiraid",
    "cogs.verification",
    "cogs.honeypot",
    "cogs.suggestions",
    "cogs.tags",
    "cogs.forms",
    "cogs.tempvoice",
    "cogs.schedules",
    "cogs.stickies",
    "cogs.music",
    "cogs.settings",
    "cogs.welcome",
    "cogs.reactionroles",
    "cogs.rolemenus",
    "cogs.starboard",
    "cogs.garden",
    "cogs.owner",
    "cogs.panel_bridge",
]


# Discord's own names for permissions, where they differ from the code name.
PERMISSION_NAMES = {
    "moderate_members": "Timeout Members", "manage_guild": "Manage Server", "manage_roles": "Manage Roles",
    "manage_channels": "Manage Channels", "manage_emojis_and_stickers": "Manage Expressions", "manage_expressions": "Manage Expressions",
    "view_audit_log": "View Audit Log", "read_message_history": "Read Message History", "external_emojis": "Use External Emojis",
    "use_external_emojis": "Use External Emojis", "manage_nicknames": "Manage Nicknames", "mention_everyone": "Mention @everyone",
}


def permission_names(names: list[str]) -> str:
    """Permissions as Discord's settings show them: "**Kick Members** and **Timeout Members**"."""
    shown = [f"**{PERMISSION_NAMES.get(name, name.replace('_', ' ').title())}**" for name in names]
    return shown[0] if len(shown) == 1 else ", ".join(shown[:-1]) + " and " + shown[-1]


def invalid_value_message(error: app_commands.TransformerError) -> str:
    """Which value was wrong, and what was expected there."""
    kind = getattr(getattr(error, "type", None), "name", "") or ""
    expected = {"user": "a member of this server", "channel": "a channel I can see", "role": "a role", "integer": "a whole number", "number": "a number"}.get(kind)
    value = str(getattr(error, "value", "") or "")[:80]
    if expected and kind == "user":
        return "That person is not in this server, and this command needs a member."
    if expected:
        return f"`{value}` is not {expected}." if value else f"One value should have been {expected}."
    return "One of the values you entered was invalid."


class GigaBot(commands.Bot):
    def __init__(self) -> None:
        self.config = load_config()

        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.voice_states = True
        # Needed for /snipe (reads Message.content of deleted messages). This is a
        # privileged intent - it must also be enabled in the Discord Developer Portal
        # under Bot -> Privileged Gateway Intents -> Message Content Intent.
        intents.message_content = True

        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            # A message that does not say otherwise can ping the people it names and nobody else:
            # never @everyone, @here or a role. The few places meant to (scheduled messages) say so.
            allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=True, replied_user=True),
            help_command=None,
            case_insensitive=True,
        )

        self.project_root = Path(__file__).resolve().parent
        self.storage_path = self.project_root / "storage"
        self.logger = logging.getLogger(__name__)
        self.embeds = EmbedManager(self)
        self.image_tools = ImageToolkit(self)
        self.samples = SampleStore(self)
        self.start_time = discord.utils.utcnow()
        self.tree.on_error = self.on_app_command_error
        self.tree.interaction_check = self.on_tree_interaction_check
        # One shared session for all outbound HTTP (Reddit JSON, weather, Giphy,
        # image downloads, ...) instead of a new session - and TCP/TLS handshake -
        # per request. Created in setup_hook (needs a running loop), closed in close().
        self.http_session: aiohttp.ClientSession | None = None

    async def setup_hook(self) -> None:
        self.http_session = aiohttp.ClientSession(headers={"User-Agent": "neo-gigabot/1.0"})
        ensure_storage_layout(self.storage_path)
        for extension in COGS:
            await self.load_extension(extension)
            self.logger.info("Loaded extension: %s", extension)

        # Blocked commands used to be stored by leaf name; upgrade any stored
        # lists to qualified names now that the full command tree is loaded.
        leaf_to_qualified: dict[str, list[str]] = {}
        for command in self.tree.walk_commands():
            if isinstance(command, app_commands.Command):
                leaf_to_qualified.setdefault(command.name.lower(), []).append(command.qualified_name.lower())
        migrated = migrate_blocked_command_names(self.storage_path, leaf_to_qualified)
        if migrated:
            self.logger.info("Migrated blocked-command lists to qualified names for %s guild(s)", migrated)

        await self.sync_commands_if_changed()

    def tree_fingerprint(self) -> str:
        """Hash of the exact registration payload Discord would receive, so a
        boot only calls tree.sync() when a command actually changed. Discord
        rate-limits command registration aggressively (~200 creates/day) - an
        unconditional sync on every boot plus a systemd crash-loop could burn
        through that and get registration blocked for the day."""
        payload = [command.to_dict(self.tree) for command in self.tree.get_commands(type=None)]
        payload.sort(key=lambda c: (c.get("type", 1), c.get("name", "")))
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()

    def _fingerprint_path(self) -> Path:
        return self.storage_path / "command_tree_fingerprint.txt"

    async def sync_commands_if_changed(self, *, force: bool = False) -> int | None:
        """Sync the command tree if its fingerprint differs from the last
        synced one (or always, with force=True). Returns the number of synced
        commands, or None when the sync was skipped."""
        user_app.mark_tree(self.tree)  # here, so commands from a reloaded cog are opened again too
        fingerprint = self.tree_fingerprint()
        path = self._fingerprint_path()
        stored = path.read_text(encoding="utf-8").strip() if path.is_file() else ""
        if not force and fingerprint == stored:
            self.logger.info("Command tree unchanged; skipping sync (use /owner sync to force)")
            return None

        synced = await self.tree.sync()
        path.write_text(fingerprint + "\n", encoding="utf-8")
        self.logger.info("Synced %s application commands", len(synced))
        return len(synced)

    async def on_tree_interaction_check(self, interaction: discord.Interaction) -> bool:
        """Tree-wide gate so the blocked-command list applies to *every* command,
        not just the cogs that remember to call ensure_command_allowed. /settings
        is exempt so a server can never lock itself out of unblocking."""
        if interaction.type is not discord.InteractionType.application_command:
            return True

        command = interaction.command
        if command is None:
            return True
        if not await user_app.gate(self, interaction, command):
            return False
        if command.qualified_name == "settings":
            return True
        # A menu's name is not a path of groups: "Edit Image" must not be
        # switched off by a block on the /edit group, so only its own name counts.
        return await self.command_gate(interaction, command.qualified_name, exact=isinstance(command, app_commands.ContextMenu))

    async def command_gate(self, interaction: discord.Interaction, qualified_name: str, *, exact: bool = False) -> bool:
        """Whether this server lets `qualified_name` run: not on its blocked
        list and not part of a module that is turned off. Sends the refusal
        itself. With `exact`, a block on a group of the same first word does
        not count. Also used for commands started some other way than by
        typing them, such as from the Edit Image menu. A server that has not
        added the bot has no say."""
        guild = user_app.home_guild(interaction)
        if guild is None:
            return True

        settings = get_guild_settings(self.storage_path, guild.id)
        blocked = settings.get("blocked_commands")
        if exact:
            is_blocked = qualified_name.lower() in {str(name).lower() for name in (blocked or [])}
        else:
            is_blocked = blocking_entry(qualified_name, blocked) is not None
        if is_blocked:
            await self.embeds.error_interaction(
                interaction,
                "Command Disabled",
                f"`/{qualified_name}` is disabled in this server.",
                ephemeral=True,
            )
            return False

        module = disabled_module_for(qualified_name, settings)
        if module is not None:
            where = site_link(self, "the website", f"/app/{guild.id}/settings")
            await self.embeds.error_interaction(
                interaction,
                "Turned Off",
                f"**{module}** is turned off in this server. An admin can turn it back on"
                + (f" on {where}." if where else " from the bot's website."),
                ephemeral=True,
            )
            return False

        return True

    async def ensure_command_allowed(self, interaction: discord.Interaction, command_name: str) -> bool:
        """Shared per-guild gate for opt-in-restricted commands: the blocked-command
        list and the bot-channel allowlist, in one settings read. Sends the
        rejection message itself and returns False if the command shouldn't run."""
        guild = user_app.home_guild(interaction)
        if guild is None or interaction.channel is None:
            return True

        settings = get_guild_settings(self.storage_path, guild.id)

        # Blocks are stored by qualified name; the caller-supplied name is the
        # leaf ("jpegify"), so prefer the interaction's own command identity.
        blocked = settings.get("blocked_commands")
        qualified = interaction.command.qualified_name.lower() if interaction.command else command_name.lower()
        if blocking_entry(qualified, blocked) is not None or blocking_entry(command_name, blocked) is not None:
            await self.embeds.error_interaction(
                interaction,
                "Command Blocked",
                f"`/{command_name}` is blocked in this server.",
                ephemeral=True,
            )
            return False

        bot_channels = settings.get("bot_channels") or []
        if bot_channels and int(interaction.channel.id) not in {int(c) for c in bot_channels}:
            await self.embeds.warning_interaction(
                interaction,
                "Wrong Channel",
                "This command can only be used in a configured bot channel.",
                ephemeral=True,
            )
            return False

        return True

    async def close(self) -> None:
        if self.http_session is not None and not self.http_session.closed:
            await self.http_session.close()
        await super().close()

    async def on_ready(self) -> None:
        self.logger.info(
            "Logged in as %s (%s)",
            self.user,
            self.user.id if self.user else "unknown",
        )

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: app_commands.Command | app_commands.ContextMenu,
    ) -> None:
        await record_command(self.storage_path, interaction, command, status="OK")

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        command = interaction.command

        if isinstance(error, app_commands.CommandOnCooldown):
            await record_command(self.storage_path, interaction, command, status="COOLDOWN")
            await self.embeds.error_interaction(
                interaction,
                "Slow Down",
                f"Try again in `{error.retry_after:.1f}` seconds.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.MissingPermissions):
            missing = ", ".join(error.missing_permissions)
            await record_command(self.storage_path, interaction, command, status="MISSING_PERMISSIONS", error=missing)
            await self.embeds.error_interaction(
                interaction,
                "Missing Permissions",
                f"You need the {permission_names(error.missing_permissions)} permission to use this command.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.BotMissingPermissions):
            # a CheckFailure, but not the person's doing: say that it is the bot that needs something
            missing = ", ".join(error.missing_permissions)
            await record_command(self.storage_path, interaction, command, status="BOT_MISSING_PERMISSIONS", error=missing)
            await self.embeds.error_interaction(
                interaction,
                "I Need More Permissions",
                f"I am missing the {permission_names(error.missing_permissions)} permission here. "
                "Give it to my role in Server Settings > Roles (or check this channel's permissions), then try again.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.NoPrivateMessage):
            await record_command(self.storage_path, interaction, command, status="CHECK_FAILED", error="guild only")
            await self.embeds.error_interaction(interaction, "Server Only", "This command only works in a server.", ephemeral=True)
            return

        if isinstance(error, app_commands.TransformerError):
            await record_command(self.storage_path, interaction, command, status="INVALID_VALUE", error=str(error))
            await self.embeds.error_interaction(
                interaction,
                "Invalid Value",
                invalid_value_message(error),
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.CheckFailure):
            await record_command(self.storage_path, interaction, command, status="CHECK_FAILED", error=str(error))
            await self.embeds.error_interaction(
                interaction,
                "You Cannot Use That",
                "You do not meet the requirements to use this command.",
                ephemeral=True,
            )
            return

        # Anything else is a genuine bug, not routine control flow - this is the
        # only branch worth a full traceback. discord.py wraps the command
        # body's real exception in CommandInvokeError; unwrap .original so the
        # command log and the logs show the actual exception type (e.g.
        # "ValueError") instead of "CommandInvokeError" for every single bug.
        original = getattr(error, "original", error)
        self.logger.exception("Unhandled app command error", exc_info=original)
        await record_command(self.storage_path, interaction, command, status="ERROR", error=type(original).__name__)
        await self.embeds.error_interaction(
            interaction,
            "Command Failed",
            "Something went wrong while running that command.",
            ephemeral=True,
        )


async def main() -> None:
    config = load_config()
    logging.basicConfig(
        level=getattr(logging, config.log_level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    bot = GigaBot()
    async with bot:
        await bot.start(config.token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
