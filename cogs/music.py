from __future__ import annotations

import asyncio
import logging
import random
import shutil
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from utils.command_policy import ensure_command_allowed
from utils.settings import get_guild_settings

LOGGER = logging.getLogger(__name__)

YTDL_OPTIONS: dict[str, Any] = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "default_search": "ytsearch",
    "extract_flat": False,
    "source_address": "0.0.0.0",
}

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
IDLE_DISCONNECT_SECONDS = 180
MAX_QUEUE_SIZE = 100
MAX_TRACK_LENGTH_SECONDS = 60 * 60 * 3
QUEUE_PAGE_SIZE = 10
SEARCH_RESULT_LIMIT = 5
FFMPEG_EXECUTABLE = shutil.which("ffmpeg")


def is_probably_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def format_duration(seconds: int | None) -> str:
    if not seconds:
        return "Unknown"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def truncate(value: str, limit: int = 80) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 1]}…"


@dataclass(slots=True)
class Track:
    title: str
    webpage_url: str
    uploader: str | None = None
    duration: int | None = None
    thumbnail: str | None = None
    stream_url: str | None = None
    requested_by_id: int | None = None
    requested_by_name: str | None = None

    @property
    def duration_label(self) -> str:
        return format_duration(self.duration)

    @classmethod
    def from_extracted_info(
        cls,
        info: dict[str, Any],
        *,
        requester: discord.abc.User | discord.Member | None = None,
    ) -> "Track":
        return cls(
            title=str(info.get("title") or "Unknown title"),
            webpage_url=str(
                info.get("webpage_url")
                or info.get("original_url")
                or info.get("url")
                or ""
            ),
            uploader=info.get("uploader") or info.get("channel") or info.get("uploader_id"),
            duration=info.get("duration"),
            thumbnail=info.get("thumbnail"),
            stream_url=info.get("url"),
            requested_by_id=requester.id if requester else None,
            requested_by_name=str(requester) if requester else None,
        )


@dataclass(slots=True)
class GuildMusicState:
    guild_id: int
    queue: deque[Track] = field(default_factory=deque)
    current: Track | None = None
    volume: float = 0.5
    text_channel_id: int | None = None
    playback_task: asyncio.Task[None] | None = None
    idle_disconnect_task: asyncio.Task[None] | None = None

    def clear_queue(self) -> None:
        self.queue.clear()
        self.current = None


class MusicControlsView(discord.ui.View):
    def __init__(self, cog: "Music", guild_id: int) -> None:
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = guild_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild_id != self.guild_id:
            await self.cog.bot.embeds.error_interaction(
                interaction,
                "Music Controls",
                "These controls belong to a different server.",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="Pause", style=discord.ButtonStyle.secondary)
    async def pause_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button["MusicControlsView"],
    ) -> None:
        await self.cog.control_pause(interaction)

    @discord.ui.button(label="Resume", style=discord.ButtonStyle.secondary)
    async def resume_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button["MusicControlsView"],
    ) -> None:
        await self.cog.control_resume(interaction)

    @discord.ui.button(label="Skip", style=discord.ButtonStyle.primary)
    async def skip_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button["MusicControlsView"],
    ) -> None:
        await self.cog.control_skip(interaction)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button["MusicControlsView"],
    ) -> None:
        await self.cog.control_stop(interaction)

    @discord.ui.button(label="Queue", style=discord.ButtonStyle.secondary)
    async def queue_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button["MusicControlsView"],
    ) -> None:
        embed = await self.cog.build_queue_embed(interaction.guild_id or self.guild_id, page=1)
        await self.cog.bot.embeds.respond(interaction, embed=embed, ephemeral=True)


class SearchChoiceSelect(discord.ui.Select["SearchChoiceView"]):
    def __init__(self, tracks: list[Track]) -> None:
        options = []
        for index, track in enumerate(tracks, start=1):
            details = f"{track.duration_label}"
            if track.uploader:
                details = f"{details} • {truncate(track.uploader, 45)}"
            options.append(
                discord.SelectOption(
                    label=truncate(track.title, 100),
                    description=truncate(details, 100),
                    value=str(index - 1),
                )
            )
        super().__init__(
            placeholder="Choose a track to queue",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.handle_selection(interaction, int(self.values[0]))


class SearchChoiceView(discord.ui.View):
    def __init__(
        self,
        cog: "Music",
        requester_id: int,
        guild_id: int,
        channel_id: int,
        tracks: list[Track],
    ) -> None:
        super().__init__(timeout=45)
        self.cog = cog
        self.requester_id = requester_id
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.tracks = tracks
        self.resolved = False
        self.add_item(SearchChoiceSelect(tracks))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.requester_id:
            await self.cog.bot.embeds.error_interaction(
                interaction,
                "Search Results",
                "Only the person who ran the command can choose a result.",
                ephemeral=True,
            )
            return False
        return True

    async def handle_selection(self, interaction: discord.Interaction, index: int) -> None:
        if self.resolved:
            await self.cog.bot.embeds.info_interaction(
                interaction,
                "Search Results",
                "That picker has already been used.",
                ephemeral=True,
            )
            return

        self.resolved = True
        for child in self.children:
            child.disabled = True

        chosen = self.tracks[index]
        try:
            state = await self.cog.enqueue_tracks(self.guild_id, [chosen])
            state.text_channel_id = self.channel_id
            guild = interaction.guild
            if guild is not None:
                await self.cog.start_playback_if_needed(guild)
            await self.cog.bot.embeds.edit_interaction_response(
                interaction,
                title="Queued",
                description=f"Added [{chosen.title}]({chosen.webpage_url}) to the queue.",
                color=discord.Color.green(),
                fields=[
                    self.cog.bot.embeds.field("Duration", chosen.duration_label, True),
                    self.cog.bot.embeds.field("Uploader", chosen.uploader or "Unknown", True),
                    self.cog.bot.embeds.field(
                        "Requested By",
                        chosen.requested_by_name or interaction.user.display_name,
                        False,
                    ),
                ],
                thumbnail_url=chosen.thumbnail,
                view=self,
            )
        except Exception as exc:
            await self.cog.bot.embeds.edit_interaction_response(
                interaction,
                title="Could Not Queue Track",
                description=str(exc),
                color=discord.Color.red(),
                view=self,
            )

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button["SearchChoiceView"],
    ) -> None:
        if self.resolved:
            await self.cog.bot.embeds.info_interaction(
                interaction,
                "Search Results",
                "That picker has already been used.",
                ephemeral=True,
            )
            return
        self.resolved = True
        for child in self.children:
            child.disabled = True
        await self.cog.bot.embeds.edit_interaction_response(
            interaction,
            title="Cancelled",
            description="No track was added to the queue.",
            color=discord.Color.orange(),
            view=self,
        )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True


class Music(commands.Cog):
    music_group = app_commands.Group(name="music", description="Music playback commands.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}

    async def cog_unload(self) -> None:
        for state in self.guild_states.values():
            if state.playback_task and not state.playback_task.done():
                state.playback_task.cancel()
            if state.idle_disconnect_task and not state.idle_disconnect_task.done():
                state.idle_disconnect_task.cancel()

    def get_music_settings(self, guild_id: int) -> dict[str, Any]:
        settings = get_guild_settings(self.bot.storage_path, guild_id)
        default_volume = int(settings.get("music_default_volume", 50))
        return {
            "dj_enabled": bool(settings.get("music_dj_enabled", False)),
            "dj_role_name": str(settings.get("music_dj_role_name", "dj")),
            "default_volume": max(0, min(default_volume, 100)),
        }

    def get_state(self, guild_id: int) -> GuildMusicState:
        if guild_id not in self.guild_states:
            config = self.get_music_settings(guild_id)
            self.guild_states[guild_id] = GuildMusicState(
                guild_id=guild_id,
                volume=config["default_volume"] / 100,
            )
        return self.guild_states[guild_id]

    def reset_session_volume(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        config = self.get_music_settings(guild_id)
        state.volume = config["default_volume"] / 100

    def queue_duration(self, state: GuildMusicState) -> int:
        return sum(track.duration or 0 for track in state.queue)

    async def ensure_music_allowed(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        command_name = interaction.command.qualified_name if interaction.command else "music"
        allowed = await ensure_command_allowed(
            self.bot,
            interaction,
            command_name,
            allow_dm=False,
        )
        if not allowed:
            return False, None

        assert interaction.guild is not None
        config = self.get_music_settings(interaction.guild.id)
        if not config["dj_enabled"]:
            return True, None

        member = interaction.user
        if not isinstance(member, discord.Member):
            return False, "Unable to validate your roles."
        if discord.utils.get(member.roles, name=config["dj_role_name"]):
            return True, None
        return False, f"DJ mode is enabled.\nYou need the `{config['dj_role_name']}` role."

    async def ensure_user_voice_channel(
        self,
        interaction: discord.Interaction,
    ) -> discord.VoiceChannel | discord.StageChannel | None:
        if not isinstance(interaction.user, discord.Member):
            return None
        voice_state = interaction.user.voice
        if voice_state is None or voice_state.channel is None:
            return None
        return voice_state.channel

    async def ensure_voice_client(
        self,
        interaction: discord.Interaction,
        target_channel: discord.VoiceChannel | discord.StageChannel,
    ) -> discord.VoiceClient:
        assert interaction.guild is not None
        voice_client = interaction.guild.voice_client
        state = self.get_state(interaction.guild.id)

        if voice_client is None:
            self.reset_session_volume(interaction.guild.id)
            state.text_channel_id = interaction.channel_id
            voice_client = await target_channel.connect(self_deaf=True)
            return voice_client

        if voice_client.channel == target_channel:
            state.text_channel_id = interaction.channel_id
            return voice_client

        if voice_client.is_playing() or voice_client.is_paused():
            raise RuntimeError("I am already playing music in another voice channel.")

        self.reset_session_volume(interaction.guild.id)
        state.text_channel_id = interaction.channel_id
        await voice_client.move_to(target_channel)
        return voice_client

    async def extract_track(
        self,
        query: str,
        *,
        requester: discord.abc.User | discord.Member,
    ) -> Track:
        loop = asyncio.get_running_loop()

        def _extract() -> dict[str, Any]:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                payload = query if is_probably_url(query) else f"ytsearch1:{query}"
                data = ydl.extract_info(payload, download=False)
                if data is None:
                    raise RuntimeError("No results found.")
                if "entries" in data:
                    entries = [entry for entry in data["entries"] if entry]
                    if not entries:
                        raise RuntimeError("No results found.")
                    data = entries[0]
                return data

        info = await loop.run_in_executor(None, _extract)
        track = Track.from_extracted_info(info, requester=requester)
        if track.duration and track.duration > MAX_TRACK_LENGTH_SECONDS:
            raise RuntimeError("That track is longer than 3 hours.")
        if not track.webpage_url:
            raise RuntimeError("I could not resolve a playable track.")
        return track

    async def search_tracks(
        self,
        query: str,
        *,
        requester: discord.abc.User | discord.Member,
        limit: int = SEARCH_RESULT_LIMIT,
    ) -> list[Track]:
        loop = asyncio.get_running_loop()

        def _search() -> list[dict[str, Any]]:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                data = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
                if data is None or "entries" not in data:
                    raise RuntimeError("No results found.")
                return [entry for entry in data["entries"] if entry]

        entries = await loop.run_in_executor(None, _search)
        tracks: list[Track] = []
        for entry in entries:
            track = Track.from_extracted_info(entry, requester=requester)
            if not track.webpage_url:
                continue
            if track.duration and track.duration > MAX_TRACK_LENGTH_SECONDS:
                continue
            tracks.append(track)
        if not tracks:
            raise RuntimeError("No results found.")
        return tracks[:limit]

    async def refresh_stream_url(self, track: Track) -> str:
        loop = asyncio.get_running_loop()

        def _refresh() -> str:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                info = ydl.extract_info(track.webpage_url, download=False)
                if info is None:
                    raise RuntimeError("Could not refresh the track URL.")
                if "entries" in info:
                    entries = [entry for entry in info["entries"] if entry]
                    if not entries:
                        raise RuntimeError("Could not refresh the track URL.")
                    info = entries[0]
                stream_url = info.get("url")
                if not stream_url:
                    raise RuntimeError("Could not get a stream URL.")
                return str(stream_url)

        stream_url = await loop.run_in_executor(None, _refresh)
        track.stream_url = stream_url
        return stream_url

    async def create_source(self, track: Track, volume: float) -> discord.PCMVolumeTransformer:
        stream_url = track.stream_url or await self.refresh_stream_url(track)
        if FFMPEG_EXECUTABLE is None:
            raise RuntimeError("ffmpeg is not installed or not in PATH.")
        audio = discord.FFmpegPCMAudio(
            stream_url,
            executable=FFMPEG_EXECUTABLE,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)

    async def enqueue_tracks(self, guild_id: int, tracks: list[Track]) -> GuildMusicState:
        state = self.get_state(guild_id)
        for track in tracks:
            if len(state.queue) >= MAX_QUEUE_SIZE:
                raise RuntimeError(f"The queue is full. Maximum size is {MAX_QUEUE_SIZE}.")
            state.queue.append(track)
        return state

    async def start_playback_if_needed(self, guild: discord.Guild) -> None:
        state = self.get_state(guild.id)
        if state.playback_task and not state.playback_task.done():
            return
        state.playback_task = asyncio.create_task(
            self.player_loop(guild.id),
            name=f"music-player-{guild.id}",
        )

    async def schedule_idle_disconnect(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.idle_disconnect_task and not state.idle_disconnect_task.done():
            state.idle_disconnect_task.cancel()

        async def _idle_disconnect() -> None:
            try:
                await asyncio.sleep(IDLE_DISCONNECT_SECONDS)
                guild = self.bot.get_guild(guild_id)
                if guild is None or guild.voice_client is None:
                    return
                if guild.voice_client.is_playing() or guild.voice_client.is_paused():
                    return
                if any(not member.bot for member in guild.voice_client.channel.members):
                    return
                state.clear_queue()
                self.reset_session_volume(guild_id)
                await guild.voice_client.disconnect(force=False)
                text_channel = self.bot.get_channel(state.text_channel_id) if state.text_channel_id else None
                if isinstance(text_channel, discord.abc.Messageable):
                    await self.bot.embeds.info(
                        text_channel,
                        "Disconnected",
                        f"Left the voice channel because the queue stayed empty for {IDLE_DISCONNECT_SECONDS} seconds.",
                    )
            except asyncio.CancelledError:
                pass
            finally:
                state.idle_disconnect_task = None

        state.idle_disconnect_task = asyncio.create_task(
            _idle_disconnect(),
            name=f"idle-disconnect-{guild_id}",
        )

    async def player_loop(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        try:
            while True:
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    state.clear_queue()
                    return

                voice_client = guild.voice_client
                if voice_client is None or not voice_client.is_connected():
                    state.clear_queue()
                    return

                if not state.queue:
                    state.current = None
                    await self.schedule_idle_disconnect(guild_id)
                    return

                if state.idle_disconnect_task and not state.idle_disconnect_task.done():
                    state.idle_disconnect_task.cancel()

                track = state.queue.popleft()
                state.current = track
                text_channel = self.bot.get_channel(state.text_channel_id) if state.text_channel_id else None

                try:
                    source = await self.create_source(track, state.volume)
                except Exception as exc:
                    LOGGER.exception(
                        "Failed to create audio source for %s",
                        track.webpage_url,
                        exc_info=exc,
                    )
                    if isinstance(text_channel, discord.abc.Messageable):
                        await self.bot.embeds.error(
                            text_channel,
                            "Playback Error",
                            f"Could not play `{track.title}`. Skipping it.",
                        )
                    state.current = None
                    continue

                finished = asyncio.Event()

                def _after(error: Exception | None) -> None:
                    if error:
                        LOGGER.error("Voice playback after-callback error: %s", error)
                    self.bot.loop.call_soon_threadsafe(finished.set)

                voice_client.play(source, after=_after)
                if isinstance(text_channel, discord.abc.Messageable):
                    await self.bot.embeds.send(
                        text_channel,
                        title="Now Playing",
                        description=f"[{track.title}]({track.webpage_url})",
                        fields=[
                            self.bot.embeds.field("Duration", track.duration_label, True),
                            self.bot.embeds.field("Uploader", track.uploader or "Unknown", True),
                            self.bot.embeds.field(
                                "Requested By",
                                track.requested_by_name or "Unknown",
                                False,
                            ),
                        ],
                        thumbnail_url=track.thumbnail,
                        footer=(
                            f"Volume: {round(state.volume * 100)}% • "
                            f"Idle disconnect: {IDLE_DISCONNECT_SECONDS}s"
                        ),
                        view=MusicControlsView(self, guild_id),
                    )

                await finished.wait()
                state.current = None
        except asyncio.CancelledError:
            raise
        finally:
            state.playback_task = None

    def build_queue_lines(self, state: GuildMusicState, page: int) -> tuple[str, int]:
        queue_items = list(state.queue)
        total_pages = max(1, (len(queue_items) + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE)
        page = max(1, min(page, total_pages))
        start = (page - 1) * QUEUE_PAGE_SIZE
        end = start + QUEUE_PAGE_SIZE
        page_items = queue_items[start:end]

        if not page_items:
            return "The queue is empty.", total_pages

        lines = []
        for absolute_index, track in enumerate(page_items, start=start + 1):
            requester = f" • requested by {track.requested_by_name}" if track.requested_by_name else ""
            lines.append(
                f"`{absolute_index}.` [{truncate(track.title, 70)}]({track.webpage_url}) "
                f"• `{track.duration_label}`{requester}"
            )
        return "\n".join(lines), total_pages

    async def build_queue_embed(self, guild_id: int, page: int = 1) -> discord.Embed:
        state = self.get_state(guild_id)
        description = "Nothing is currently playing."
        if state.current:
            description = (
                f"Now playing: [{state.current.title}]({state.current.webpage_url})\n"
                f"Requested by: `{state.current.requested_by_name or 'Unknown'}`"
            )

        upcoming, total_pages = self.build_queue_lines(state, page)
        total_runtime = format_duration(self.queue_duration(state))

        return self.bot.embeds.info_embed(
            "Music Queue",
            description,
            fields=[
                self.bot.embeds.field("Up Next", upcoming),
                self.bot.embeds.field("Queued Tracks", str(len(state.queue)), True),
                self.bot.embeds.field("Queue Runtime", total_runtime, True),
                self.bot.embeds.field("Page", f"{max(1, min(page, total_pages))}/{total_pages}", True),
            ],
            footer=(
                f"Volume: {round(state.volume * 100)}% • "
                f"Idle disconnect: {IDLE_DISCONNECT_SECONDS}s"
            ),
        )

    async def build_now_playing_embed(self, guild_id: int) -> discord.Embed:
        state = self.get_state(guild_id)
        track = state.current
        if track is None:
            return self.bot.embeds.info_embed(
                "Now Playing",
                "Nothing is currently playing.",
                footer=f"Idle disconnect: {IDLE_DISCONNECT_SECONDS}s",
            )
        return self.bot.embeds.info_embed(
            "Now Playing",
            f"[{track.title}]({track.webpage_url})",
            fields=[
                self.bot.embeds.field("Duration", track.duration_label, True),
                self.bot.embeds.field("Uploader", track.uploader or "Unknown", True),
                self.bot.embeds.field("Requested By", track.requested_by_name or "Unknown", False),
                self.bot.embeds.field("Queued After This", str(len(state.queue)), True),
                self.bot.embeds.field("Upcoming Runtime", format_duration(self.queue_duration(state)), True),
            ],
            thumbnail_url=track.thumbnail,
            footer=(
                f"Volume: {round(state.volume * 100)}% • "
                f"Idle disconnect: {IDLE_DISCONNECT_SECONDS}s"
            ),
        )

    def stop_playback_task(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.playback_task and not state.playback_task.done():
            state.playback_task.cancel()

    def remove_queue_item(self, guild_id: int, position: int) -> Track:
        state = self.get_state(guild_id)
        if position < 1 or position > len(state.queue):
            raise IndexError("That queue position does not exist.")
        items = list(state.queue)
        removed = items.pop(position - 1)
        state.queue = deque(items)
        return removed

    def move_queue_item(self, guild_id: int, from_position: int, to_position: int) -> Track:
        state = self.get_state(guild_id)
        if from_position < 1 or from_position > len(state.queue):
            raise IndexError("The source queue position does not exist.")
        if to_position < 1 or to_position > len(state.queue):
            raise IndexError("The destination queue position does not exist.")
        items = list(state.queue)
        track = items.pop(from_position - 1)
        items.insert(to_position - 1, track)
        state.queue = deque(items)
        return track

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return

        guild = member.guild
        voice_client = guild.voice_client
        if voice_client is None or voice_client.channel is None:
            return
        if before.channel != voice_client.channel and after.channel != voice_client.channel:
            return

        non_bot_members = [m for m in voice_client.channel.members if not m.bot]
        if non_bot_members:
            return

        state = self.get_state(guild.id)
        state.clear_queue()
        self.stop_playback_task(guild.id)
        self.reset_session_volume(guild.id)
        try:
            await voice_client.disconnect(force=False)
        except Exception:
            LOGGER.exception("Failed to disconnect after voice channel emptied.")

    def _voice_channel_mismatch(self, interaction: discord.Interaction) -> str | None:
        guild = interaction.guild
        if guild is None or guild.voice_client is None or guild.voice_client.channel is None:
            return None

        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            return None

        if member.voice.channel.id != guild.voice_client.channel.id:
            return f"You must be in {guild.voice_client.channel.mention} to control playback."
        return None

    async def ensure_read_access(self, interaction: discord.Interaction) -> bool:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(
                interaction,
                "Music Unavailable",
                reason or "Not allowed.",
                ephemeral=True,
            )
            return False
        return True

    async def ensure_control_access(self, interaction: discord.Interaction) -> bool:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(
                interaction,
                "Music Unavailable",
                reason or "Not allowed.",
                ephemeral=True,
            )
            return False
        mismatch = self._voice_channel_mismatch(interaction)
        if mismatch:
            await self.bot.embeds.error_interaction(
                interaction,
                "Wrong Voice Channel",
                mismatch,
                ephemeral=True,
            )
            return False
        return True

    async def control_pause(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        guild = interaction.guild
        if guild is None or guild.voice_client is None or not guild.voice_client.is_playing():
            await self.bot.embeds.info_interaction(interaction, "Pause", "Nothing is currently playing.", ephemeral=True)
            return
        guild.voice_client.pause()
        await self.bot.embeds.success_interaction(interaction, "Paused", "Playback paused.", ephemeral=True)

    async def control_resume(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        guild = interaction.guild
        if guild is None or guild.voice_client is None or not guild.voice_client.is_paused():
            await self.bot.embeds.info_interaction(interaction, "Resume", "Playback is not paused.", ephemeral=True)
            return
        guild.voice_client.resume()
        await self.bot.embeds.success_interaction(interaction, "Resumed", "Playback resumed.", ephemeral=True)

    async def control_skip(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        guild = interaction.guild
        if guild is None or guild.voice_client is None or not guild.voice_client.is_playing():
            await self.bot.embeds.info_interaction(interaction, "Skip", "Nothing is currently playing.", ephemeral=True)
            return
        guild.voice_client.stop()
        await self.bot.embeds.success_interaction(interaction, "Skipped", "Skipped the current track.", ephemeral=True)

    async def control_stop(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Stop Failed",
                "This command can only be used in a server.",
                ephemeral=True,
            )
            return
        state = self.get_state(guild.id)
        state.clear_queue()
        self.stop_playback_task(guild.id)
        if guild.voice_client is not None:
            guild.voice_client.stop()
        await self.bot.embeds.success_interaction(
            interaction,
            "Stopped",
            "Playback stopped and queue cleared.",
            ephemeral=True,
        )

    @music_group.command(name="help", description="Show the music command guide.")
    @app_commands.guild_only()
    async def help_music(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_read_access(interaction):
            return
        await self.bot.embeds.respond(
            interaction,
            title="Music Help",
            description="Use `/music` for playback, queue management, and voice controls.",
            fields=[
                self.bot.embeds.field(
                    "Playback",
                    "`/music join`, `/music play`, `/music nowplaying`, `/music queue`, `/music leave`",
                ),
                self.bot.embeds.field(
                    "Controls",
                    "`/music pause`, `/music resume`, `/music skip`, `/music stop`, `/music volume`",
                ),
                self.bot.embeds.field(
                    "Queue Tools",
                    "`/music shuffle`, `/music remove`, `/music move`, `/music clearqueue`",
                ),
                self.bot.embeds.field(
                    "Notes",
                    (
                        "Searches show a result picker when you use a normal search term. "
                        f"Volume resets when the bot joins. Idle disconnect is {IDLE_DISCONNECT_SECONDS} seconds."
                    ),
                ),
            ],
        )

    @music_group.command(name="join", description="Join your current voice channel.")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_read_access(interaction):
            return
        channel = await self.ensure_user_voice_channel(interaction)
        if channel is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Not in Voice",
                "You need to join a voice channel first.",
                ephemeral=True,
            )
            return
        try:
            await self.ensure_voice_client(interaction, channel)
        except Exception as exc:
            await self.bot.embeds.error_interaction(
                interaction,
                "Could Not Join",
                str(exc),
                ephemeral=True,
            )
            return
        state = self.get_state(interaction.guild_id)
        await self.bot.embeds.success_interaction(
            interaction,
            "Connected",
            (
                f"Joined {channel.mention}.\n"
                f"Volume reset to `{round(state.volume * 100)}%`. "
                f"Idle disconnect: `{IDLE_DISCONNECT_SECONDS}s`."
            ),
        )

    @music_group.command(name="play", description="Play a track from a URL or search term.")
    @app_commands.describe(query="A song name, video title, or URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        if not await self.ensure_read_access(interaction):
            return

        channel = await self.ensure_user_voice_channel(interaction)
        if channel is None:
            await self.bot.embeds.error_interaction(
                interaction,
                "Not in Voice",
                "You need to join a voice channel first.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True)
        try:
            await self.ensure_voice_client(interaction, channel)
            if is_probably_url(query):
                track = await self.extract_track(query, requester=interaction.user)
                state = await self.enqueue_tracks(interaction.guild_id, [track])
                state.text_channel_id = interaction.channel_id
                guild = interaction.guild
                if guild is not None:
                    await self.start_playback_if_needed(guild)
                description = (
                    f"Queued [{track.title}]({track.webpage_url}). Playback will begin momentarily."
                    if state.current is None and len(state.queue) == 1
                    else f"Added [{track.title}]({track.webpage_url}) to the queue."
                )
                await self.bot.embeds.respond(
                    interaction,
                    title="Queued",
                    description=description,
                    fields=[
                        self.bot.embeds.field("Duration", track.duration_label, True),
                        self.bot.embeds.field("Uploader", track.uploader or "Unknown", True),
                        self.bot.embeds.field("Requested By", track.requested_by_name or "Unknown", False),
                    ],
                    thumbnail_url=track.thumbnail,
                )
                return

            results = await self.search_tracks(query, requester=interaction.user)
            top_result = results[0]
            description_lines = []
            for index, track in enumerate(results, start=1):
                uploader = f" • {track.uploader}" if track.uploader else ""
                description_lines.append(
                    f"`{index}.` [{truncate(track.title, 60)}]({track.webpage_url}) • `{track.duration_label}`{uploader}"
                )
            view = SearchChoiceView(
                self,
                requester_id=interaction.user.id,
                guild_id=interaction.guild_id,
                channel_id=interaction.channel_id,
                tracks=results,
            )
            await self.bot.embeds.respond(
                interaction,
                title="Choose a Track",
                description="Select one of the search results below.",
                fields=[
                    self.bot.embeds.field("Top Results", "\n".join(description_lines)),
                    self.bot.embeds.field(
                        "Quick Default",
                        f"Top match: [{top_result.title}]({top_result.webpage_url})",
                    ),
                ],
                thumbnail_url=top_result.thumbnail,
                view=view,
            )
        except Exception as exc:
            await self.bot.embeds.respond(
                interaction,
                title="Could Not Queue Track",
                description=str(exc),
                color=discord.Color.red(),
                ephemeral=True,
            )

    @music_group.command(name="queue", description="Show the current queue.")
    @app_commands.describe(page="Queue page number")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction, page: app_commands.Range[int, 1, 50] = 1) -> None:
        if not await self.ensure_read_access(interaction):
            return
        embed = await self.build_queue_embed(interaction.guild_id, page=page)
        await self.bot.embeds.respond(interaction, embed=embed)

    @music_group.command(name="nowplaying", description="Show the current track.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_read_access(interaction):
            return
        embed = await self.build_now_playing_embed(interaction.guild_id)
        await self.bot.embeds.respond(
            interaction,
            embed=embed,
            view=MusicControlsView(self, interaction.guild_id),
        )

    @music_group.command(name="skip", description="Skip the current track.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        await self.control_skip(interaction)

    @music_group.command(name="pause", description="Pause playback.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        await self.control_pause(interaction)

    @music_group.command(name="resume", description="Resume playback.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        await self.control_resume(interaction)

    @music_group.command(name="stop", description="Stop playback and clear the queue.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        await self.control_stop(interaction)

    @music_group.command(name="leave", description="Disconnect from voice and clear the queue.")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        guild = interaction.guild
        if guild is None or guild.voice_client is None:
            await self.bot.embeds.info_interaction(
                interaction,
                "Disconnected",
                "I am not in a voice channel.",
            )
            return
        state = self.get_state(guild.id)
        state.clear_queue()
        self.stop_playback_task(guild.id)
        self.reset_session_volume(guild.id)
        await guild.voice_client.disconnect(force=False)
        await self.bot.embeds.success_interaction(
            interaction,
            "Disconnected",
            "Left the voice channel and cleared the queue.",
        )

    @music_group.command(name="shuffle", description="Shuffle the queue.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        state = self.get_state(interaction.guild_id)
        if not state.queue:
            await self.bot.embeds.info_interaction(interaction, "Shuffle", "The queue is empty.")
            return
        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)
        await self.bot.embeds.success_interaction(
            interaction,
            "Shuffled",
            "The queue has been shuffled.",
        )

    @music_group.command(name="remove", description="Remove a track from the queue.")
    @app_commands.describe(position="The 1-based queue position to remove")
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, MAX_QUEUE_SIZE]) -> None:
        if not await self.ensure_control_access(interaction):
            return
        try:
            removed = self.remove_queue_item(interaction.guild_id, position)
        except IndexError as exc:
            await self.bot.embeds.error_interaction(interaction, "Remove Failed", str(exc), ephemeral=True)
            return
        await self.bot.embeds.success_interaction(
            interaction,
            "Removed",
            f"Removed [{removed.title}]({removed.webpage_url}) from the queue.",
        )

    @music_group.command(name="move", description="Move a queued track to a different position.")
    @app_commands.describe(
        from_position="The current 1-based queue position",
        to_position="The new 1-based queue position",
    )
    @app_commands.guild_only()
    async def move(
        self,
        interaction: discord.Interaction,
        from_position: app_commands.Range[int, 1, MAX_QUEUE_SIZE],
        to_position: app_commands.Range[int, 1, MAX_QUEUE_SIZE],
    ) -> None:
        if not await self.ensure_control_access(interaction):
            return
        try:
            moved = self.move_queue_item(interaction.guild_id, from_position, to_position)
        except IndexError as exc:
            await self.bot.embeds.error_interaction(interaction, "Move Failed", str(exc), ephemeral=True)
            return
        await self.bot.embeds.success_interaction(
            interaction,
            "Moved",
            f"Moved [{moved.title}]({moved.webpage_url}) to position `{to_position}`.",
        )

    @music_group.command(name="clearqueue", description="Clear queued tracks but keep the current track playing.")
    @app_commands.guild_only()
    async def clearqueue(self, interaction: discord.Interaction) -> None:
        if not await self.ensure_control_access(interaction):
            return
        state = self.get_state(interaction.guild_id)
        if not state.queue:
            await self.bot.embeds.info_interaction(interaction, "Clear Queue", "The queue is already empty.")
            return
        queue_count = len(state.queue)
        state.queue.clear()
        await self.bot.embeds.success_interaction(
            interaction,
            "Queue Cleared",
            f"Removed `{queue_count}` queued track(s). The current track was left alone.",
        )

    @music_group.command(name="volume", description="Set playback volume from 0 to 100.")
    @app_commands.describe(percent="Volume percentage")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]) -> None:
        if not await self.ensure_control_access(interaction):
            return
        state = self.get_state(interaction.guild_id)
        state.volume = percent / 100
        guild = interaction.guild
        if guild is not None and guild.voice_client is not None and guild.voice_client.source is not None:
            source = guild.voice_client.source
            if isinstance(source, discord.PCMVolumeTransformer):
                source.volume = state.volume
        await self.bot.embeds.success_interaction(
            interaction,
            "Volume Updated",
            (
                f"Volume set to `{percent}%` for this voice session. "
                "It will reset the next time I join."
            ),
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
