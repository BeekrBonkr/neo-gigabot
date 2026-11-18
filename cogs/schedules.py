from __future__ import annotations

import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import schedules as store
from utils.modules import module_enabled
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


def describe_action(entry: dict) -> str:
    """One line saying what a scheduled action does, for lists."""
    action, options = entry.get("action", "message"), entry.get("options") or {}
    channel = f"<#{entry['channel_id']}>"
    if action == "lock":
        return f"Lock {channel}"
    if action == "unlock":
        return f"Unlock {channel}"
    if action == "slowmode":
        return f"Set slowmode in {channel} to {options.get('seconds', 0)}s" if options.get("seconds") else f"Turn slowmode off in {channel}"
    if action == "purge":
        return f"Clear the last {options.get('count', 0)} messages in {channel}"
    if action in ("role_add", "role_remove"):
        return f"{'Give' if action == 'role_add' else 'Take'} <@&{options.get('role_id', 0)}> {'to' if action == 'role_add' else 'from'} <@{options.get('user_id', 0)}>"
    return f"Post in {channel}"


class Schedules(commands.Cog):
    """Does things at a set time, once or on repeat: posts a message, locks or
    unlocks a channel, sets slowmode, clears a channel, gives or takes a role."""

    schedule = app_commands.Group(
        name="schedule", description="Messages and actions that run at a set time, once or on repeat.", guild_only=True,
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
            LOGGER.exception("Scheduled action run failed")

    @_runner.before_loop
    async def _wait_until_ready(self) -> None:
        await self.bot.wait_until_ready()

    async def run_due(self, now: datetime | None = None) -> None:
        for entry in store.due(self.bot.storage_path, now):
            guild = self.bot.get_guild(entry["guild_id"])
            needs_channel = store.ACTIONS.get(entry["action"], store.ACTIONS["message"])["channel"]
            channel = guild.get_channel(entry["channel_id"]) if guild and needs_channel else None
            if guild is None or (needs_channel and channel is None):
                # The server or channel is gone: there is nowhere to do this, ever.
                store.remove(self.bot.storage_path, entry["guild_id"], entry["id"])
                continue
            if not module_enabled(self.bot.storage_path, guild.id, "schedules"):
                continue  # kept, and picked up again when the module is turned back on
            try:
                await self.perform(guild, channel, entry)
            except discord.Forbidden:
                LOGGER.warning("Scheduled action %s (%s) in guild %s is missing permissions", entry["id"], entry["action"], guild.id)
            except discord.HTTPException as exc:
                LOGGER.warning("Scheduled action %s failed: %s", entry["id"], exc)
                continue  # transient: try again on the next tick
            store.finish_run(self.bot.storage_path, entry, now)

    async def perform(self, guild: discord.Guild, channel, entry: dict) -> None:
        """Carry out one scheduled action. Raises discord's errors for run_due to sort out."""
        action, options, content = entry["action"], entry["options"], entry["content"]
        reason = f"Scheduled action #{entry['id']}"

        async def note() -> None:
            if content:
                await channel.send(content, allowed_mentions=ADMIN_MENTIONS)

        if action in ("role_add", "role_remove"):
            member, role = guild.get_member(int(options.get("user_id", 0))), guild.get_role(int(options.get("role_id", 0)))
            if member is None or role is None or role.managed or role.is_default() or guild.me is None or role >= guild.me.top_role:
                LOGGER.warning("Scheduled action %s skipped: the member or role is gone, or the role is out of the bot's reach", entry["id"])
                return
            await (member.add_roles if action == "role_add" else member.remove_roles)(role, reason=reason)
        elif action in ("lock", "unlock"):
            overwrite = channel.overwrites_for(guild.default_role)
            overwrite.send_messages = False if action == "lock" else None
            if action == "lock":
                await note()  # before the lock, in case it shuts the bot out too
            await channel.set_permissions(guild.default_role, overwrite=overwrite, reason=reason)
            if action == "unlock":
                await note()
        elif action == "slowmode":
            await channel.edit(slowmode_delay=int(options.get("seconds", 0)), reason=reason)
            await note()
        elif action == "purge":
            await channel.purge(limit=int(options.get("count", 0)), check=lambda message: not message.pinned, reason=reason)
            await note()
        else:
            await channel.send(content, allowed_mentions=ADMIN_MENTIONS)

    @schedule.command(name="add", description="Schedule a message. For exact times and other actions, use the website.")
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

    @schedule.command(name="list", description="List this server's scheduled messages and actions.")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def list_schedules(self, interaction: discord.Interaction) -> None:
        entries = store.list_for_guild(self.bot.storage_path, interaction.guild.id)
        lines = []
        for entry in entries:
            moment = store.parse_utc(entry["next_run_at"])
            lines.append(f"`#{entry['id']}` {describe_action(entry)} {discord.utils.format_dt(moment, 'R') if moment else '?'}, "
                         f"{describe_interval(entry['interval_minutes'])}" + (f"\n> {entry['content'][:100]}" if entry["content"] else ""))
        website = site_link(self.bot, "website", f"/app/{interaction.guild.id}/scheduled")
        text = "\n".join(lines) or "Nothing is scheduled."
        if website:
            text += f"\n\n🌐 Pick an exact date and time, or schedule locks, slowmode, clean-ups and roles, on the {website}."
        await self.bot.embeds.respond(interaction, embed=self.bot.embeds.info_embed(f"Scheduled Actions ({len(entries)}/{store.MAX_SCHEDULES_PER_GUILD})", text[:4000]), ephemeral=True)

    @schedule.command(name="remove", description="Cancel a scheduled message or action.")
    @app_commands.describe(schedule_id="Its number, shown in /schedule list")
    @app_commands.checks.has_permissions(manage_guild=True)
    async def remove(self, interaction: discord.Interaction, schedule_id: int) -> None:
        if store.remove(self.bot.storage_path, interaction.guild.id, schedule_id) is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "Nothing is scheduled with that number here.", ephemeral=True)
            return
        await self.bot.embeds.success_interaction(interaction, "Schedule Removed", f"Cancelled `#{schedule_id}`.", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Schedules(bot))
