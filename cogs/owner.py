from __future__ import annotations

import platform
import time
from datetime import timedelta

import discord
import psutil
from discord import app_commands
from discord.ext import commands

from utils.command_log import log_path_for_date, parse_day
from utils.migration import default_legacy_path, migrate_all

ACTIVITY_TYPE_CHOICES = [
    app_commands.Choice(name="playing", value="playing"),
    app_commands.Choice(name="watching", value="watching"),
    app_commands.Choice(name="listening", value="listening"),
    app_commands.Choice(name="competing", value="competing"),
]
ACTIVITY_TYPE_MAP = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}

_PROCESS = psutil.Process()


def _format_duration(delta: timedelta) -> str:
    total_seconds = max(0, int(delta.total_seconds()))
    days, remainder = divmod(total_seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes, seconds = divmod(remainder, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if not parts or seconds:
        parts.append(f"{seconds}s")
    return " ".join(parts)


@app_commands.allowed_contexts(guilds=False, dms=True, private_channels=False)
class Owner(commands.GroupCog, group_name="owner", group_description="Owner-only maintenance commands"):
    """Owner-only diagnostics, maintenance, and multi-server management commands.

    Restricted to DMs (see the allowed_contexts decorator above) so /owner never
    shows up in any server's slash-command autocomplete - not just blocked from
    running there. The interaction_check below still gates *who* can run it: this
    only controls *where it's visible*, and someone who directly DMs the bot
    themselves would still see it listed (though still unable to run it).
    """

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.bot.config.owner_id:
            await self.bot.embeds.error_interaction(
                interaction,
                "Owner Only",
                "Only the configured owner can use this command.",
                ephemeral=True,
            )
            return False
        return True

    @app_commands.command(name="reload", description="Reload a cog extension.")
    @app_commands.describe(extension="Dotted extension path, e.g. cogs.music")
    async def reload_extension(self, interaction: discord.Interaction, extension: str) -> None:
        try:
            await self.bot.reload_extension(extension)
        except commands.ExtensionError as exc:
            await self.bot.embeds.error_interaction(interaction, "Reload Failed", f"```{exc}```", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(
            interaction,
            "Reloaded Extension",
            f"Reloaded `{extension}` successfully.",
            ephemeral=True,
        )

    @app_commands.command(name="system", description="Show host system and process resource usage.")
    async def system(self, interaction: discord.Interaction) -> None:
        cpu_percent = psutil.cpu_percent(interval=0.2)
        virtual_memory = psutil.virtual_memory()
        disk_usage = psutil.disk_usage("/")
        process_memory = _PROCESS.memory_info().rss / (1024 * 1024)
        process_uptime = _format_duration(timedelta(seconds=time.time() - _PROCESS.create_time()))

        embed = self.bot.embeds.create(
            title="🖥️ System Status",
            color=discord.Color.dark_teal(),
            fields=[
                self.bot.embeds.field("CPU Usage", f"{cpu_percent:.1f}%", True),
                self.bot.embeds.field("RAM Usage", f"{virtual_memory.percent:.1f}%", True),
                self.bot.embeds.field("Disk Usage", f"{disk_usage.percent:.1f}%", True),
                self.bot.embeds.field("Process Memory", f"{process_memory:.1f} MB", True),
                self.bot.embeds.field("Process Uptime", process_uptime, True),
                self.bot.embeds.field("Gateway Latency", f"{self.bot.latency * 1000:.0f} ms", True),
                self.bot.embeds.field("Python", platform.python_version(), True),
                self.bot.embeds.field("discord.py", discord.__version__, True),
                self.bot.embeds.field("Platform", platform.system(), True),
            ],
        )
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    @app_commands.command(name="avatar", description="Change the bot's global avatar.")
    @app_commands.describe(attachment="New avatar image", image_url="Or a direct URL to an image")
    async def avatar(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        image_url: str | None = None,
    ) -> None:
        if attachment is None and not image_url:
            await self.bot.embeds.error_interaction(
                interaction, "Missing Image", "Provide an attachment or an image URL.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        try:
            if attachment is not None:
                image_bytes = await attachment.read()
            else:
                import aiohttp

                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(image_url, headers={"User-Agent": "neo-gigabot/1.0"}) as response:
                        if response.status != 200:
                            raise RuntimeError(f"Could not download that image (HTTP {response.status}).")
                        image_bytes = await response.read()

            await self.bot.user.edit(avatar=image_bytes)
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed(
                    "Avatar Update Failed",
                    f"Discord rejected that image (must be a valid PNG/JPG/GIF, 128x128 to 4096x4096, under 10MB): {exc}",
                )
            )
            return
        except Exception as exc:  # noqa: BLE001
            await interaction.followup.send(embed=self.bot.embeds.error_embed("Avatar Update Failed", str(exc)))
            return

        await interaction.followup.send(embed=self.bot.embeds.success_embed("Avatar Updated", "The bot's avatar has been changed."))

    @app_commands.command(name="status", description="Change the bot's status/activity text.")
    @app_commands.describe(text="Status text to show", activity_type="Playing / Watching / Listening / Competing")
    @app_commands.choices(activity_type=ACTIVITY_TYPE_CHOICES)
    async def status(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 128],
        activity_type: app_commands.Choice[str] | None = None,
    ) -> None:
        chosen_type = ACTIVITY_TYPE_MAP[activity_type.value] if activity_type else discord.ActivityType.watching
        await self.bot.change_presence(activity=discord.Activity(type=chosen_type, name=text))
        await self.bot.embeds.success_interaction(
            interaction, "Status Updated", f"Now showing: {chosen_type.name} {text}", ephemeral=True
        )

    @app_commands.command(name="migrate", description="Import legacy gigabot data (settings + playlists) into this bot's storage.")
    @app_commands.describe(
        source_path="Root of the old gigabot install (defaults to a sibling 'gigabot' directory)",
        dry_run="Preview what would be migrated without writing anything (default: true)",
    )
    async def migrate(
        self,
        interaction: discord.Interaction,
        source_path: str | None = None,
        dry_run: bool = True,
    ) -> None:
        from pathlib import Path

        legacy_configured = getattr(self.bot.config, "legacy_data_path", "") or ""
        source = Path(source_path) if source_path else (Path(legacy_configured) if legacy_configured else default_legacy_path(self.bot.project_root))

        if not source.is_dir():
            await self.bot.embeds.error_interaction(
                interaction, "Source Not Found", f"`{source}` does not exist or is not a directory.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        summary = migrate_all(source, self.bot.storage_path, dry_run=dry_run)

        lines = [
            f"**Source:** `{source}`",
            f"**Mode:** {'Dry run (nothing written)' if dry_run else 'Applied'}",
            "",
            f"**Guild settings:** {summary.settings.migrated} migrated",
        ]
        if summary.settings.skipped_files:
            lines.append(f"Skipped (not a guild file): {', '.join(summary.settings.skipped_files)}")
        if summary.settings.failed:
            lines.append(f"⚠️ {len(summary.settings.failed)} failed - see logs.")
            for name, error in summary.settings.failed[:5]:
                self.bot.logger.warning("Settings migration failed for %s: %s", name, error)

        lines.append("")
        lines.append(f"**Playlists:** {summary.playlists.playlists_migrated} migrated, {summary.playlists.songs_migrated} songs")
        if summary.playlists.failed:
            lines.append(f"⚠️ {len(summary.playlists.failed)} failed - see logs.")
            for name, error in summary.playlists.failed[:5]:
                self.bot.logger.warning("Playlist migration failed for %s: %s", name, error)

        lines.append("")
        lines.append(f"**Reaction roles:** {summary.reaction_roles.mappings_migrated} mappings migrated")
        if summary.reaction_roles.empty_messages_skipped:
            lines.append(f"Skipped {summary.reaction_roles.empty_messages_skipped} message(s) with no mappings left.")
        if summary.reaction_roles.failed:
            lines.append(f"⚠️ {len(summary.reaction_roles.failed)} failed - see logs.")
            for name, error in summary.reaction_roles.failed[:5]:
                self.bot.logger.warning("Reaction role migration failed for %s: %s", name, error)

        if dry_run:
            lines.append("")
            lines.append("Re-run with `dry_run: False` to apply.")

        embed = self.bot.embeds.create(
            title="📦 Legacy Data Migration",
            description="\n".join(lines),
            color=discord.Color.green() if summary.ok else discord.Color.orange(),
        )
        await interaction.followup.send(embed=embed)

    @app_commands.command(name="commandlog", description="Download the command-usage log for a specific day.")
    @app_commands.describe(day="A date like 2026-07-05, or 'today' / 'yesterday'")
    async def commandlog(self, interaction: discord.Interaction, day: str = "today") -> None:
        parsed = parse_day(day)
        if parsed is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Invalid Date",
                "Use `YYYY-MM-DD`, `today`, or `yesterday`.",
                ephemeral=True,
            )
            return

        path = log_path_for_date(self.bot.storage_path, parsed)
        if not path.is_file() or path.stat().st_size == 0:
            await self.bot.embeds.info_interaction(
                interaction,
                "Nothing Logged",
                f"No commands were logged on `{parsed.isoformat()}`.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            await interaction.followup.send(
                content=f"Command log for `{parsed.isoformat()}`:",
                file=discord.File(path, filename=path.name),
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("Upload Failed", f"Could not upload that day's log: {exc}")
            )

    servers = app_commands.Group(name="servers", description="Inspect or leave servers this bot is in.")

    @servers.command(name="list", description="List every server this bot is in.")
    async def servers_list(self, interaction: discord.Interaction) -> None:
        guilds = sorted(self.bot.guilds, key=lambda g: (g.member_count or 0), reverse=True)
        lines = [f"`{guild.id}` - **{guild.name}** ({guild.member_count or '?'} members)" for guild in guilds[:40]]
        description = "\n".join(lines) if lines else "Not in any servers."
        if len(guilds) > 40:
            description += f"\n... and {len(guilds) - 40} more."

        embed = self.bot.embeds.info_embed(f"Servers ({len(guilds)})", description)
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    @servers.command(name="info", description="Show details about a specific server by ID.")
    @app_commands.describe(server_id="The server's ID")
    async def servers_info(self, interaction: discord.Interaction, server_id: str) -> None:
        guild = self._resolve_guild(server_id)
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "I'm not in a server with that ID.", ephemeral=True)
            return

        embed = self.bot.embeds.create(
            title=guild.name,
            color=discord.Color.blurple(),
            thumbnail_url=guild.icon.url if guild.icon else None,
            fields=[
                self.bot.embeds.field("ID", str(guild.id), True),
                self.bot.embeds.field("Owner", str(guild.owner) if guild.owner else str(guild.owner_id), True),
                self.bot.embeds.field("Members", str(guild.member_count), True),
                self.bot.embeds.field("Channels", str(len(guild.channels)), True),
                self.bot.embeds.field("Roles", str(len(guild.roles)), True),
                self.bot.embeds.field("Created", discord.utils.format_dt(guild.created_at, style="R"), True),
            ],
        )
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    @servers.command(name="channels", description="List the text channels in a specific server by ID.")
    @app_commands.describe(server_id="The server's ID")
    async def servers_channels(self, interaction: discord.Interaction, server_id: str) -> None:
        guild = self._resolve_guild(server_id)
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "I'm not in a server with that ID.", ephemeral=True)
            return

        text_channels = [c for c in guild.channels if isinstance(c, discord.TextChannel)]
        lines = [f"`{c.id}` - #{c.name}" for c in text_channels[:40]]
        description = "\n".join(lines) if lines else "No text channels."
        if len(text_channels) > 40:
            description += f"\n... and {len(text_channels) - 40} more."

        embed = self.bot.embeds.info_embed(f"Text Channels in {guild.name}", description)
        await self.bot.embeds.respond(interaction, embed=embed, ephemeral=True)

    @servers.command(name="invite", description="Generate an invite link for a specific server by ID, if possible.")
    @app_commands.describe(server_id="The server's ID")
    async def servers_invite(self, interaction: discord.Interaction, server_id: str) -> None:
        guild = self._resolve_guild(server_id)
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "I'm not in a server with that ID.", ephemeral=True)
            return

        target_channel = next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite),
            None,
        )
        if target_channel is None:
            await self.bot.embeds.error_interaction(
                interaction, "Cannot Create Invite", "I don't have permission to create an invite in any channel there.", ephemeral=True
            )
            return

        try:
            invite = await target_channel.create_invite(max_age=3600, max_uses=1, reason="Owner requested via /owner servers invite")
        except discord.Forbidden:
            await self.bot.embeds.error_interaction(interaction, "Cannot Create Invite", "Missing permission to create an invite.", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(
            interaction, "Invite Created", f"{invite.url} (expires in 1 hour, single use)", ephemeral=True
        )

    @servers.command(name="leave", description="Leave a specific server by ID.")
    @app_commands.describe(server_id="The server's ID")
    async def servers_leave(self, interaction: discord.Interaction, server_id: str) -> None:
        guild = self._resolve_guild(server_id)
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Not Found", "I'm not in a server with that ID.", ephemeral=True)
            return

        name = guild.name
        await guild.leave()
        await self.bot.embeds.success_interaction(interaction, "Left Server", f"Left **{name}** (`{guild.id}`).", ephemeral=True)

    def _resolve_guild(self, server_id: str) -> discord.Guild | None:
        try:
            return self.bot.get_guild(int(server_id))
        except ValueError:
            return None


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Owner(bot))
