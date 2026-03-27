from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import discord
from discord.ext import commands

from utils.config import load_config
from utils.settings import flush_settings_async, initialize_settings
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
        intents.message_content = True
        intents.members = True
        intents.guilds = True
        intents.messages = True
        intents.reactions = True
        intents.voice_states = True

        super().__init__(
            command_prefix=self.get_dynamic_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
        )

        self.project_root = Path(__file__).resolve().parent
        self.storage_path = self.project_root / "storage"
        self.logger = logging.getLogger(__name__)

    async def setup_hook(self) -> None:
        ensure_storage_layout(self.storage_path)
        initialize_settings(self.storage_path)

        for extension in COGS:
            await self.load_extension(extension)
            self.logger.info("Loaded extension: %s", extension)

        synced = await self.tree.sync()
        self.logger.info("Synced %s application commands", len(synced))

    async def get_dynamic_prefix(
        self,
        bot: commands.Bot,
        message: discord.Message,
    ) -> list[str]:
        prefix = self.config.default_prefix
        if message.guild is not None:
            from utils.settings import get_guild_settings

            guild_settings = get_guild_settings(self.storage_path, message.guild.id)
            prefix = guild_settings.get("prefix", self.config.default_prefix)
        return commands.when_mentioned_or(prefix)(bot, message)

    async def on_ready(self) -> None:
        self.logger.info(
            "Logged in as %s (%s)",
            self.user,
            self.user.id if self.user else "unknown",
        )

    async def close(self) -> None:
        await flush_settings_async()
        await super().close()


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
