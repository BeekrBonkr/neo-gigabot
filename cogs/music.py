from __future__ import annotations

from dataclasses import dataclass, field

import discord
from discord import app_commands
from discord.ext import commands


@dataclass
class GuildMusicState:
    queue: list[str] = field(default_factory=list)
    now_playing: str | None = None


class Music(commands.Cog):
    """Music playback and playlist migration target."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}

    def get_state(self, guild_id: int) -> GuildMusicState:
        return self.guild_states.setdefault(guild_id, GuildMusicState())

    @app_commands.command(name="queue", description="Show the current music queue.")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Server Only",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return

        state = self.get_state(interaction.guild.id)
        if not state.queue:
            await self.bot.embeds.info_interaction(
                interaction,
                "Queue",
                "The queue is empty.",
            )
            return

        fields = [
            self.bot.embeds.field("Upcoming", "\n".join(f"- {item}" for item in state.queue[:10]))
        ]
        await self.bot.embeds.respond(
            interaction,
            title="Current Queue",
            description=(
                f"Now playing: `{state.now_playing}`"
                if state.now_playing
                else "Nothing is currently playing."
            ),
            fields=fields,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
