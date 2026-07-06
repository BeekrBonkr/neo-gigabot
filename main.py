from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils.command_log import record_command
from utils.config import load_config
from utils.embeds import EmbedManager
from utils.imaging import ImageToolkit
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

    async def setup_hook(self) -> None:
        ensure_storage_layout(self.storage_path)
        for extension in COGS:
            await self.load_extension(extension)
            self.logger.info("Loaded extension: %s", extension)

        synced = await self.tree.sync()
        self.logger.info("Synced %s application commands", len(synced))

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
