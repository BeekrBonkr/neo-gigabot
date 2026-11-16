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

        async def _create_source(track: Track, volume: float) -> FakeSource:
            self.create_source_calls.append(track.stream_url)
            return FakeSource(self.returncodes.popleft() if self.returncodes else 0)

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
