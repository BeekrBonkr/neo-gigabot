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
            await self.bot.embeds.error(
                ctx,
                "Server Only",
                "This command can only be used in a server.",
            )
            return

        state = self.get_state(ctx.guild.id)
        if not state.queue:
            await self.bot.embeds.info(
                ctx,
                "Queue",
                "The queue is empty.",
            )
            return

        fields = [
            self.bot.embeds.field("Upcoming", "\n".join(f"- {item}" for item in state.queue[:10]))
        ]
        await self.bot.embeds.send(
            ctx,
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
