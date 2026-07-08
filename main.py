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

from utils.command_log import record_command
from utils.config import load_config
from utils.embeds import EmbedManager
from utils.imaging import ImageToolkit
from utils.settings import get_guild_settings, migrate_blocked_command_names
from utils.storage import ensure_storage_layout

COGS = [
    "cogs.help",
    "cogs.info",
    "cogs.fun",
    "cogs.images",
    "cogs.logging",
    "cogs.moderation",
    "cogs.music",
    "cogs.settings",
    "cogs.welcome",
    "cogs.reactionroles",
    "cogs.starboard",
    "cogs.owner",
]


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
            help_command=None,
            case_insensitive=True,
        )

        self.project_root = Path(__file__).resolve().parent
        self.storage_path = self.project_root / "storage"
        self.logger = logging.getLogger(__name__)
        self.embeds = EmbedManager(self)
        self.image_tools = ImageToolkit(self)
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
        if interaction.guild is None:
            return True

        command = interaction.command
        if command is None or command.qualified_name == "settings":
            return True

        settings = get_guild_settings(self.storage_path, interaction.guild.id)
        blocked = {str(name).lower() for name in (settings.get("blocked_commands") or [])}
        if command.qualified_name.lower() in blocked:
            await self.embeds.error_interaction(
                interaction,
                "Command Disabled",
                f"`/{command.qualified_name}` is disabled in this server.",
                ephemeral=True,
            )
            return False

        return True

    async def ensure_command_allowed(self, interaction: discord.Interaction, command_name: str) -> bool:
        """Shared per-guild gate for opt-in-restricted commands: the blocked-command
        list and the bot-channel allowlist, in one settings read. Sends the
        rejection message itself and returns False if the command shouldn't run."""
        if interaction.guild is None or interaction.channel is None:
            return True

        settings = get_guild_settings(self.storage_path, interaction.guild.id)

        # Blocks are stored by qualified name; the caller-supplied name is the
        # leaf ("jpegify"), so prefer the interaction's own command identity.
        blocked = {str(name).lower() for name in (settings.get("blocked_commands") or [])}
        qualified = interaction.command.qualified_name.lower() if interaction.command else command_name.lower()
        if qualified in blocked or command_name.lower() in blocked:
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
                f"You are missing: `{missing}`.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.TransformerError):
            await record_command(self.storage_path, interaction, command, status="INVALID_VALUE", error=str(error))
            await self.embeds.error_interaction(
                interaction,
                "Invalid Value",
                "One of the values you entered was invalid.",
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
