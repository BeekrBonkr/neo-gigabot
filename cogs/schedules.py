from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import schedules as store
from utils.durations import parse_duration
from utils.website import site_link

LOGGER = logging.getLogger(__name__)
# Written by admins, so role and @everyone pings are theirs to use, exactly as
# if they had posted the announcement themselves.
ADMIN_MENTIONS = discord.AllowedMentions(everyone=True, roles=True, users=True)


def describe_interval(minutes: int) -> str:
    if not minutes:
        return "once"
    for size, unit in ((10080, "week"), (1440, "day"), (60, "hour")):
        if minutes % size == 0:
            count = minutes // size
            return f"every {unit}" if count == 1 else f"every {count} {unit}s"
    return f"every {minutes} minutes"


class Schedules(commands.Cog):
    """Posts announcements at a set time, once or on repeat."""

    schedule = app_commands.Group(
        name="schedule", description="Messages posted at a set time, once or on repeat.", guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot

    async def cog_load(self) -> None:
        self._runner.start()

    async def cog_unload(self) -> None:
        self._runner.cancel()

    @tasks.loop(seconds=30)
    async def _runner(self) -> None:
        try:
            await self.run_due()
        except Exception:  # noqa: BLE001 - one bad row must not stop the schedule for everyone
            LOGGER.exception("Scheduled message run failed")

    @_runner.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def run_due(self, now: datetime | None = None) -> None:
        for entry in store.due(self.bot.storage_path, now):
            guild = self.bot.get_guild(entry["guild_id"])
            channel = guild.get_channel(entry["channel_id"]) if guild else None
            if channel is None:
                # The server or channel is gone: there is nowhere to post, ever.
                store.remove(self.bot.storage_path, entry["guild_id"], entry["id"])
                continue
            try:
                await channel.send(entry["content"], allowed_mentions=ADMIN_MENTIONS)
            except discord.Forbidden:
                LOGGER.warning("Scheduled message %s can't be posted in channel %s (missing permissions)", entry["id"], channel.id)
            except discord.HTTPException as exc:
                LOGGER.warning("Scheduled message %s failed: %s", entry["id"], exc)
                continue  # transient: try again on the next tick
            store.finish_run(self.bot.storage_path, entry, now)

    @schedule.command(name="add", description="Schedule a message. For an exact date and time, use the website.")
    @app_commands.describe(
        channel="Where to post it", message="What to post",
        when="How long from now, like 30m, 2h or 1d", repeat="Repeat every, like 1d or 1w (leave empty to post once)",
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def add(self, interaction: discord.Interaction, channel: discord.TextChannel, message: app_commands.Range[str, 1, store.MAX_CONTENT_LENGTH],
                  when: str, repeat: str | None = None) -> None:
        delay, every = parse_duration(when), (parse_duration(repeat) if repeat else None)
        if delay is None or (repeat and every is None):
            await self.bot.embeds.error_interaction(interaction, "Invalid Time", "Give times like `30m`, `2h`, `1d` or `1w`.", ephemeral=True)
            return
        run_at = datetime.now(timezone.utc) + delay
        try:
            entry = store.add(self.bot.storage_path, interaction.guild.id, channel.id, message.replace("\\n", "\n"), run_at,
                              int(every.total_seconds() // 60) if every else 0, interaction.user.id)
        except store.ScheduleError as exc:
            await self.bot.embeds.error_interaction(interaction, "Not Scheduled", str(exc), ephemeral=True)
            return
        await self.bot.embeds.success_interaction(
            interaction, "Message Scheduled",
            f"`#{entry['id']}` will be posted in {channel.mention} {discord.utils.format_dt(run_at, 'R')}, {describe_interval(entry['interval_minutes'])}.", ephemeral=True,
        )

    @schedule.command(name="list", description="List this server's scheduled messages.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_schedules(self, interaction: discord.Interaction) -> None:
        entries = store.list_for_guild(self.bot.storage_path, interaction.guild.id)
        lines = []
        for entry in entries:
            moment = store.parse_utc(entry["next_run_at"])
            lines.append(f"`#{entry['id']}` <#{entry['channel_id']}> {discord.utils.format_dt(moment, 'R') if moment else '?'}, "
                         f"{describe_interval(entry['interval_minutes'])}\n> {entry['content'][:100]}")
        website = site_link(self.bot, "website", f"/app/{interaction.guild.id}/scheduled")
        text = "\n".join(lines) or "Nothing is scheduled."
        if website:
            text += f"\n\n🌐 Schedule for an exact date and time on the {website}."
        await self.bot.embeds.respond(interaction, embed=self.bot.embeds.info_embed(f"Scheduled Messages ({len(entries)}/{store.MAX_SCHEDULES_PER_GUILD})", text[:4000]), ephemeral=True)

    @schedule.command(name="remove", description="Cancel a scheduled message.")
    @app_commands.describe(schedule_id="The message's number, shown in /schedule list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, schedule_id: int) -> None:
        if store.remove(self.bot.storage_path, interaction.guild.id, schedule_id) is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "There is no scheduled message with that number here.", ephemeral=True)
            return
        await self.bot.embeds.success_interaction(interaction, "Schedule Removed", f"Cancelled `#{schedule_id}`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Schedules(bot))
