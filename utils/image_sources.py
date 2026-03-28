from __future__ import annotations

import re
import time

import aiohttp
import discord

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp")
HTTP_USER_AGENT = "neo-gigabot/1.0"
SOURCE_TTL_SECONDS = 60 * 15
MESSAGE_LINK_RE = re.compile(
    r"^https?://(?:canary\.|ptb\.)?discord(?:app)?\.com/channels/"
    r"(?P<guild_id>@me|\d+)/(?P<channel_id>\d+)/(?P<message_id>\d+)$"
)


class ImageSourceManager:
    def __init__(self, bot: discord.Client) -> None:
        self.bot = bot
        self._selected_sources: dict[int, dict[str, int | float]] = {}

    def prune_sources(self) -> None:
        now = time.time()
        stale = [
            user_id
            for user_id, data in self._selected_sources.items()
            if now - float(data["timestamp"]) > SOURCE_TTL_SECONDS
        ]
        for user_id in stale:
            self._selected_sources.pop(user_id, None)

    def remember_source(self, user_id: int, channel_id: int, message_id: int) -> None:
        self._selected_sources[user_id] = {
            "channel_id": channel_id,
            "message_id": message_id,
            "timestamp": time.time(),
        }

    def clear_source(self, user_id: int) -> bool:
        return self._selected_sources.pop(user_id, None) is not None

    def supported_attachment(self, attachment: discord.Attachment) -> bool:
        content_type = attachment.content_type or ""
        name = attachment.filename.lower()
        return content_type.startswith("image/") or name.endswith(SUPPORTED_EXTENSIONS)

    def message_has_image(self, message: discord.Message) -> bool:
        if any(self.supported_attachment(attachment) for attachment in message.attachments):
            return True

        for embed in message.embeds:
            if (embed.image and embed.image.url) or (embed.thumbnail and embed.thumbnail.url):
                return True

        return False

    async def fetch_bytes_from_url(self, url: str) -> bytes:
        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers={"User-Agent": HTTP_USER_AGENT}) as response:
                response.raise_for_status()
                return await response.read()

    def parse_message_link(self, message_link: str) -> tuple[str, int, int]:
        match = MESSAGE_LINK_RE.match(message_link.strip())
        if not match:
            raise ValueError("That is not a valid Discord message link.")

        guild_id = match.group("guild_id")
        channel_id = int(match.group("channel_id"))
        message_id = int(match.group("message_id"))
        return guild_id, channel_id, message_id

    async def get_channel_for_link(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        channel_id: int,
    ) -> discord.abc.Messageable:
        channel = None

        if guild_id == "@me":
            channel = self.bot.get_channel(channel_id)
            if channel is None:
                try:
                    channel = await self.bot.fetch_channel(channel_id)
                except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                    raise ValueError("I could not access that DM channel.")
            return channel

        guild_id_int = int(guild_id)
        guild = (
            interaction.guild
            if interaction.guild and interaction.guild.id == guild_id_int
            else self.bot.get_guild(guild_id_int)
        )
        if guild is None:
            try:
                guild = await self.bot.fetch_guild(guild_id_int)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                raise ValueError("I am not in that server or I cannot access it.")

        channel = guild.get_channel(channel_id) or guild.get_thread(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException):
                raise ValueError("I could not access the channel from that message link.")

        return channel

    async def fetch_message_from_link(
        self,
        interaction: discord.Interaction,
        message_link: str,
    ) -> discord.Message:
        guild_id, channel_id, message_id = self.parse_message_link(message_link)
        channel = await self.get_channel_for_link(interaction, guild_id, channel_id)

        if not hasattr(channel, "fetch_message"):
            raise ValueError("That message link does not point to a fetchable text channel.")

        try:
            return await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            raise ValueError(
                "I could not fetch that message. Make sure the link is valid and I can view that channel."
            )

    async def extract_image_from_message(self, message: discord.Message) -> tuple[bytes, str]:
        for attachment in message.attachments:
            if self.supported_attachment(attachment):
                return await attachment.read(), attachment.filename

        for embed in message.embeds:
            if embed.image and embed.image.url:
                return await self.fetch_bytes_from_url(embed.image.url), "embedded_image.png"
            if embed.thumbnail and embed.thumbnail.url:
                return await self.fetch_bytes_from_url(embed.thumbnail.url), "embedded_thumbnail.png"

        raise ValueError("That message does not contain a supported image.")

    async def selected_message_image(self, interaction: discord.Interaction) -> tuple[bytes, str] | None:
        self.prune_sources()
        source = self._selected_sources.get(interaction.user.id)
        if not source:
            return None

        channel_id = int(source["channel_id"])
        message_id = int(source["message_id"])

        if interaction.guild is not None:
            channel = interaction.guild.get_channel(channel_id) or interaction.guild.get_thread(channel_id)
        else:
            channel = self.bot.get_channel(channel_id)

        if channel is None or not hasattr(channel, "fetch_message"):
            self._selected_sources.pop(interaction.user.id, None)
            return None

        try:
            message = await channel.fetch_message(message_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            self._selected_sources.pop(interaction.user.id, None)
            return None

        return await self.extract_image_from_message(message)

    async def history_image(self, interaction: discord.Interaction) -> tuple[bytes, str]:
        channel = interaction.channel
        if channel is None or not hasattr(channel, "history"):
            raise ValueError("I could not inspect recent messages in this channel.")

        async for message in channel.history(limit=50):
            if not self.message_has_image(message):
                continue
            return await self.extract_image_from_message(message)

        raise ValueError("No recent image was found in the last 50 messages.")

    async def get_source_image(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> tuple[bytes, str]:
        if attachment is not None:
            if not self.supported_attachment(attachment):
                raise ValueError("That attachment is not a supported image.")
            return await attachment.read(), attachment.filename

        if message_link:
            message = await self.fetch_message_from_link(interaction, message_link)
            return await self.extract_image_from_message(message)

        selected = await self.selected_message_image(interaction)
        if selected is not None:
            return selected

        return await self.history_image(interaction)
