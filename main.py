from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands

from utils.config import load_config
from utils.embeds import EmbedManager
from utils.storage import ensure_storage_layout

COGS = [
    "cogs.fun",
    "cogs.images",
    "cogs.moderation",
    "cogs.music",
    "cogs.settings",
    "cogs.owner",
]


class GigaBot(commands.Bot):
    def __init__(self) -> None:
        self.config = load_config()

        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.voice_states = True

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

    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        self.logger.exception("Unhandled app command error", exc_info=error)

        if isinstance(error, app_commands.CommandOnCooldown):
            await self.embeds.error_interaction(
                interaction,
                "Slow Down",
                f"Try again in `{error.retry_after:.1f}` seconds.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.MissingPermissions):
            missing = ", ".join(error.missing_permissions)
            await self.embeds.error_interaction(
                interaction,
                "Missing Permissions",
                f"You are missing: `{missing}`.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.CheckFailure):
            await self.embeds.error_interaction(
                interaction,
                "You Cannot Use That",
                "You do not meet the requirements to use this command.",
                ephemeral=True,
            )
            return

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
