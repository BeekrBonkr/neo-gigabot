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

from utils import playlists as playlist_store
from utils.playlists import PlaylistError
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
    "socket_timeout": 10,
}

FFMPEG_BEFORE_OPTIONS = "-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"
FFMPEG_OPTIONS = "-vn"
IDLE_DISCONNECT_SECONDS = 180
MAX_QUEUE_SIZE = 100
MAX_TRACK_LENGTH_SECONDS = 60 * 60 * 3
EXTRACTION_TIMEOUT_SECONDS = 25
VOICE_CONNECT_TIMEOUT_SECONDS = 15
MAX_CONCURRENT_EXTRACTIONS = 4
SEARCH_RESULT_COUNT = 5
MAX_QUERY_LENGTH = 300

# Direct-URL playback is restricted to these hosts (and their subdomains). Anything
# else typed as an absolute URL is rejected outright rather than silently falling
# back to a search - yt-dlp's generic extractor will happily fetch *any* URL handed
# to it, which is an SSRF-ish abuse vector if a user points the bot at an internal
# or arbitrary host. Add hosts here deliberately, not by request.
ALLOWED_URL_HOSTS: tuple[str, ...] = (
    "youtube.com",
    "youtu.be",
    "music.youtube.com",
    "soundcloud.com",
    "bandcamp.com",
    "vimeo.com",
)


def is_probably_url(value: str) -> bool:
    parsed = urlparse(value)
    return bool(parsed.scheme and parsed.netloc)


def is_allowed_media_url(value: str) -> bool:
    host = (urlparse(value).hostname or "").lower()
    if not host:
        return False
    return any(host == allowed or host.endswith(f".{allowed}") for allowed in ALLOWED_URL_HOSTS)


async def own_playlist_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[str]]:
    """Autocomplete for a playlist-name option against the invoking user's own playlists."""
    bot = interaction.client
    playlists = playlist_store.list_playlists(bot.storage_path, interaction.user.id)
    matches = [p["display_name"] for p in playlists if current.lower() in p["display_name"].lower()]
    return [app_commands.Choice(name=name, value=name) for name in matches[:25]]


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

    def to_storage_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "webpage_url": self.webpage_url,
            "uploader": self.uploader,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
        }

    @classmethod
    def from_storage_dict(
        cls,
        data: dict[str, Any],
        *,
        requester: discord.abc.User | discord.Member | None = None,
    ) -> "Track":
        return cls(
            title=str(data.get("title") or "Unknown title"),
            webpage_url=str(data.get("webpage_url") or ""),
            uploader=data.get("uploader"),
            duration=data.get("duration"),
            thumbnail=data.get("thumbnail"),
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


class SearchResultsView(discord.ui.View):
    """Dropdown of search results, replacing the old bot's number-emoji reactions."""

    def __init__(self, *, cog: "Music", author_id: int, results: list[Track], timeout: float = 30.0) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.results = results
        self.message: discord.Message | None = None

        select = discord.ui.Select(
            placeholder="Pick a track to queue...",
            options=[
                discord.SelectOption(
                    label=track.title[:100],
                    description=(f"{track.uploader or 'Unknown uploader'} • {track.duration_label}")[:100],
                    value=str(index),
                )
                for index, track in enumerate(results)
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who searched can pick a result.", ephemeral=True
            )
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                index = int(item.values[0])
                break
        else:
            return

        track = self.results[index]
        self._disable()
        await interaction.response.edit_message(view=self)
        await self.cog.queue_track_and_report(interaction, track, followup=True)

    def _disable(self) -> None:
        for item in self.children:
            item.disabled = True

    async def on_timeout(self) -> None:
        self._disable()
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class QueueView(discord.ui.View):
    """Prev/next pagination for /queue. Dedicated commands handle playback control."""

    def __init__(self, *, cog: "Music", author_id: int, guild_id: int, per_page: int = 10, timeout: float = 120.0) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.guild_id = guild_id
        self.per_page = per_page
        self.page = 0
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Only the person who ran `/queue` can page through it.", ephemeral=True
            )
            return False
        return True

    def max_page(self) -> int:
        state = self.cog.get_state(self.guild_id)
        return max(0, (len(state.queue) - 1) // self.per_page)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        self.page = max(0, min(self.page, self.max_page()))
        embed = self.cog.build_queue_embed(self.guild_id, page=self.page, per_page=self.per_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = min(self.max_page(), self.page + 1)
        await self._refresh(interaction)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PlaylistView(discord.ui.View):
    """Browsing view for a single playlist: paging, queueing it, liking, and owner-only shuffle."""

    def __init__(
        self,
        *,
        cog: "Music",
        owner_id: int,
        name: str,
        viewer_id: int,
        per_page: int = 10,
        timeout: float = 120.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.owner_id = owner_id
        self.name = name
        self.viewer_id = viewer_id
        self.per_page = per_page
        self.page = 0
        self.message: discord.Message | None = None

        if owner_id == viewer_id:
            shuffle_button = discord.ui.Button(label="Shuffle", style=discord.ButtonStyle.secondary)
            shuffle_button.callback = self._on_shuffle
            self.add_item(shuffle_button)
        else:
            like_button = discord.ui.Button(label="Like / Unlike", style=discord.ButtonStyle.secondary, emoji="❤️")
            like_button.callback = self._on_like
            self.add_item(like_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message(
                "Only the person who opened this playlist can interact with it.", ephemeral=True
            )
            return False
        return True

    def _fetch(self) -> dict[str, Any]:
        return playlist_store.get_visible_playlist(
            self.cog.bot.storage_path, self.owner_id, self.name, viewer_id=self.viewer_id
        )

    def _max_page(self, song_count: int) -> int:
        return max(0, (song_count - 1) // self.per_page)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        try:
            playlist = self._fetch()
        except PlaylistError as exc:
            await interaction.response.edit_message(
                embed=self.cog.bot.embeds.error_embed("Playlist Unavailable", str(exc)), view=None
            )
            self.stop()
            return

        self.page = max(0, min(self.page, self._max_page(len(playlist["songs"]))))
        embed = self.cog.build_playlist_embed(playlist, page=self.page, per_page=self.per_page)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Previous", style=discord.ButtonStyle.secondary, row=0)
    async def previous(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page = max(0, self.page - 1)
        await self._refresh(interaction)

    @discord.ui.button(label="Next", style=discord.ButtonStyle.secondary, row=0)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.page += 1
        await self._refresh(interaction)

    @discord.ui.button(label="Queue This Playlist", style=discord.ButtonStyle.success, row=1)
    async def queue_playlist(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        try:
            playlist = self._fetch()
        except PlaylistError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        if not playlist["songs"]:
            await interaction.response.send_message("That playlist is empty.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        tracks = [Track.from_storage_dict(song, requester=interaction.user) for song in playlist["songs"]]
        try:
            added = await self.cog.connect_and_enqueue(interaction, tracks)
        except RuntimeError as exc:
            await interaction.followup.send(
                embed=self.cog.bot.embeds.error_embed("Could Not Queue Playlist", str(exc)), ephemeral=True
            )
            return

        await interaction.followup.send(
            embed=self.cog.bot.embeds.success_embed(
                "Playlist Queued", f"Added {added} of {len(tracks)} song(s) from `{playlist['display_name']}`."
            )
        )

    async def _on_shuffle(self, interaction: discord.Interaction) -> None:
        try:
            playlist_store.shuffle_playlist(self.cog.bot.storage_path, self.owner_id, self.name)
        except PlaylistError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        await self._refresh(interaction)

    async def _on_like(self, interaction: discord.Interaction) -> None:
        try:
            playlist, now_liked = playlist_store.toggle_like(
                self.cog.bot.storage_path, self.owner_id, self.name, interaction.user.id
            )
        except PlaylistError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        self.page = max(0, min(self.page, self._max_page(len(playlist["songs"]))))
        embed = self.cog.build_playlist_embed(playlist, page=self.page, per_page=self.per_page)
        await interaction.response.edit_message(embed=embed, view=self)
        await interaction.followup.send("Liked!" if now_liked else "Unliked.", ephemeral=True)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class Music(commands.GroupCog, group_name="music", group_description="Music playback, queue, and playlist commands."):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.guild_states: dict[int, GuildMusicState] = {}
        self._connect_locks: dict[int, asyncio.Lock] = {}
        self._extraction_semaphore = asyncio.Semaphore(MAX_CONCURRENT_EXTRACTIONS)
        self.playlist_group = PlaylistGroup(self)
        super().__init__()

    async def cog_load(self) -> None:
        self.app_command.add_command(self.playlist_group)

    async def cog_unload(self) -> None:
        self.app_command.remove_command(self.playlist_group.name)
        for state in self.guild_states.values():
            if state.playback_task and not state.playback_task.done():
                state.playback_task.cancel()
            if state.idle_disconnect_task and not state.idle_disconnect_task.done():
                state.idle_disconnect_task.cancel()

    # ---------- settings / state ----------

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

    def _get_connect_lock(self, guild_id: int) -> asyncio.Lock:
        lock = self._connect_locks.get(guild_id)
        if lock is None:
            lock = asyncio.Lock()
            self._connect_locks[guild_id] = lock
        return lock

    # ---------- access checks ----------

    async def ensure_bot_channel_allowed(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        """Lighter check for commands that don't touch live playback (playlists, savenow)."""
        if interaction.guild is None or interaction.channel is None:
            return False, "This command can only be used in a server."

        if not is_bot_channel(self.bot.storage_path, interaction.guild.id, interaction.channel.id):
            settings = get_guild_settings(self.bot.storage_path, interaction.guild.id)
            bot_channels = settings.get("bot_channels", []) or []
            if bot_channels:
                mentions = ", ".join(f"<#{channel_id}>" for channel_id in bot_channels)
                return False, f"This command can only be used in {mentions}."
            return False, "This command is not allowed in this channel."

        return True, None

    async def ensure_music_allowed(self, interaction: discord.Interaction) -> tuple[bool, str | None]:
        """Full check for commands that control or add to the live queue: bot-channel + DJ mode."""
        allowed, reason = await self.ensure_bot_channel_allowed(interaction)
        if not allowed:
            return allowed, reason

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

        async with self._get_connect_lock(interaction.guild.id):
            voice_client = interaction.guild.voice_client
            state = self.get_state(interaction.guild.id)

            if voice_client is None:
                self.reset_session_volume(interaction.guild.id)
                state.text_channel_id = interaction.channel_id
                try:
                    voice_client = await asyncio.wait_for(
                        target_channel.connect(self_deaf=True), timeout=VOICE_CONNECT_TIMEOUT_SECONDS
                    )
                except asyncio.TimeoutError as exc:
                    raise RuntimeError("Timed out connecting to that voice channel.") from exc
                except discord.ClientException as exc:
                    raise RuntimeError(str(exc)) from exc
                except discord.Forbidden as exc:
                    raise RuntimeError("I don't have permission to join that voice channel.") from exc

                if isinstance(target_channel, discord.StageChannel):
                    try:
                        await interaction.guild.me.edit(suppress=False)
                    except discord.HTTPException:
                        pass

                return voice_client

            if voice_client.channel == target_channel:
                state.text_channel_id = interaction.channel_id
                return voice_client

            if voice_client.is_playing() or voice_client.is_paused():
                raise RuntimeError("I am already playing music in another voice channel.")

            self.reset_session_volume(interaction.guild.id)
            state.text_channel_id = interaction.channel_id
            try:
                await asyncio.wait_for(voice_client.move_to(target_channel), timeout=VOICE_CONNECT_TIMEOUT_SECONDS)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("Timed out moving to that voice channel.") from exc
            return voice_client

    # ---------- extraction ----------

    async def extract_track(
        self,
        query: str,
        *,
        requester: discord.abc.User | discord.Member,
    ) -> Track:
        query = (query or "").strip()
        if not query:
            raise RuntimeError("Give me a song name, video title, or link.")
        if len(query) > MAX_QUERY_LENGTH:
            raise RuntimeError("That query is too long.")

        if is_probably_url(query) and not is_allowed_media_url(query):
            raise RuntimeError(
                "I can only play direct links from YouTube, SoundCloud, Bandcamp, or Vimeo. "
                "Try searching by name instead."
            )

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

        info = await self._run_extraction(loop, _extract)
        track = Track.from_extracted_info(info, requester=requester)

        if track.duration and track.duration > MAX_TRACK_LENGTH_SECONDS:
            raise RuntimeError("That track is longer than 3 hours.")

        if not track.webpage_url:
            raise RuntimeError("I could not resolve a playable track.")

        return track

    async def search_tracks(self, query: str, *, count: int = SEARCH_RESULT_COUNT) -> list[Track]:
        query = (query or "").strip()
        if not query:
            raise RuntimeError("Give me something to search for.")
        if len(query) > MAX_QUERY_LENGTH:
            raise RuntimeError("That query is too long.")
        if is_probably_url(query):
            raise RuntimeError("Use `/play` for direct links. `/search` is for text queries.")

        loop = asyncio.get_running_loop()

        def _search() -> list[dict[str, Any]]:
            with yt_dlp.YoutubeDL(YTDL_OPTIONS) as ydl:
                data = ydl.extract_info(f"ytsearch{count}:{query}", download=False)
            entries = [entry for entry in (data or {}).get("entries", []) if entry]
            return entries

        entries = await self._run_extraction(loop, _search)
        if not entries:
            raise RuntimeError("No results found.")

        return [Track.from_extracted_info(entry) for entry in entries]

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

        stream_url = await self._run_extraction(loop, _refresh)
        track.stream_url = stream_url
        return stream_url

    async def _run_extraction(self, loop: asyncio.AbstractEventLoop, func) -> Any:
        async with self._extraction_semaphore:
            try:
                return await asyncio.wait_for(
                    loop.run_in_executor(None, func), timeout=EXTRACTION_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError as exc:
                raise RuntimeError("That took too long to look up. Try again in a moment.") from exc
            except yt_dlp.utils.DownloadError as exc:
                raise RuntimeError("I couldn't fetch that. Double check the link or try a different search.") from exc

    async def create_source(self, track: Track, volume: float) -> discord.PCMVolumeTransformer:
        stream_url = track.stream_url or await self.refresh_stream_url(track)
        audio = discord.FFmpegPCMAudio(
            stream_url,
            before_options=FFMPEG_BEFORE_OPTIONS,
            options=FFMPEG_OPTIONS,
        )
        return discord.PCMVolumeTransformer(audio, volume=volume)

    # ---------- queue management ----------

    async def enqueue_tracks(self, guild_id: int, tracks: list[Track]) -> tuple[GuildMusicState, int]:
        state = self.get_state(guild_id)
        added = 0
        for track in tracks:
            if len(state.queue) >= MAX_QUEUE_SIZE:
                break
            state.queue.append(track)
            added += 1
        return state, added

    async def connect_and_enqueue(self, interaction: discord.Interaction, tracks: list[Track]) -> int:
        """Connect to the requester's voice channel and enqueue already-known tracks
        (no extraction needed - used for playlist playback)."""
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            raise RuntimeError(reason or "Not allowed.")

        channel = await self.ensure_user_voice_channel(interaction)
        if channel is None:
            raise RuntimeError("You need to join a voice channel first.")

        await self.ensure_voice_client(interaction, channel)
        state, added = await self.enqueue_tracks(interaction.guild_id, tracks)
        state.text_channel_id = interaction.channel_id

        guild = interaction.guild
        if guild is not None:
            await self.start_playback_if_needed(guild)

        return added

    async def resolve_and_queue(
        self,
        interaction: discord.Interaction,
        query: str,
        *,
        requester: discord.abc.User | discord.Member,
    ) -> tuple[Track, bool]:
        """Full /play pipeline: gate checks, connect, extract, enqueue.

        Returns (track, started_now).
        """
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            raise RuntimeError(reason or "Not allowed.")

        channel = await self.ensure_user_voice_channel(interaction)
        if channel is None:
            raise RuntimeError("You need to join a voice channel first.")

        await self.ensure_voice_client(interaction, channel)
        track = await self.extract_track(query, requester=requester)

        state, added = await self.enqueue_tracks(interaction.guild_id, [track])
        if added == 0:
            raise RuntimeError(f"The queue is full. Maximum size is {MAX_QUEUE_SIZE}.")
        state.text_channel_id = interaction.channel_id

        started_now = state.current is None and len(state.queue) == 1

        guild = interaction.guild
        if guild is not None:
            await self.start_playback_if_needed(guild)

        return track, started_now

    async def queue_track_and_report(
        self,
        interaction: discord.Interaction,
        track: Track,
        *,
        followup: bool = False,
    ) -> None:
        """Used by the search-results dropdown: queue an already-resolved track and report back."""
        try:
            allowed, reason = await self.ensure_music_allowed(interaction)
            if not allowed:
                raise RuntimeError(reason or "Not allowed.")

            channel = await self.ensure_user_voice_channel(interaction)
            if channel is None:
                raise RuntimeError("You need to join a voice channel first.")

            await self.ensure_voice_client(interaction, channel)
            track.requested_by_id = interaction.user.id
            track.requested_by_name = str(interaction.user)
            state, added = await self.enqueue_tracks(interaction.guild_id, [track])
            if added == 0:
                raise RuntimeError(f"The queue is full. Maximum size is {MAX_QUEUE_SIZE}.")
            state.text_channel_id = interaction.channel_id

            started_now = state.current is None and len(state.queue) == 1

            guild = interaction.guild
            if guild is not None:
                await self.start_playback_if_needed(guild)

            embed = self.bot.embeds.success_embed(
                "Now Playing" if started_now else "Queued",
                f"[{track.title}]({track.webpage_url})",
            )
        except RuntimeError as exc:
            embed = self.bot.embeds.error_embed("Could Not Queue Track", str(exc))

        send = interaction.followup.send if followup else interaction.response.send_message
        await send(embed=embed)

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

                def _after(error: Exception | None, _finished: asyncio.Event = finished) -> None:
                    if error:
                        LOGGER.error("Voice playback after-callback error: %s", error)
                    self.bot.loop.call_soon_threadsafe(_finished.set)

                try:
                    voice_client.play(source, after=_after)
                except discord.ClientException as exc:
                    LOGGER.warning("Could not start playback for %s: %s", track.webpage_url, exc)
                    state.current = None
                    continue

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

                # If the voice client vanished (kicked/disconnected) while a track was
                # playing, stop the loop instead of looping forever trying to play more.
                if guild.voice_client is None or not guild.voice_client.is_connected():
                    state.clear_queue()
                    return

        except asyncio.CancelledError:
            raise
        finally:
            state.playback_task = None

    def build_queue_embed(self, guild_id: int, *, page: int = 0, per_page: int = 10) -> discord.Embed:
        state = self.get_state(guild_id)

        description = "Nothing is currently playing."
        if state.current:
            description = f"Now playing: [{state.current.title}]({state.current.webpage_url})"

        queue_list = list(state.queue)
        start = page * per_page
        chunk = queue_list[start : start + per_page]

        if queue_list:
            upcoming = "\n".join(
                f"{index}. [{track.title}]({track.webpage_url}) • `{track.duration_label}`"
                for index, track in enumerate(chunk, start=start + 1)
            )
        else:
            upcoming = "The queue is empty."

        max_page = max(0, (len(queue_list) - 1) // per_page)
        return self.bot.embeds.info_embed(
            "Music Queue",
            description,
            fields=[self.bot.embeds.field(f"Up Next ({len(queue_list)} total)", upcoming)],
            footer=f"Page {page + 1}/{max_page + 1} • Volume: {round(state.volume * 100)}%",
        )

    def build_playlist_embed(self, playlist: dict[str, Any], *, page: int = 0, per_page: int = 10) -> discord.Embed:
        songs = playlist["songs"]
        start = page * per_page
        chunk = songs[start : start + per_page]
        max_page = max(0, (len(songs) - 1) // per_page) if songs else 0

        if chunk:
            body = "\n".join(
                f"{index}. [{song['title']}]({song['webpage_url']}) • {song.get('uploader') or 'Unknown'}"
                for index, song in enumerate(chunk, start=start + 1)
            )
        else:
            body = "This playlist is empty."

        return self.bot.embeds.create(
            title=f"🎵 {playlist['display_name']} ({'Private' if playlist['status'] == 'private' else 'Public'})",
            description=playlist["description"] or "No description.",
            color=discord.Color.teal(),
            fields=[self.bot.embeds.field(f"Songs ({len(songs)})", body)],
            footer=f"❤️ {len(playlist['likes'])} like(s) • Page {page + 1}/{max_page + 1}",
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

    # ---------- slash commands ----------

    @app_commands.command(name="join", description="Join your current voice channel.")
    @app_commands.checks.cooldown(1, 5.0)
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
        except RuntimeError as exc:
            await self.bot.embeds.error_interaction(interaction, "Could Not Join", str(exc), ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        await self.bot.embeds.success_interaction(
            interaction,
            "Connected",
            f"Joined {channel.mention}. Volume reset to `{round(state.volume * 100)}%`.",
        )

    @app_commands.command(name="play", description="Play a track from a URL or search term.")
    @app_commands.describe(query="A song name, video title, or a YouTube/SoundCloud/Bandcamp/Vimeo link")
    @app_commands.checks.cooldown(2, 5.0)
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, query: str) -> None:
        await interaction.response.defer(thinking=True)

        try:
            track, started_now = await self.resolve_and_queue(interaction, query, requester=interaction.user)
        except RuntimeError as exc:
            await interaction.followup.send(
                embed=self.bot.embeds.error_embed("Could Not Queue Track", str(exc)), ephemeral=True
            )
            return

        description = (
            f"Queued [{track.title}]({track.webpage_url}). Playback will begin momentarily."
            if started_now
            else f"Added [{track.title}]({track.webpage_url}) to the queue."
        )
        await interaction.followup.send(
            embed=self.bot.embeds.create(
                title="Queued",
                description=description,
                color=discord.Color.blurple(),
                fields=[
                    self.bot.embeds.field("Duration", track.duration_label, True),
                    self.bot.embeds.field("Uploader", track.uploader or "Unknown", True),
                ],
                thumbnail_url=track.thumbnail,
            )
        )

    @app_commands.command(name="search", description="Search for a track and pick one to queue.")
    @app_commands.describe(query="What to search for")
    @app_commands.checks.cooldown(1, 15.0)
    @app_commands.guild_only()
    async def search(self, interaction: discord.Interaction, query: str) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True)
        try:
            results = await self.search_tracks(query)
        except RuntimeError as exc:
            await interaction.followup.send(embed=self.bot.embeds.error_embed("Search Failed", str(exc)), ephemeral=True)
            return

        embed = self.bot.embeds.info_embed(
            f"Search Results for '{query}'",
            "Pick a result from the dropdown below within 30 seconds.",
        )
        view = SearchResultsView(cog=self, author_id=interaction.user.id, results=results)
        await interaction.followup.send(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="queue", description="Show the current queue.")
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def queue(self, interaction: discord.Interaction) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        view = QueueView(cog=self, author_id=interaction.user.id, guild_id=interaction.guild_id)
        embed = self.build_queue_embed(interaction.guild_id)
        if view.max_page() == 0:
            view.clear_items()
        await interaction.response.send_message(embed=embed, view=view if view.children else None)
        if view.children:
            view.message = await interaction.original_response()

    @app_commands.command(name="remove", description="Remove a track from the queue by its position.")
    @app_commands.describe(position="Position shown in /queue, starting at 1")
    @app_commands.checks.cooldown(1, 5.0)
    @app_commands.guild_only()
    async def remove(self, interaction: discord.Interaction, position: app_commands.Range[int, 1, MAX_QUEUE_SIZE]) -> None:
        allowed, reason = await self.ensure_music_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Music Unavailable", reason or "Not allowed.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        if position > len(state.queue):
            await self.bot.embeds.error_interaction(
                interaction, "Invalid Position", f"The queue only has {len(state.queue)} track(s).", ephemeral=True
            )
            return

        queue_list = list(state.queue)
        removed = queue_list.pop(position - 1)
        state.queue = deque(queue_list)

        await self.bot.embeds.success_interaction(interaction, "Removed", f"Removed `{removed.title}` from the queue.")

    @app_commands.command(name="nowplaying", description="Show the current track.")
    @app_commands.checks.cooldown(1, 10.0)
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
    @app_commands.checks.cooldown(1, 5.0)
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
    @app_commands.checks.cooldown(1, 5.0)
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
    @app_commands.checks.cooldown(1, 5.0)
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
    @app_commands.checks.cooldown(1, 5.0)
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

    @app_commands.command(name="resetq", description="Force-clear the queue and disconnect. For when things get stuck.")
    @app_commands.checks.has_permissions(manage_guild=True)
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def resetq(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Reset Failed", "This command can only be used in a server.", ephemeral=True)
            return

        state = self.get_state(guild.id)
        state.clear_queue()
        self.stop_playback_task(guild.id)
        self.reset_session_volume(guild.id)

        if guild.voice_client is not None:
            try:
                await guild.voice_client.disconnect(force=True)
            except Exception:
                LOGGER.exception("Failed to force-disconnect during /resetq in guild %s", guild.id)

        await self.bot.embeds.success_interaction(interaction, "Reset", "Music queue cleared and voice connection reset.")

    @app_commands.command(name="leave", description="Disconnect from voice and clear the queue.")
    @app_commands.checks.cooldown(1, 5.0)
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
    @app_commands.checks.cooldown(1, 10.0)
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
    @app_commands.checks.cooldown(1, 10.0)
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

    @app_commands.command(name="savenow", description="Save the currently playing track to one of your playlists.")
    @app_commands.describe(playlist="Which of your playlists to save it to")
    @app_commands.autocomplete(playlist=own_playlist_autocomplete)
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def savenow(self, interaction: discord.Interaction, playlist: str) -> None:
        allowed, reason = await self.ensure_bot_channel_allowed(interaction)
        if not allowed:
            await self.bot.embeds.error_interaction(interaction, "Not Allowed", reason or "Not allowed.", ephemeral=True)
            return

        state = self.get_state(interaction.guild_id)
        if state.current is None:
            await self.bot.embeds.error_interaction(interaction, "Nothing Playing", "There's no song playing right now.", ephemeral=True)
            return

        try:
            updated, added = playlist_store.add_songs(
                self.bot.storage_path,
                interaction.user.id,
                playlist,
                [state.current.to_storage_dict()],
            )
        except PlaylistError as exc:
            await self.bot.embeds.error_interaction(interaction, "Could Not Save", str(exc), ephemeral=True)
            return

        if added == 0:
            await self.bot.embeds.error_interaction(interaction, "Could Not Save", "That playlist is full.", ephemeral=True)
            return

        await self.bot.embeds.success_interaction(
            interaction,
            "Saved",
            f"Saved `{state.current.title}` to `{updated['display_name']}`.",
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

    @join.error
    @play.error
    @search.error
    @queue.error
    @remove.error
    @nowplaying.error
    @skip.error
    @pause.error
    @resume.error
    @stop.error
    @resetq.error
    @leave.error
    @shuffle.error
    @volume.error
    @savenow.error
    @musicdj.error
    @musicdjrole.error
    @musicdefaultvolume.error
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self.bot.embeds.error_interaction(
                interaction,
                "Slow Down",
                f"Try again in `{error.retry_after:.1f}` seconds.",
                ephemeral=True,
            )
            return

        if isinstance(error, app_commands.MissingPermissions):
            missing = ", ".join(error.missing_permissions)
            await self.bot.embeds.error_interaction(
                interaction, "Missing Permissions", f"You are missing: `{missing}`.", ephemeral=True
            )
            return

        if isinstance(error, app_commands.TransformerError):
            await self.bot.embeds.error_interaction(
                interaction, "Invalid Value", "One of the values you entered was invalid.", ephemeral=True
            )
            return

        raise error


class PlaylistPickerView(discord.ui.View):
    """Select menu used by /playlist profile and /playlist liked to open one playlist."""

    def __init__(
        self,
        *,
        cog: "Music",
        viewer_id: int,
        entries: list[tuple[int, dict[str, Any]]],
        timeout: float = 60.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.viewer_id = viewer_id
        self.entries = entries
        self.message: discord.Message | None = None

        select = discord.ui.Select(
            placeholder="Open a playlist...",
            options=[
                discord.SelectOption(
                    label=playlist["display_name"][:100],
                    description=f"{len(playlist['songs'])} song(s)"[:100],
                    value=str(index),
                )
                for index, (_, playlist) in enumerate(entries)
            ],
        )
        select.callback = self._on_select
        self.add_item(select)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.viewer_id:
            await interaction.response.send_message("Only the person who ran this command can pick one.", ephemeral=True)
            return False
        return True

    async def _on_select(self, interaction: discord.Interaction) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Select):
                index = int(item.values[0])
                break
        else:
            return

        owner_id, playlist = self.entries[index]
        try:
            fresh = playlist_store.get_visible_playlist(
                self.cog.bot.storage_path, owner_id, playlist["name"], viewer_id=self.viewer_id
            )
        except PlaylistError as exc:
            await interaction.response.edit_message(
                embed=self.cog.bot.embeds.error_embed("Playlist Unavailable", str(exc)), view=None
            )
            return

        view = PlaylistView(cog=self.cog, owner_id=owner_id, name=fresh["name"], viewer_id=self.viewer_id)
        embed = self.cog.build_playlist_embed(fresh, page=0, per_page=view.per_page)
        await interaction.response.edit_message(embed=embed, view=view)
        view.message = await interaction.original_response()
        self.stop()

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


class PlaylistGroup(app_commands.Group):
    """`/playlist ...` - personal song playlists, backed by utils/playlists.py (SQLite)."""

    def __init__(self, cog: Music) -> None:
        super().__init__(name="playlist", description="Create and manage your personal song playlists.")
        self.cog = cog

    async def _ensure_allowed(self, interaction: discord.Interaction) -> bool:
        allowed, reason = await self.cog.ensure_bot_channel_allowed(interaction)
        if not allowed:
            await self.cog.bot.embeds.error_interaction(interaction, "Not Allowed", reason or "Not allowed.", ephemeral=True)
        return allowed

    @app_commands.command(name="create", description="Create a new playlist.")
    @app_commands.describe(name="Letters, numbers, underscores, and hyphens only (max 20 chars)")
    @app_commands.checks.cooldown(2, 20.0)
    @app_commands.guild_only()
    async def create(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            playlist = playlist_store.create_playlist(self.cog.bot.storage_path, interaction.user.id, name)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Create Playlist", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Playlist Created", f"Created `{playlist['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="delete", description="Delete one of your playlists.")
    @app_commands.describe(name="Playlist to delete")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(2, 20.0)
    @app_commands.guild_only()
    async def delete(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            playlist = playlist_store.delete_playlist(self.cog.bot.storage_path, interaction.user.id, name)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Delete Playlist", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Playlist Deleted", f"Deleted `{playlist['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="rename", description="Rename one of your playlists.")
    @app_commands.describe(name="Current playlist name", new_name="New name (letters, numbers, _ and - only)")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(2, 20.0)
    @app_commands.guild_only()
    async def rename(self, interaction: discord.Interaction, name: str, new_name: str) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            playlist = playlist_store.rename_playlist(self.cog.bot.storage_path, interaction.user.id, name, new_name)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Rename Playlist", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Playlist Renamed", f"Renamed to `{playlist['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="add", description="Add up to 10 songs to a playlist (comma-separated searches or links).")
    @app_commands.describe(name="Playlist to add to", songs="Comma-separated song names or links, e.g. 'song a, song b'")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(1, 30.0)
    @app_commands.guild_only()
    async def add(self, interaction: discord.Interaction, name: str, songs: str) -> None:
        if not await self._ensure_allowed(interaction):
            return

        queries = [q.strip() for q in songs.split(",") if q.strip()]
        if not queries:
            await self.cog.bot.embeds.error_interaction(interaction, "Nothing To Add", "Give me at least one song.", ephemeral=True)
            return
        if len(queries) > playlist_store.MAX_SONGS_ADDED_PER_CALL:
            await self.cog.bot.embeds.error_interaction(
                interaction,
                "Too Many Songs",
                f"You can only add {playlist_store.MAX_SONGS_ADDED_PER_CALL} songs at a time.",
                ephemeral=True,
            )
            return

        # Fail fast before doing any network lookups if the playlist doesn't exist / is full.
        existing = playlist_store.get_playlist(self.cog.bot.storage_path, interaction.user.id, name)
        if existing is None:
            await self.cog.bot.embeds.error_interaction(interaction, "No Such Playlist", f"You don't have a playlist named `{name}`.", ephemeral=True)
            return

        await interaction.response.defer(thinking=True, ephemeral=True)

        tracks: list[Track] = []
        failures: list[str] = []
        for query in queries:
            try:
                track = await self.cog.extract_track(query, requester=interaction.user)
                tracks.append(track)
            except RuntimeError as exc:
                failures.append(f"`{query}`: {exc}")

        added_count = 0
        if tracks:
            try:
                updated, added_count = playlist_store.add_songs(
                    self.cog.bot.storage_path,
                    interaction.user.id,
                    name,
                    [track.to_storage_dict() for track in tracks],
                )
            except PlaylistError as exc:
                await interaction.followup.send(embed=self.cog.bot.embeds.error_embed("Could Not Add Songs", str(exc)))
                return
            display_name = updated["display_name"]
        else:
            display_name = existing["display_name"]

        summary_lines = [f"Added {added_count} of {len(queries)} requested song(s) to `{display_name}`."]
        if failures:
            summary_lines.append("Failures:\n" + "\n".join(failures[:10]))

        await interaction.followup.send(embed=self.cog.bot.embeds.info_embed("Add Songs Result", "\n\n".join(summary_lines)))

    @app_commands.command(name="removesong", description="Remove a song from a playlist by its position.")
    @app_commands.describe(name="Playlist name", position="Position shown in /playlist view, starting at 1")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(2, 10.0)
    @app_commands.guild_only()
    async def removesong(
        self,
        interaction: discord.Interaction,
        name: str,
        position: app_commands.Range[int, 1, playlist_store.MAX_SONGS_PER_PLAYLIST],
    ) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            updated, removed = playlist_store.remove_song(self.cog.bot.storage_path, interaction.user.id, name, position)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Remove Song", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Song Removed", f"Removed `{removed['title']}` from `{updated['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="shuffle", description="Shuffle the song order in one of your playlists.")
    @app_commands.describe(name="Playlist name")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(2, 10.0)
    @app_commands.guild_only()
    async def shuffle(self, interaction: discord.Interaction, name: str) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            updated = playlist_store.shuffle_playlist(self.cog.bot.storage_path, interaction.user.id, name)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Shuffle", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Shuffled", f"Shuffled `{updated['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="description", description="Set a playlist's description.")
    @app_commands.describe(name="Playlist name", description="New description (max 200 characters)")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(2, 10.0)
    @app_commands.guild_only()
    async def set_description(self, interaction: discord.Interaction, name: str, description: str) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            updated = playlist_store.set_description(self.cog.bot.storage_path, interaction.user.id, name, description)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Update Description", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Description Updated", f"Updated the description for `{updated['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="status", description="Make a playlist public or private.")
    @app_commands.describe(name="Playlist name", status="public or private")
    @app_commands.choices(status=[
        app_commands.Choice(name="public", value="public"),
        app_commands.Choice(name="private", value="private"),
    ])
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(2, 10.0)
    @app_commands.guild_only()
    async def status(self, interaction: discord.Interaction, name: str, status: app_commands.Choice[str]) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            updated = playlist_store.set_status(self.cog.bot.storage_path, interaction.user.id, name, status.value)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Update Status", str(exc), ephemeral=True)
            return
        await self.cog.bot.embeds.success_interaction(
            interaction, "Status Updated", f"`{updated['display_name']}` is now {updated['status']}.", ephemeral=True
        )

    @app_commands.command(name="view", description="View a playlist (yours, or someone else's public one).")
    @app_commands.describe(name="Playlist name", user="Whose playlist to view (defaults to you)")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def view(self, interaction: discord.Interaction, name: str, user: discord.User | None = None) -> None:
        if not await self._ensure_allowed(interaction):
            return

        owner = user or interaction.user
        try:
            playlist = playlist_store.get_visible_playlist(
                self.cog.bot.storage_path, owner.id, name, viewer_id=interaction.user.id
            )
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not View Playlist", str(exc), ephemeral=True)
            return

        view = PlaylistView(cog=self.cog, owner_id=owner.id, name=playlist["name"], viewer_id=interaction.user.id)
        embed = self.cog.build_playlist_embed(playlist, page=0, per_page=view.per_page)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="play", description="Queue a playlist (yours, or someone else's public one).")
    @app_commands.describe(name="Playlist name", user="Whose playlist to queue (defaults to you)")
    @app_commands.autocomplete(name=own_playlist_autocomplete)
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def play(self, interaction: discord.Interaction, name: str, user: discord.User | None = None) -> None:
        owner = user or interaction.user
        try:
            playlist = playlist_store.get_visible_playlist(
                self.cog.bot.storage_path, owner.id, name, viewer_id=interaction.user.id
            )
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Queue Playlist", str(exc), ephemeral=True)
            return

        if not playlist["songs"]:
            await self.cog.bot.embeds.error_interaction(
                interaction, "Empty Playlist", f"`{playlist['display_name']}` has no songs.", ephemeral=True
            )
            return

        await interaction.response.defer(thinking=True)
        tracks = [Track.from_storage_dict(song, requester=interaction.user) for song in playlist["songs"]]
        try:
            added = await self.cog.connect_and_enqueue(interaction, tracks)
        except RuntimeError as exc:
            await interaction.followup.send(embed=self.cog.bot.embeds.error_embed("Could Not Queue Playlist", str(exc)), ephemeral=True)
            return

        await interaction.followup.send(
            embed=self.cog.bot.embeds.success_embed(
                "Playlist Queued", f"Added {added} of {len(tracks)} song(s) from `{playlist['display_name']}`."
            )
        )

    @app_commands.command(name="profile", description="List a user's public playlists.")
    @app_commands.describe(user="Whose playlists to list (defaults to you)")
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def profile(self, interaction: discord.Interaction, user: discord.User | None = None) -> None:
        if not await self._ensure_allowed(interaction):
            return

        target = user or interaction.user
        all_playlists = playlist_store.list_playlists(self.cog.bot.storage_path, target.id)
        visible = [p for p in all_playlists if p["status"] == "public" or target.id == interaction.user.id]

        if not visible:
            await self.cog.bot.embeds.info_interaction(interaction, "No Playlists", f"{target.mention} has no visible playlists.")
            return

        lines = []
        for playlist in visible:
            marker = "🔒" if playlist["status"] == "private" else "🌐"
            lines.append(f"{marker} **{playlist['display_name']}** — {len(playlist['songs'])} song(s), ❤️ {len(playlist['likes'])}")

        embed = self.cog.bot.embeds.info_embed(f"{target.display_name}'s Playlists", "\n".join(lines))
        entries = [(target.id, playlist) for playlist in visible]
        view = PlaylistPickerView(cog=self.cog, viewer_id=interaction.user.id, entries=entries)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    @app_commands.command(name="like", description="Like a public playlist.")
    @app_commands.describe(user="Playlist owner", name="Playlist name")
    @app_commands.checks.cooldown(1, 5.0)
    @app_commands.guild_only()
    async def like(self, interaction: discord.Interaction, user: discord.User, name: str) -> None:
        if not await self._ensure_allowed(interaction):
            return
        try:
            updated, now_liked = playlist_store.toggle_like(self.cog.bot.storage_path, user.id, name, interaction.user.id)
        except PlaylistError as exc:
            await self.cog.bot.embeds.error_interaction(interaction, "Could Not Like Playlist", str(exc), ephemeral=True)
            return

        verb = "Liked" if now_liked else "Unliked"
        await self.cog.bot.embeds.success_interaction(
            interaction, verb, f"{verb} {user.mention}'s playlist `{updated['display_name']}`.", ephemeral=True
        )

    @app_commands.command(name="unlike", description="Remove your like from a playlist.")
    @app_commands.describe(user="Playlist owner", name="Playlist name")
    @app_commands.checks.cooldown(1, 5.0)
    @app_commands.guild_only()
    async def unlike(self, interaction: discord.Interaction, user: discord.User, name: str) -> None:
        # `like` already toggles; keep a dedicated `unlike` for discoverability/parity with the old bot.
        await self.like.callback(self, interaction, user, name)

    @app_commands.command(name="liked", description="List playlists you've liked.")
    @app_commands.checks.cooldown(1, 10.0)
    @app_commands.guild_only()
    async def liked(self, interaction: discord.Interaction) -> None:
        if not await self._ensure_allowed(interaction):
            return

        liked_playlists = playlist_store.list_liked_playlists(self.cog.bot.storage_path, interaction.user.id)
        if not liked_playlists:
            await self.cog.bot.embeds.info_interaction(interaction, "No Liked Playlists", "You haven't liked any playlists yet.")
            return

        lines = [f"**{p['display_name']}** (owner <@{p['owner_id']}>) — {len(p['songs'])} song(s)" for p in liked_playlists]
        embed = self.cog.bot.embeds.info_embed("Playlists You've Liked", "\n".join(lines))
        entries = [(p["owner_id"], p) for p in liked_playlists]
        view = PlaylistPickerView(cog=self.cog, viewer_id=interaction.user.id, entries=entries)
        await interaction.response.send_message(embed=embed, view=view)
        view.message = await interaction.original_response()

    async def on_error(self, interaction: discord.Interaction, error: app_commands.AppCommandError) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self.cog.bot.embeds.error_interaction(
                interaction, "Slow Down", f"Try again in `{error.retry_after:.1f}` seconds.", ephemeral=True
            )
            return

        if isinstance(error, app_commands.TransformerError):
            await self.cog.bot.embeds.error_interaction(
                interaction, "Invalid Value", "One of the values you entered was invalid.", ephemeral=True
            )
            return

        LOGGER.exception("Unhandled /playlist error", exc_info=error)
        await self.cog.bot.embeds.error_interaction(
            interaction, "Command Failed", "Something went wrong while running that command.", ephemeral=True
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Music(bot))
