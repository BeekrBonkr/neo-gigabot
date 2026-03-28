from __future__ import annotations

import asyncio
import logging
import random
from collections import deque
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import discord
import yt_dlp
from discord import app_commands
from discord.ext import commands

from utils.settings import get_guild_settings, is_bot_channel, update_guild_settings

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
            webpage_url=str(info.get("webpage_url") or info.get("original_url") or info.get("url") or ""),
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


class Music(commands.Cog):
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

    async def ensure_music_allowed(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        if interaction.guild is None or interaction.channel is None:
            return False, "This command can only be used in a server."

        if not is_bot_channel(self.bot.storage_path, interaction.guild.id, interaction.channel.id):
            settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
            bot_channels = settings.get("bot_channels", []) or []
            if bot_channels:
                mentions = ", ".join(f"<#{channel_id}>" for channel_id in bot_channels)
                return False, f"This command can only be used in {mentions}."
            return False, "This command is not allowed in this channel."

        config = self.get_music_settings(interaction.guild.id)
        if not config["dj_enabled"]:
            return True, None

        member = interaction.user
        if not isinstance(member, discord.Member):
            return False, "Unable to validate your roles."

        if discord.utils.get(member.roles, name=config["dj_role_name"]):
            return True, None

        return False, f"DJ mode is enabled. You need the `{config['dj_role_name']}` role."

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
        audio = discord.FFmpegPCMAudio(
            stream_url,
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
        state.playback_task = asyncio.create_task(self.player_loop(guild.id), name=f"music-player-{guild.id}")

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

                state.clear_queue()
                self.reset_session_volume(guild_id)
                await guild.voice_client.disconnect(force=False)

                text_channel = self.bot.get_channel(state.text_channel_id) if state.text_channel_id else None
                if isinstance(text_channel, discord.abc.Messageable):
                    await self.bot.embeds.info(
                        text_channel,
                        "Disconnected",
                        "Left the voice channel because the queue stayed empty.",
                    )
            except asyncio.CancelledError:
                pass
            finally:
                state.idle_disconnect_task = None

        state.idle_disconnect_task = asyncio.create_task(_idle_disconnect(), name=f"idle-disconnect-{guild_id}")

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
                    LOGGER.exception("Failed to create audio source for %s", track.webpage_url, exc_info=exc)
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
                            self.bot.embeds.field("Requested By", track.requested_by_name or "Unknown", False),
                        ],
                        thumbnail_url=track.thumbnail,
                    )

                await finished.wait()
                state.current = None

        except asyncio.CancelledError:
            raise
        finally:
            state.playback_task = None

    async def build_queue_embed(self, guild_id: int) -> discord.Embed:
        state = self.get_state(guild_id)

        description = "Nothing is currently playing."
        if state.current:
            description = f"Now playing: [{state.current.title}]({state.current.webpage_url})"

        if state.queue:
            upcoming = "\n".join(
                f"{index}. [{track.title}]({track.webpage_url}) • `{track.duration_label}`"
                for index, track in enumerate(list(state.queue)[:10], start=1)
            )
        else:
            upcoming = "The queue is empty."

        return self.bot.embeds.info_embed(
            "Music Queue",
            description,
            fields=[self.bot.embeds.field("Up Next", upcoming)],
            footer=f"Volume: {round(state.volume * 100)}%",
        )

    def stop_playback_task(self, guild_id: int) -> None:
        state = self.get_state(guild_id)
        if state.playback_task and not state.playback_task.done():
            state.playback_task.cancel()

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

    @app_commands.command(name="join", description="Join your current voice channel.")
    @app_commands.guild_only()
    async def join(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        channel = await self.ensure_user_voice_channel(interaction)
        if channel is None:
            await self.bot.embeds.error_interaction(interaction, "Not in Voice", "You need to join a voice channel first.", ephemeral=True)
            return

        try:
            await self.ensure_voice_client(interaction, channel)
        except Exception as exc:
            await self.bot.embeds.error_interaction(interaction, "Could Not Join", str(exc), ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        await self.bot.embeds.success_interaction(
            interaction,
            "Connected",
            f"Joined {channel.mention}. Volume reset to `{round(state.volume * 100)}%`.",
        )

    @app_commands.command(name="play", description="Play a track from a URL or search term.")
    @app_commands.describe(query="A song name, video title, or URL")
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        channel = await self.ensure_user_voice_channel(interaction)
        if channel is None:
            await self.bot.embeds.error_interaction(interaction, "Not in Voice", "You need to join a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)

        try:
            await self.ensure_voice_client(interaction, channel)
            track = await self.extract_track(query, requester=interaction.user)
            state = await self.enqueue_tracks(interaction.guild_id, [track])
            state.text_channel_id = interaction.channel_id

            guild = interaction.guild
            if guild is not None:
                await self.start_playback_if_needed(guild)

            if state.current is None and len(state.queue) == 1:
                description = f"Queued [{track.title}]({track.webpage_url}). Playback will begin momentarily."
            else:
                description = f"Added [{track.title}]({track.webpage_url}) to the queue."

            await self.bot.embeds.respond(
                interaction,
                title="Queued",
                description=description,
                fields=[
                    self.bot.embeds.field("Duration", track.duration_label, True),
                    self.bot.embeds.field("Uploader", track.uploader or "Unknown", True),
                ],
                thumbnail_url=track.thumbnail,
            )
        except Exception as exc:
            await self.bot.embeds.respond(
                interaction,
                title="Could Not Queue Track",
                description=str(exc),
                color=discord.Color.red(),
                ephemeral=True,
            )

    @app_commands.command(name="queue", description="Show the current queue.")
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        embed = await self.build_queue_embed(interaction.guild_id)
        await self.bot.embeds.respond(interaction, embed=embed)

    @app_commands.command(name="nowplaying", description="Show the current track.")
    @app_commands.guild_only()
    async def nowplaying(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        if state.current is None:
            await self.bot.embeds.info_interaction(interaction, "Now Playing", "Nothing is currently playing.")
            return

        track = state.current
        await self.bot.embeds.respond(
            interaction,
            title="Now Playing",
            description=f"[{track.title}]({track.webpage_url})",
            fields=[
                self.bot.embeds.field("Duration", track.duration_label, True),
                self.bot.embeds.field("Uploader", track.uploader or "Unknown", True),
                self.bot.embeds.field("Requested By", track.requested_by_name or "Unknown", False),
            ],
            thumbnail_url=track.thumbnail,
        )

    @app_commands.command(name="skip", description="Skip the current track.")
    @app_commands.guild_only()
    async def skip(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None or guild.voice_client is None or not guild.voice_client.is_playing():
            await self.bot.embeds.info_interaction(interaction, "Skip", "Nothing is currently playing.")
            return

        guild.voice_client.stop()
        await self.bot.embeds.success_interaction(interaction, "Skipped", "Skipped the current track.")

    @app_commands.command(name="pause", description="Pause playback.")
    @app_commands.guild_only()
    async def pause(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None or guild.voice_client is None or not guild.voice_client.is_playing():
            await self.bot.embeds.info_interaction(interaction, "Pause", "Nothing is currently playing.")
            return

        guild.voice_client.pause()
        await self.bot.embeds.success_interaction(interaction, "Paused", "Playback paused.")

    @app_commands.command(name="resume", description="Resume playback.")
    @app_commands.guild_only()
    async def resume(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None or guild.voice_client is None or not guild.voice_client.is_paused():
            await self.bot.embeds.info_interaction(interaction, "Resume", "Playback is not paused.")
            return

        guild.voice_client.resume()
        await self.bot.embeds.success_interaction(interaction, "Resumed", "Playback resumed.")

    @app_commands.command(name="stop", description="Stop playback and clear the queue.")
    @app_commands.guild_only()
    async def stop(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Stop Failed", "This command can only be used in a server.", ephemeral=True)
            return

        state = self.get_state(guild.id)
        state.clear_queue()
        self.stop_playback_task(guild.id)

        if guild.voice_client is not None:
            guild.voice_client.stop()

        await self.bot.embeds.success_interaction(interaction, "Stopped", "Playback stopped and queue cleared.")

    @app_commands.command(name="leave", description="Disconnect from voice and clear the queue.")
    @app_commands.guild_only()
    async def leave(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        guild = interaction.guild
        if guild is None or guild.voice_client is None:
            await self.bot.embeds.info_interaction(interaction, "Leave", "I am not in a voice channel.")
            return

        state = self.get_state(guild.id)
        state.clear_queue()
        self.stop_playback_task(guild.id)
        self.reset_session_volume(guild.id)

        await guild.voice_client.disconnect(force=False)
        await self.bot.embeds.success_interaction(interaction, "Disconnected", "Left the voice channel and cleared the queue.")

    @app_commands.command(name="shuffle", description="Shuffle the queue.")
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        if not state.queue:
            await self.bot.embeds.info_interaction(interaction, "Shuffle", "The queue is empty.")
            return

        items = list(state.queue)
        random.shuffle(items)
        state.queue = deque(items)

        await self.bot.embeds.success_interaction(interaction, "Shuffled", "The queue has been shuffled.")

    @app_commands.command(name="volume", description="Set playback volume from 0 to 100.")
    @app_commands.describe(percent="Volume percentage")
    @app_commands.guild_only()
    async def volume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
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
            f"Volume set to `{percent}%` for this voice session. It will reset the next time I join.",
        )

    @app_commands.command(name="musicdj", description="Enable or disable DJ mode.")
    @app_commands.describe(enabled="Whether DJ mode should be enabled")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def musicdj(self, interaction: discord.Interaction, enabled: bool) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Settings Error", "This command can only be used in a server.", ephemeral=True)
            return

        update_guild_settings(
            self.bot.storage_path,
            interaction.guild.id,
            {"music_dj_enabled": enabled},
        )

        await self.bot.embeds.success_interaction(
            interaction,
            "DJ Mode Updated",
            f"DJ mode is now {'enabled' if enabled else 'disabled'}.",
            ephemeral=True,
        )

    @app_commands.command(name="musicdjrole", description="Set the role name used for DJ mode.")
    @app_commands.describe(role_name="Exact server role name")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def musicdjrole(self, interaction: discord.Interaction, role_name: str) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Settings Error", "This command can only be used in a server.", ephemeral=True)
            return

        update_guild_settings(
            self.bot.storage_path,
            interaction.guild.id,
            {"music_dj_role_name": role_name},
        )

        await self.bot.embeds.success_interaction(
            interaction,
            "DJ Role Updated",
            f"Music DJ role name set to `{role_name}`.",
            ephemeral=True,
        )

    @app_commands.command(name="musicdefaultvolume", description="Set the default volume used each time the bot joins voice.")
    @app_commands.describe(percent="Default volume percentage")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.guild_only()
    async def musicdefaultvolume(self, interaction: discord.Interaction, percent: app_commands.Range[int, 0, 100]) -> None:
        if interaction.guild is None:
            await self.bot.embeds.error_interaction(interaction, "Settings Error", "This command can only be used in a server.", ephemeral=True)
            return

        update_guild_settings(
            self.bot.storage_path,
            interaction.guild.id,
            {"music_default_volume": int(percent)},
        )

        await self.bot.embeds.success_interaction(
            interaction,
            "Default Volume Updated",
            f"Default music volume is now `{percent}%` whenever I freshly join a voice channel.",
            ephemeral=True,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
