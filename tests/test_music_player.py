"""Player-loop resilience tests for the music cog.

These reproduce failure modes seen in the production journal:

* The "Now Playing" embed send raising Forbidden used to kill the player task
  after ffmpeg had already started, leaving an orphaned player. Every later
  track then failed with "Already playing audio" and the queue drained.
* A signed stream URL that goes 403 makes ffmpeg exit with code 8 almost
  immediately. discord.py reports no error, so the track "finished" and the
  user heard silence.
"""

from __future__ import annotations

import asyncio
from collections import deque
from types import SimpleNamespace

import discord
import pytest

from cogs.music import GuildMusicState, Music, Track

GUILD_ID = 1
CHANNEL_ID = 2


def _forbidden() -> discord.Forbidden:
    response = SimpleNamespace(status=403, reason="Forbidden")
    return discord.Forbidden(response, {"message": "Missing Access", "code": 50001})


class FakeChannel(discord.abc.Messageable):
    """Enough of Messageable for the loop's isinstance check."""

    async def _get_channel(self):  # pragma: no cover - never used
        raise NotImplementedError


class FakeEmbeds:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.errors: list[str] = []
        self.fail_send_with: Exception | None = None

    def field(self, name: str, value: str, inline: bool = False):
        return (name, value, inline)

    async def send(self, target, *, title: str, **_):
        if self.fail_send_with is not None:
            raise self.fail_send_with
        self.sent.append(title)

    async def error(self, target, title: str, description: str, **_):
        self.errors.append(description)

    async def info(self, target, title: str, description: str, **_):
        pass


class FakeSource:
    """Stands in for PCMVolumeTransformer(FFmpegPCMAudio(...))."""

    def __init__(self, returncode: int | None) -> None:
        self.original = SimpleNamespace(_process=SimpleNamespace(poll=lambda: returncode))
        self.cleaned_up = False

    def cleanup(self) -> None:
        self.cleaned_up = True


class FakeVoiceClient:
    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.orphaned = False
        self.stop_calls = 0
        self.played: list[FakeSource] = []

    def is_connected(self) -> bool:
        return True

    def play(self, source, *, after) -> None:
        if self.orphaned:
            raise discord.ClientException("Already playing audio.")
        self.played.append(source)
        # Simulate the player thread finishing the track a moment later.
        self.loop.call_later(0.001, after, None)

    def stop(self) -> None:
        self.orphaned = False
        self.stop_calls += 1


def _track(name: str = "song") -> Track:
    return Track(title=name, webpage_url=f"https://youtube.com/watch?v={name}", stream_url="https://cdn/x", stream_url_fetched_at=1.0)


class Harness:
    def __init__(self, loop: asyncio.AbstractEventLoop, returncodes: list[int | None]) -> None:
        self.embeds = FakeEmbeds()
        self.voice = FakeVoiceClient(loop)
        guild = SimpleNamespace(id=GUILD_ID, voice_client=self.voice)
        channel = FakeChannel()
        bot = SimpleNamespace(
            loop=loop,
            embeds=self.embeds,
            get_guild=lambda gid: guild if gid == GUILD_ID else None,
            get_channel=lambda cid: channel if cid == CHANNEL_ID else None,
        )
        self.cog = Music.__new__(Music)
        self.cog.bot = bot
        self.cog.guild_states = {GUILD_ID: GuildMusicState(guild_id=GUILD_ID, text_channel_id=CHANNEL_ID)}
        self.state = self.cog.guild_states[GUILD_ID]
        self.returncodes = deque(returncodes)
        self.create_source_calls: list[str | None] = []

        self.start_offsets: list[float] = []
        self.autoplay = False
        self.related: list[Track] = []

        async def _create_source(track: Track, volume: float, *, start_at: float = 0.0) -> FakeSource:
            self.create_source_calls.append(track.stream_url)
            self.start_offsets.append(start_at)
            return FakeSource(self.returncodes.popleft() if self.returncodes else 0)

        async def _related(track: Track, exclude: set[str], guild_id=None) -> Track | None:
            self.related_excludes = set(exclude)
            return self.related.pop(0) if self.related else None

        self.voice.channel = SimpleNamespace(members=[SimpleNamespace(bot=False)])
        self.cog.get_music_settings = lambda gid: {"autoplay": self.autoplay}  # type: ignore[method-assign]
        self.cog.find_related_track = _related  # type: ignore[method-assign]

        async def _idle(_guild_id: int) -> None:
            pass

        self.cog.create_source = _create_source  # type: ignore[method-assign]
        self.cog.schedule_idle_disconnect = _idle  # type: ignore[method-assign]


def _run(returncodes: list[int | None], tracks: list[Track], setup=None) -> Harness:
    async def go() -> Harness:
        h = Harness(asyncio.get_running_loop(), returncodes)
        h.state.queue.extend(tracks)
        if setup:
            setup(h)
        await asyncio.wait_for(h.cog.player_loop(GUILD_ID), timeout=2)
        return h

    return asyncio.run(go())


def test_announce_failure_does_not_kill_playback() -> None:
    def setup(h: Harness) -> None:
        h.embeds.fail_send_with = _forbidden()

    h = _run([0, 0], [_track("a"), _track("b")])
    h2 = _run([0, 0], [_track("a"), _track("b")], setup)

    # Without the Forbidden both tracks play and are announced.
    assert len(h.voice.played) == 2 and h.embeds.sent == ["Now Playing", "Now Playing"]
    # With it, both still play; only the announcement is lost.
    assert len(h2.voice.played) == 2 and h2.embeds.sent == []
    assert h2.state.playback_task is None


def test_orphaned_player_is_stopped_and_track_retried() -> None:
    def setup(h: Harness) -> None:
        h.voice.orphaned = True

    h = _run([0], [_track("a")], setup)

    assert h.voice.stop_calls == 1
    assert len(h.voice.played) == 1
    assert h.state.queue == deque()


def test_fast_ffmpeg_failure_refreshes_url_and_retries_once() -> None:
    # ffmpeg exits 8 (HTTP 403) on the first attempt, then streams fine.
    h = _run([8, 0], [_track("a")])

    assert h.create_source_calls == ["https://cdn/x", None], "second attempt must force a fresh extraction"
    assert len(h.voice.played) == 2
    assert h.embeds.errors == []


def test_repeated_ffmpeg_failure_reports_and_skips() -> None:
    h = _run([8, 8, 0], [_track("a"), _track("b")])

    assert len(h.create_source_calls) == 3  # a, a (retry), b
    assert len(h.embeds.errors) == 1 and "`a`" in h.embeds.errors[0]
    assert h.state.queue == deque()


def test_clean_short_clip_is_not_treated_as_failure() -> None:
    # ffmpeg exit 0 within the fast-fail window = a genuinely short clip.
    h = _run([0], [_track("a")])

    assert h.create_source_calls == ["https://cdn/x"]
    assert h.embeds.errors == []


def test_after_callback_error_counts_as_failure() -> None:
    def setup(h: Harness) -> None:
        original_play = h.voice.play

        def play(source, *, after):
            if not h.voice.played:
                h.voice.played.append(source)
                h.voice.loop.call_later(0.001, after, RuntimeError("boom"))
                return
            original_play(source, after=after)

        h.voice.play = play  # type: ignore[method-assign]

    h = _run([None, None], [_track("a")], setup)

    assert len(h.voice.played) == 2
    assert h.create_source_calls == ["https://cdn/x", None]


def test_playback_failed_heuristics() -> None:
    assert Music._playback_failed(FakeSource(8), started_at=0.0, after_error=[]) is True
    assert Music._playback_failed(FakeSource(0), started_at=0.0, after_error=[]) is False
    assert Music._playback_failed(FakeSource(0), started_at=0.0, after_error=[RuntimeError()]) is True
    # Unknown exit code: fall back to elapsed time.
    import time

    now = time.monotonic()
    assert Music._playback_failed(FakeSource(None), started_at=now, after_error=[]) is True
    assert Music._playback_failed(FakeSource(None), started_at=now - 60, after_error=[]) is False
    # Killed by discord.py's cleanup (SIGKILL) is not a stream failure.
    assert Music._playback_failed(FakeSource(-9), started_at=now - 60, after_error=[]) is False


def test_queue_command_passes_missing_not_none_for_empty_view() -> None:
    """discord.py calls view.is_finished() on anything that isn't MISSING."""

    async def go() -> dict:
        cog = Music.__new__(Music)
        cog.bot = SimpleNamespace(embeds=__import__("utils.embeds", fromlist=["EmbedManager"]).EmbedManager(None))
        cog.guild_states = {GUILD_ID: GuildMusicState(guild_id=GUILD_ID)}

        async def allowed(_interaction):
            return True, None

        cog.ensure_music_allowed = allowed  # type: ignore[method-assign]

        captured: dict = {}

        async def send_message(**kwargs):
            captured.update(kwargs)

        interaction = SimpleNamespace(
            user=SimpleNamespace(id=5),
            guild_id=GUILD_ID,
            response=SimpleNamespace(send_message=send_message),
        )
        await Music.queue.callback(cog, interaction)
        return captured

    captured = asyncio.run(go())
    assert captured["view"] is discord.utils.MISSING


def test_seek_replays_the_same_track_from_the_new_position_without_reannouncing() -> None:
    def setup(h: Harness) -> None:
        original_play = h.voice.play

        def play(source, *, after) -> None:
            if not h.voice.played:
                h.state.seek_to = 42.0  # a seek lands while the first play is running
            original_play(source, after=after)

        h.voice.play = play  # type: ignore[method-assign]

    h = _run([None, 0, 0], [_track("a"), _track("b")], setup)
    assert [round(o) for o in h.start_offsets] == [0, 42, 0]
    assert h.embeds.sent == ["Now Playing", "Now Playing"] and h.embeds.errors == []  # the seek is neither announced nor a failure


def test_seek_validates_the_position_and_stops_the_current_source() -> None:
    from cogs.music import parse_timestamp

    assert [parse_timestamp(v) for v in ("90", "1:30", "1:02:03", "1:75", "abc", "", "1:2:3:4")] == [90, 90, 3723, None, None, None, None]

    async def go() -> None:
        h = Harness(asyncio.get_running_loop(), [])
        h.voice.is_playing, h.voice.is_paused = (lambda: True), (lambda: False)
        guild = SimpleNamespace(id=GUILD_ID, voice_client=h.voice)
        with pytest.raises(RuntimeError, match="Nothing is playing"):
            h.cog.seek(guild, 5)
        h.state.current = Track(title="x", webpage_url="https://youtu.be/x", duration=100)
        for bad in (-1, 100, 500):
            with pytest.raises(RuntimeError, match="between"):
                h.cog.seek(guild, bad)
        h.cog.seek(guild, 30)
        assert h.state.seek_to == 30.0 and h.voice.stop_calls == 1
        h.state.current = Track(title="live", webpage_url="https://youtu.be/y")
        with pytest.raises(RuntimeError, match="no known length"):
            h.cog.seek(guild, 5)

    asyncio.run(go())


def test_autoplay_continues_with_a_related_track_only_when_enabled() -> None:
    off = _run([0], [_track("a")], lambda h: h.related.append(_track("never")))
    assert len(off.voice.played) == 1

    def setup(h: Harness) -> None:
        h.autoplay = True
        h.related.extend([_track("b"), _track("c")])

    on = _run([0, 0, 0], [_track("a")], setup)
    assert len(on.voice.played) == 3  # a, then b and c from autoplay, then nothing related is left
    assert on.related_excludes == {"yt:a", "yt:b", "yt:c"}  # what already played is never offered again


def test_autoplay_stops_when_nobody_is_listening_and_after_a_stop() -> None:
    def empty_channel(h: Harness) -> None:
        h.autoplay = True
        h.related.append(_track("b"))
        h.voice.channel.members = [SimpleNamespace(bot=True)]

    assert len(_run([0], [_track("a")], empty_channel).voice.played) == 1

    state = GuildMusicState(guild_id=GUILD_ID)
    state.last_track = _track("a")
    state.clear_queue()
    assert state.last_track is None and state.seek_to is None


def test_playlist_entries_become_songs_and_unsafe_links_are_dropped() -> None:
    from cogs.music import flat_entry_to_song, track_key

    youtube = flat_entry_to_song({"id": "abc", "title": "T", "url": "https://www.youtube.com/watch?v=abc", "duration": 61.0, "channel": "C"})
    assert youtube == {"title": "T", "webpage_url": "https://www.youtube.com/watch?v=abc", "uploader": "C", "duration": 61, "thumbnail": None}
    soundcloud = flat_entry_to_song({"id": "1", "title": None, "url": "https://soundcloud.com/some-artist/why-not"})
    assert soundcloud["title"] == "Why Not" and soundcloud["uploader"] == "some-artist"
    assert flat_entry_to_song({"url": "https://evil.example/x", "title": "x"}) is None
    assert flat_entry_to_song({"url": "file:///etc/passwd"}) is None
    assert track_key("https://youtu.be/abc?t=3") == track_key("https://music.youtube.com/watch?v=abc") == "yt:abc"
