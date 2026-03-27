from __future__ import annotations

from dataclasses import dataclass, field

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

    @commands.command(name="queue")
    async def queue(self, ctx: commands.Context) -> None:
        if ctx.guild is None:
            await ctx.send("This command can only be used in a server.")
            return

        state = self.get_state(ctx.guild.id)
        if not state.queue:
            await ctx.send("The queue is empty.")
            return

        await ctx.send("Current queue:\n" + "\n".join(f"- {item}" for item in state.queue[:10]))


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
