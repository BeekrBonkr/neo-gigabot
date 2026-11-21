from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer
from discord import app_commands

from cogs.panel_bridge import PanelBridge, SettingsValidationError, _stringify_ids, validate_settings
from utils.settings import DEFAULT_GUILD_SETTINGS, get_default_settings, get_guild_settings
from utils.warnings import add_warning, list_warnings

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _valid(**overrides):
    data = dict(DEFAULT_GUILD_SETTINGS)
    data.update(overrides)
    return data


def test_validate_normalizes_id_shapes():
    cleaned = validate_settings(
        _valid(bot_channels=["123", 123, "456"], logging_channel_id=789, mod_log_events=["ban", "warn"]),
        DEFAULT_GUILD_SETTINGS,
    )
    assert cleaned["bot_channels"] == [123, 456]
    assert cleaned["logging_channel_id"] == "789"
    assert cleaned["mod_log_events"] == ["warn", "ban"]


@pytest.mark.parametrize(
    "overrides",
    [
        {"nope": 1},
        {"music_default_volume": 101},
        {"music_default_volume": True},
        {"warn_action": "explode"},
        {"warn_mute_duration": "soon"},
        {"embed_color": "blue"},
        {"bot_channels": ["abc"]},
        {"logging_enabled": "yes"},
        {"blocked_commands": ["settings"]},
        {"starboard_threshold": 0},
        {"starboard_emoji": " "},
        {"join_message": "x" * 1001},
    ],
)
def test_validate_rejects_bad_values(overrides):
    with pytest.raises(SettingsValidationError):
        validate_settings(_valid(**overrides), DEFAULT_GUILD_SETTINGS)


def test_validate_rejects_non_dict():
    with pytest.raises(SettingsValidationError):
        validate_settings(None, DEFAULT_GUILD_SETTINGS)


def test_stringify_ids_only_touches_snowflakes():
    assert _stringify_ids({"a": 5, "b": 1108208007449423885, "c": [True]}) == {
        "a": 5,
        "b": "1108208007449423885",
        "c": [True],
    }


def _client(storage):
    bot = SimpleNamespace(storage_path=storage, guilds=[], voice_clients=[], get_guild=lambda _id: None)
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return TestClient(TestServer(cog.build_app()))


def test_requests_without_the_token_are_rejected(storage):
    async def run():
        async with _client(storage) as client:
            assert (await client.get("/v1/guilds")).status == 401
            assert (await client.get("/v1/guilds", headers={"Authorization": "Bearer wrong"})).status == 401
            assert (await client.get("/v1/guilds", headers=AUTH)).status == 200

    asyncio.run(run())


def test_settings_round_trip_updates_the_bot_cache(storage):
    async def run():
        async with _client(storage) as client:
            current = (await (await client.get("/v1/guilds/42/settings", headers=AUTH)).json())["settings"]
            current["music_default_volume"] = 80
            current["autoroles"] = ["1108208007449423885"]
            response = await client.put("/v1/guilds/42/settings", headers=AUTH, json={"settings": current})
            assert response.status == 200
            body = await response.json()
            assert body["before"]["music_default_volume"] == 50

            current["warn_action"] = "nonsense"
            bad = await client.put("/v1/guilds/42/settings", headers=AUTH, json={"settings": current})
            assert bad.status == 400

    asyncio.run(run())
    saved = get_guild_settings(storage, 42)
    assert saved["music_default_volume"] == 80
    assert saved["autoroles"] == [1108208007449423885]
    assert saved["warn_action"] == "none"


def test_defaults_put_rewrites_the_template(storage):
    async def run():
        async with _client(storage) as client:
            defaults = (await (await client.get("/v1/defaults", headers=AUTH)).json())["settings"]
            defaults["starboard_threshold"] = 7
            assert (await client.put("/v1/defaults", headers=AUTH, json={"settings": defaults})).status == 200

    asyncio.run(run())
    assert get_default_settings(storage)["starboard_threshold"] == 7
    assert get_guild_settings(storage, 99)["starboard_threshold"] == 7


def test_data_delete_removes_a_warning(storage):
    warning_id = add_warning(storage, 1, 2, 3, "spam")

    async def run():
        async with _client(storage) as client:
            ok = await client.post("/v1/data/warnings/delete", headers=AUTH, json={"guild_id": "1", "id": str(warning_id)})
            assert ok.status == 200
            assert (await ok.json())["before"]["reason"] == "spam"
            gone = await client.post("/v1/data/warnings/delete", headers=AUTH, json={"guild_id": "1", "id": str(warning_id)})
            assert gone.status == 404
            unknown = await client.post("/v1/data/nope/delete", headers=AUTH, json={})
            assert unknown.status == 404
            sticky = await client.post("/v1/data/sticky_roles/delete", headers=AUTH, json={"guild_id": "1", "user_id": "2"})
            assert sticky.status == 404

    asyncio.run(run())
    assert list_warnings(storage, 1, 2) == []


# ---------- per-user access and web music control ----------

class _Role:
    def __init__(self, role_id, name):
        self.id, self.name = role_id, name


class _VoiceClient:
    def __init__(self, channel):
        self.channel, self.source = channel, None
        self._playing, self._paused, self.stopped = True, False, 0

    def is_connected(self):
        return True

    def is_playing(self):
        return self._playing and not self._paused

    def is_paused(self):
        return self._paused

    def pause(self):
        self._paused = True

    def resume(self):
        self._paused = False

    def stop(self):
        self.stopped += 1


def _track(title):
    return SimpleNamespace(title=title, webpage_url=f"https://youtu.be/{title}", uploader="u", duration=100, thumbnail=None, requested_by_name="x")


def _music_world(storage, *, dj_mode=False, blocked=()):
    """A guild with the bot in a voice channel, a listener in it, a member
    outside it, an admin, and a stranger who isn't in the guild at all."""
    from collections import deque

    from utils.settings import update_guild_settings

    channel = SimpleNamespace(name="Lounge", members=[])
    other = SimpleNamespace(name="AFK", members=[])

    def member(uid, voice_channel, roles=(), admin=False):
        return SimpleNamespace(
            id=uid, bot=False, display_name=f"m{uid}", roles=list(roles),
            voice=SimpleNamespace(channel=voice_channel) if voice_channel else None,
            guild_permissions=SimpleNamespace(administrator=admin, manage_guild=admin),
        )

    members = {
        1: member(1, channel),
        2: member(2, other),
        3: member(3, channel, roles=[_Role(50, "dj")]),
        4: member(4, None, admin=True),
    }
    channel.members = [members[1], members[3]]
    voice = _VoiceClient(channel)
    state = SimpleNamespace(queue=deque([_track("a"), _track("b"), _track("c")]), current=_track("now"), volume=0.5,
                            loop_mode="off", skip_requested=False)
    state.clear_queue = lambda: (state.queue.clear(), setattr(state, "current", None))
    guild = SimpleNamespace(
        id=77, name="Guild", icon=None, member_count=4, owner_id=9, me=None, unavailable=False, created_at=None,
        voice_client=voice, get_member=members.get,
    )
    voice.guild = guild
    stopped = []
    music = SimpleNamespace(
        guild_states={77: state}, get_state=lambda gid: state, stop_playback_task=stopped.append,
        effective_queue_limit=lambda gid: 4,
    )

    async def enqueue(gid, tracks):
        added = 0
        for item in tracks:
            if len(state.queue) < 4:
                state.queue.append(item)
                added += 1
        return state, added

    async def extract(query, requester=None, guild_id=None):
        if query == "bad":
            raise RuntimeError("No results found.")
        return _track(query)

    async def start(_guild):
        return None

    music.enqueue_tracks, music.extract_track, music.start_playback_if_needed = enqueue, extract, start

    async def connect(_guild, target, text_channel_id=None):
        if target.name == "Locked":
            raise RuntimeError("I don't have permission to join that voice channel.")
        voice.channel = target
        _guild.voice_client = voice
        music.joined.append(target.name)
        return voice

    async def idle(gid):
        music.idle_scheduled.append(gid)

    music.connect_to_channel, music.schedule_idle_disconnect, music.joined, music.idle_scheduled = connect, idle, [], []
    guild.music, guild.members_by_id, guild.other_channel = music, members, other
    bot = SimpleNamespace(
        storage_path=storage, guilds=[guild], voice_clients=[voice],
        get_guild=lambda gid: guild if gid == 77 else None, get_cog=lambda name: music if name == "Music" else None,
        get_user=lambda uid: None,
    )
    update_guild_settings(storage, 77, {"music_dj_enabled": dj_mode, "blocked_commands": list(blocked)})
    cog = PanelBridge(bot)
    cog._token = TOKEN
    # Built lazily: aiohttp's test server needs a running event loop.
    return (lambda: TestClient(TestServer(cog.build_app()))), state, voice


def _act(client, user_id, action, **extra):
    return client.post("/v1/guilds/77/music/action", headers=AUTH, json={"user_id": str(user_id), "action": action, **extra})


def test_user_guilds_lists_only_shared_guilds_with_standing(storage):
    make_client, _, _ = _music_world(storage)

    async def run():
        async with make_client() as client:
            listener = (await (await client.get("/v1/users/1/guilds", headers=AUTH)).json())["guilds"]
            assert [(g["id"], g["admin"], g["now_playing"]) for g in listener] == [("77", False, "now")]
            assert (await (await client.get("/v1/users/4/guilds", headers=AUTH)).json())["guilds"][0]["admin"] is True
            assert (await (await client.get("/v1/users/999/guilds", headers=AUTH)).json())["guilds"] == []
            member = await (await client.get("/v1/guilds/77/members/999", headers=AUTH)).json()
            assert member["member"] is False and member["admin"] is False

    asyncio.run(run())


def test_listening_is_only_for_users_in_the_bots_channel_while_a_track_is_loaded(storage):
    make_client, state, voice = _music_world(storage, dj_mode=True)

    async def listening(client, user_id):
        return (await (await client.get(f"/v1/users/{user_id}/listening", headers=AUTH)).json())["listening"]

    async def run():
        async with make_client() as client:
            # DJ mode stops user 1 from controlling the music, not from opening the player.
            assert await listening(client, 1) == {"guild_id": "77", "guild_name": "Guild", "channel": "Lounge", "title": "now", "paused": False}
            assert await listening(client, 2) is None    # in another voice channel
            assert await listening(client, 4) is None    # not in voice
            assert await listening(client, 999) is None  # not in the guild
            voice.pause()
            assert (await listening(client, 1))["paused"] is True
            state.current = None
            assert await listening(client, 1) is None    # bot is in the channel but nothing is loaded

    asyncio.run(run())


def test_web_join_brings_the_bot_in_but_never_out_of_a_channel_with_music(storage):
    make_client, state, voice = _music_world(storage)
    guild = voice.guild
    music, members, other = guild.music, guild.members_by_id, guild.other_channel

    async def view(client, user_id):
        return await (await client.get(f"/v1/guilds/77/music?user_id={user_id}", headers=AUTH)).json()

    async def run():
        async with make_client() as client:
            # Music is playing in Lounge: user 2 (in AFK) can't pull the bot over, and isn't offered it.
            assert (await view(client, 2))["join_channel"] is None
            refused = await _act(client, 2, "join")
            assert refused.status == 403 and "already playing music in Lounge" in (await refused.json())["error"]
            # Paused, or between tracks with songs still queued, counts as playing.
            voice.pause()
            assert (await _act(client, 2, "join")).status == 403
            voice._playing, voice._paused, state.current = False, False, None
            assert (await _act(client, 2, "join")).status == 403 and music.joined == []

            # Already in the bot's channel: nothing to do. Not in voice at all: refused.
            assert (await (await _act(client, 1, "join")).json())["detail"] == "Already in Lounge"
            assert (await _act(client, 4, "join")).status == 403 and (await _act(client, 999, "join")).status == 403

            # Idle with an empty queue: it may be moved, and leaves again if nothing gets queued.
            state.queue.clear()
            assert (await view(client, 2))["join_channel"] == "AFK"
            listed = (await (await client.get("/v1/users/2/guilds", headers=AUTH)).json())["guilds"][0]
            assert listed["join_channel"] == "AFK"
            moved = await _act(client, 2, "join")
            assert moved.status == 200 and (await moved.json())["detail"] == "Joined AFK"
            assert music.joined == ["AFK"] and music.idle_scheduled == [77]

            # Not connected anywhere: joins the user's channel; a failed connect is reported.
            guild.voice_client = None
            assert (await view(client, 1))["join_channel"] == "Lounge"
            assert (await _act(client, 1, "join")).status == 200 and music.joined == ["AFK", "Lounge"]
            guild.voice_client = None
            other.name = "Locked"
            failed = await _act(client, 2, "join")
            assert failed.status == 409 and "permission" in (await failed.json())["error"]

    asyncio.run(run())


def test_web_join_follows_dj_mode_and_disabled_commands(storage):
    make_client, _, voice = _music_world(storage, dj_mode=True, blocked=["music join"])
    voice.guild.voice_client = None

    async def run():
        async with make_client() as client:
            blocked = await _act(client, 3, "join")
            assert blocked.status == 403 and "disabled" in (await blocked.json())["error"]

    asyncio.run(run())

    from utils.settings import update_guild_settings
    update_guild_settings(storage, 77, {"blocked_commands": []})

    async def run_dj():
        async with make_client() as client:
            assert (await _act(client, 1, "join")).status == 403   # no DJ role
            assert (await _act(client, 3, "join")).status == 200   # has it

    asyncio.run(run_dj())


def test_server_switches_turn_off_the_web_player_joining_and_discovery(storage):
    from utils.settings import update_guild_settings

    make_client, state, voice = _music_world(storage)
    cog_guild = voice.guild

    async def run():
        async with make_client() as client:
            update_guild_settings(storage, 77, {"web_join_enabled": False})
            state.queue.clear(); state.current = None; voice._playing = False
            refused = await _act(client, 2, "join")
            assert refused.status == 403 and "turned off" in (await refused.json())["error"]
            assert (await _act(client, 1, "shuffle")).status == 200  # the player itself still works

            update_guild_settings(storage, 77, {"web_player_enabled": False})
            view = await (await client.get("/v1/guilds/77/music?user_id=1", headers=AUTH)).json()
            assert view["disabled"] is True and view["session"] is None and "turned off" in view["reason"]
            assert (await _act(client, 1, "shuffle")).status == 403
            listed = (await (await client.get("/v1/users/1/guilds", headers=AUTH)).json())["guilds"][0]
            assert listed["voice_active"] is False and listed["now_playing"] is None and listed["can_control"] is False
            state.current = _track("now")
            assert (await (await client.get("/v1/users/1/listening", headers=AUTH)).json())["listening"] is None

    asyncio.run(run())
    assert cog_guild.music.joined == []


def test_seek_goes_through_the_same_gate_as_the_other_controls(storage):
    make_client, state, voice = _music_world(storage)
    sought = []

    def seek(guild, seconds):
        if seconds >= 100:
            raise RuntimeError("Pick a time between 0:00 and 1:40.")
        sought.append(seconds)
        return state.current

    voice.guild.music.seek = seek
    voice.guild.music.playback_position = lambda guild: 12.34

    async def run():
        async with make_client() as client:
            assert (await _act(client, 2, "seek", seconds="30")).status == 403        # not in the bot's channel
            assert (await _act(client, 1, "seek", seconds="abc")).status == 400
            assert (await _act(client, 1, "seek", seconds="500")).status == 409
            done = await (await _act(client, 1, "seek", seconds="75")).json()
            assert done["detail"] == "Jumped to 1:15 in now" and sought == [75]
            assert done["session"]["position"] == 12.3 and done["session"]["autoplay"] is False

    asyncio.run(run())


def test_live_stream_sends_the_view_and_then_only_changes(storage, monkeypatch):
    import cogs.panel_bridge as bridge_module

    monkeypatch.setattr(bridge_module, "STREAM_TICK_SECONDS", 0.01)
    make_client, state, voice = _music_world(storage)

    async def next_event(response):
        while True:
            line = await asyncio.wait_for(response.content.readline(), timeout=2)
            if line.startswith(b"data: "):
                return __import__("json").loads(line[6:])

    async def run():
        async with make_client() as client:
            assert (await client.get("/v1/guilds/77/music/events?user_id=999", headers=AUTH)).status == 403
            response = await client.get("/v1/guilds/77/music/events?user_id=1", headers=AUTH)
            assert response.status == 200 and response.headers["Content-Type"] == "text/event-stream"
            first = await next_event(response)
            assert first["can_control"] is True and first["session"]["current"]["title"] == "now"
            state.queue.popleft()                       # something changes...
            second = await next_event(response)         # ...and it is pushed without being asked for
            assert second["session"]["queue_length"] == 2
            response.close()

    asyncio.run(run())


def test_music_view_is_for_members_and_control_needs_the_voice_channel(storage):
    make_client, _, _ = _music_world(storage)

    async def run():
        async with make_client() as client:
            assert (await client.get("/v1/guilds/77/music?user_id=999", headers=AUTH)).status == 403
            inside = await (await client.get("/v1/guilds/77/music?user_id=1", headers=AUTH)).json()
            assert inside["can_control"] is True and inside["session"]["queue_length"] == 3
            outside = await (await client.get("/v1/guilds/77/music?user_id=2", headers=AUTH)).json()
            assert outside["can_control"] is False and "Lounge" in outside["reason"]
            # an admin outside the call can look but not touch, same as in Discord
            assert (await _act(client, 4, "skip")).status == 403
            assert (await _act(client, 2, "skip")).status == 403
            assert (await _act(client, 999, "skip")).status == 403

    asyncio.run(run())


def test_music_actions_do_what_the_commands_do(storage):
    make_client, state, voice = _music_world(storage)

    async def run():
        async with make_client() as client:
            assert (await _act(client, 1, "toggle")).status == 200 and voice.is_paused()
            assert (await _act(client, 1, "toggle")).status == 200 and not voice.is_paused()
            assert (await _act(client, 1, "volume", percent="80")).status == 200 and state.volume == 0.8
            assert (await _act(client, 1, "volume", percent="101")).status == 400
            assert (await _act(client, 1, "loop", mode="queue")).status == 200 and state.loop_mode == "queue"
            assert (await _act(client, 1, "loop", mode="sideways")).status == 400
            assert (await _act(client, 1, "move", position="3", to="1", expect_title="c")).status == 200
            assert [t.title for t in state.queue] == ["c", "a", "b"]
            # a stale page: the title at that position is no longer what the user saw
            assert (await _act(client, 1, "remove", position="1", expect_title="a")).status == 409
            assert (await _act(client, 1, "remove", position="9")).status == 409
            assert (await _act(client, 1, "remove", position="1", expect_title="c")).status == 200
            assert [t.title for t in state.queue] == ["a", "b"]
            assert (await _act(client, 1, "play", query="new")).status == 200
            assert (await _act(client, 1, "play", query="bad")).status == 409
            assert (await _act(client, 1, "play", query="x")).status == 200
            full = await _act(client, 1, "play", query="overflow")
            assert full.status == 409 and "full" in (await full.json())["error"]
            assert (await _act(client, 1, "skip")).status == 200 and state.skip_requested and voice.stopped == 1
            assert (await _act(client, 1, "stop")).status == 200 and not state.queue
            assert (await _act(client, 1, "dance")).status == 400

    asyncio.run(run())


def test_autoplay_is_switched_from_the_web_by_those_who_manage_the_server(storage):
    from utils.settings import get_guild_settings

    make_client, _, _ = _music_world(storage)

    async def run():
        async with make_client() as client:
            listener = await (await client.get("/v1/guilds/77/music?user_id=1", headers=AUTH)).json()
            admin = await (await client.get("/v1/guilds/77/music?user_id=4", headers=AUTH)).json()
            assert listener["can_autoplay"] is False and admin["can_autoplay"] is True and admin["session"]["autoplay"] is False
            refused = await _act(client, 1, "autoplay", enabled="true")  # in the call, but it is a server setting
            assert refused.status == 403 and "manage the server" in (await refused.json())["error"]
            done = await (await _act(client, 4, "autoplay", enabled="true")).json()  # an admin need not be in the call
            assert done["session"]["autoplay"] is True and done["detail"].startswith("Autoplay on")
            assert get_guild_settings(storage, 77)["music_autoplay"] is True
            done = await (await _act(client, 4, "autoplay", enabled="false")).json()
            assert done["session"]["autoplay"] is False and get_guild_settings(storage, 77)["music_autoplay"] is False
            assert (await _act(client, 999, "autoplay", enabled="true")).status == 403

    asyncio.run(run())


def test_dj_mode_and_disabled_commands_apply_on_the_web_too(storage):
    make_client, _, _ = _music_world(storage, dj_mode=True, blocked=["music shuffle"])

    async def run():
        async with make_client() as client:
            refused = await _act(client, 1, "skip")
            assert refused.status == 403 and "DJ mode" in (await refused.json())["error"]
            assert (await _act(client, 3, "skip")).status == 200  # has the dj role
            blocked = await _act(client, 3, "shuffle")
            assert blocked.status == 403 and "disabled" in (await blocked.json())["error"]

    asyncio.run(run())


# ---------- command catalogue for the website ----------

def test_command_catalogue_has_categories_parameters_and_permissions(storage):
    @app_commands.command(name="kick", description="Kick a member.")
    @app_commands.describe(member="Who to kick", reason="Why")
    @app_commands.checks.has_permissions(kick_members=True)
    @app_commands.checks.bot_has_permissions(kick_members=True)
    async def kick(interaction: discord.Interaction, member: discord.Member, reason: str = "") -> None: ...

    @app_commands.command(name="ping", description="Check latency.")
    async def ping(interaction: discord.Interaction) -> None: ...

    owner_group = app_commands.Group(name="owner", description="Owner tools")

    @owner_group.command(name="restart", description="Restart.")
    async def restart(interaction: discord.Interaction) -> None: ...

    menu = app_commands.ContextMenu(name="Set Image Source", callback=_menu_callback)
    tree = SimpleNamespace(
        walk_commands=lambda: [kick, ping, owner_group, restart],
        get_commands=lambda type=None: [menu] if type is discord.AppCommandType.message else [],
    )
    bot = SimpleNamespace(storage_path=storage, guilds=[], voice_clients=[], get_guild=lambda _id: None, tree=tree)
    cog = PanelBridge(bot)
    cog._token = TOKEN

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            return await (await client.get("/v1/commands", headers=AUTH)).json()

    body = asyncio.run(run())
    by_name = {c["name"]: c for c in body["commands"]}
    assert by_name["kick"]["category"] == "moderation" and by_name["kick"]["requires"] == ["Kick Members"]
    assert [(p["name"], p["required"]) for p in by_name["kick"]["params"]] == [("member", True), ("reason", False)]
    assert by_name["kick"]["params"][0]["description"] == "Who to kick"
    assert by_name["ping"]["category"] == "fun" and by_name["ping"]["requires"] == []
    assert by_name["owner restart"]["category"] == "owner"
    assert by_name["Set Image Source"]["kind"] == "menu" and by_name["Set Image Source"]["category"] == "images"
    assert [c["key"] for c in body["categories"]][:3] == ["fun", "images", "music"]


async def _menu_callback(interaction, message: "discord.Message") -> None: ...


def test_moderation_tab_lists_timeouts_and_lifts_them_for_admins_only(storage):
    from utils import warnings as warning_store

    make_client, _, voice = _music_world(storage)
    guild = voice.guild
    lifted = []

    def person(uid, muted):
        async def timeout(until, reason=None):
            lifted.append((uid, until))
            member.muted = False

        member = SimpleNamespace(id=uid, display_name=f"m{uid}", display_avatar=None, muted=muted, timeout=timeout, mention=f"<@{uid}>",
                                 timed_out_until=None, guild_permissions=SimpleNamespace(administrator=uid == 4))
        member.is_timed_out = lambda: member.muted
        return member

    people = {uid: person(uid, uid == 2) for uid in (1, 2, 4)}
    guild.get_member, guild.members = people.get, list(people.values())
    guild.me = SimpleNamespace(guild_permissions=SimpleNamespace(moderate_members=True, ban_members=False), joined_at=None)
    for _ in range(3):
        warning_store.add_warning(storage, 77, 2, 4, "spam")

    def act(client, actor, action, target):
        return client.post("/v1/guilds/77/moderation/action", headers=AUTH, json={"user_id": str(actor), "action": action, "target_id": str(target)})

    async def run():
        async with make_client() as client:
            assert (await client.get("/v1/guilds/77/moderation?user_id=1", headers=AUTH)).status == 403  # not an admin
            overview = await (await client.get("/v1/guilds/77/moderation?user_id=4", headers=AUTH)).json()
            assert [m["user_id"] for m in overview["muted"]] == ["2"] and overview["bot_can_unmute"] and not overview["bot_can_unban"]
            assert (await act(client, 1, "unmute", 2)).status == 403 and lifted == []
            assert (await (await act(client, 4, "unmute", 2)).json())["detail"] == "Unmuted m2" and lifted == [(2, None)]
            assert (await (await act(client, 4, "unmute", 2)).json())["detail"] == "m2 isn't muted"
            assert (await act(client, 4, "unmute", 999)).status == 404
            assert (await (await act(client, 4, "clear_warnings", 2)).json())["removed"] == 3
            assert (await act(client, 4, "explode", 2)).status == 400

    asyncio.run(run())
    assert warning_store.list_warnings(storage, 77, 2) == []


def test_member_search_is_for_admins_and_ranks_exact_matches_first(storage):
    from datetime import datetime, timezone

    from utils import cases as case_store
    from utils import warnings as warning_store

    make_client, _, voice = _music_world(storage)
    guild = voice.guild

    def person(uid, name, display, admin=False, muted=False):
        role = SimpleNamespace(id=50 + uid, name=f"r{uid}", position=uid, color=SimpleNamespace(value=0x112233 if uid == 2 else 0), is_default=lambda: False)
        member = SimpleNamespace(id=uid, name=name, display_name=display, global_name=None, bot=False, display_avatar=None, roles=[role],
                                 joined_at=datetime(2025, 1, 1, tzinfo=timezone.utc), created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
                                 timed_out_until=None, is_timed_out=lambda: muted, guild_permissions=SimpleNamespace(administrator=admin))
        member.__str__ = lambda: name
        return member

    people = {1: person(1, "anna", "Anna Banana"), 2: person(2, "banana", "B", muted=True), 3: person(3, "zed", "Savannah"), 4: person(4, "boss", "Boss", admin=True)}
    guild.get_member, guild.members = people.get, list(people.values())
    warning_store.add_warning(storage, 77, 2, 4, "spam")
    case_store.add_case(storage, 77, 2, 4, "Member Warned")

    async def search(client, actor, query):
        response = await client.get("/v1/guilds/77/membersearch", headers=AUTH, params={"user_id": str(actor), "q": query})
        return response.status, (await response.json())

    async def run():
        async with make_client() as client:
            assert (await search(client, 1, "ann"))[0] == 403
            status, found = await search(client, 4, "ANNA")
            assert status == 200 and [m["user_id"] for m in found["members"]] == ["1", "3"]      # exact name, then a name containing it
            _, found = await search(client, 4, "banana")
            assert [m["user_id"] for m in found["members"]] == ["2", "1"]
            card = found["members"][0]
            assert card["warnings"] == 1 and card["cases"] == 1 and card["roles"] == [{"id": "52", "name": "r2", "color": "#112233"}] and card["in_server"]
            assert [m["user_id"] for m in (await search(client, 4, "3"))[1]["members"]] == ["3"]  # by ID
            _, gone = await search(client, 4, "424242")
            assert gone["members"][0]["in_server"] is False and gone["members"][0]["user_id"] == "424242"
            assert (await search(client, 4, "  "))[1] == {"members": [], "more": False}

    asyncio.run(run())


# ---------- commands tab, snipe module and welcome image for the website ----------

def test_command_list_names_each_commands_module(storage):
    @app_commands.command(name="snipe", description="Show the last deleted message.")
    async def snipe(interaction: discord.Interaction) -> None: ...

    @app_commands.command(name="kick", description="Kick a member.")
    async def kick(interaction: discord.Interaction) -> None: ...

    @app_commands.command(name="ping", description="Check latency.")
    async def ping(interaction: discord.Interaction) -> None: ...

    tree = SimpleNamespace(walk_commands=lambda: [snipe, kick, ping], get_commands=lambda type=None: [])

    async def sample(name):
        return (b"GIF89a...", "boom.gif") if name == "animate boom" else None

    cog = PanelBridge(SimpleNamespace(storage_path=storage, guilds=[], voice_clients=[], get_guild=lambda _id: None, tree=tree,
                                      samples=SimpleNamespace(get=sample)))
    cog._token = TOKEN

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            made = await client.get("/v1/samples/animate%20boom", headers=AUTH)
            assert made.status == 200 and made.content_type == "image/gif" and await made.read() == b"GIF89a..."
            assert (await client.get("/v1/samples/ping", headers=AUTH)).status == 404
            assert (await client.get("/v1/samples/animate%20boom")).status == 401
            return await (await client.get("/v1/commands", headers=AUTH)).json()

    by_name = {c["name"]: c for c in asyncio.run(run())["commands"]}
    assert by_name["ping"]["sample"] is False
    assert by_name["snipe"]["module"] == "snipe" and by_name["snipe"]["module_flag"] == "snipe_enabled" and by_name["snipe"]["module_solo"] is True
    assert by_name["kick"]["module"] == "moderation" and by_name["kick"]["module_solo"] is False
    assert by_name["ping"]["module"] is None
    assert by_name["kick"]["added"] == "2026-03-28" and by_name["ping"]["added"] == "2026-03-27"  # from utils/command_dates.json


def test_saving_settings_keeps_snipe_and_its_module_in_step(storage):
    async def run():
        async with _client(storage) as client:
            async def put(**changes):
                current = (await (await client.get("/v1/guilds/42/settings", headers=AUTH)).json())["settings"]
                response = await client.put("/v1/guilds/42/settings", headers=AUTH, json={"settings": {**current, **changes}})
                assert response.status == 200
                return (await response.json())["settings"]

            off = await put(blocked_commands=["snipe", "fun"])
            assert off["snipe_enabled"] is False
            on = await put(snipe_enabled=True)
            assert on["snipe_enabled"] is True and on["blocked_commands"] == ["fun"]
            assert (await put(welcome_card_background="image"))["welcome_card_background"] == "gradient"  # nothing uploaded yet

    asyncio.run(run())


def test_welcome_background_upload_and_preview_are_for_admins(storage):
    import base64
    import io

    from PIL import Image

    from cogs.welcome import Welcome
    from utils import welcome_card

    async def read():
        out = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(out, format="PNG")
        return out.getvalue()

    avatar = SimpleNamespace(url="https://cdn.discordapp.com/a.png", read=read)
    avatar.replace = lambda **kw: avatar

    def person(uid):
        return SimpleNamespace(id=uid, display_name=f"m{uid}", display_avatar=avatar, guild=guild, mention=f"<@{uid}>",
                               guild_permissions=SimpleNamespace(administrator=uid == 4), __str__=lambda: f"m{uid}")

    guild = SimpleNamespace(id=77, name="The Den", member_count=12, owner_id=9)
    people = {uid: person(uid) for uid in (1, 4)}
    guild.get_member = people.get
    bot = SimpleNamespace(storage_path=storage, guilds=[guild], voice_clients=[], get_guild=lambda gid: guild if gid == 77 else None)
    bot.get_cog = lambda name: Welcome(bot) if name == "Welcome" else None
    cog = PanelBridge(bot)
    cog._token = TOKEN
    upload = io.BytesIO()
    Image.new("RGB", (1600, 900), (10, 120, 240)).save(upload, format="PNG")
    picture = base64.b64encode(upload.getvalue()).decode()

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            async def post(path, **body):
                response = await client.post(f"/v1/guilds/77/welcome/{path}", headers=AUTH, json=body)
                return response.status, await response.json()

            assert (await post("background", user_id="1", action="upload", image=picture))[0] == 403
            assert (await post("background", user_id="4", action="upload", image="!!!"))[0] == 400
            assert (await post("background", user_id="4", action="upload", image=base64.b64encode(b"nope").decode()))[0] == 400
            assert welcome_card.read_background(storage, 77) is None
            status, body = await post("background", user_id="4", action="upload", image=picture)
            assert status == 200 and body["settings"]["welcome_card_background"] == "image" and body["before"]["welcome_card_background"] == "gradient"
            assert Image.open(io.BytesIO(welcome_card.read_background(storage, 77))).size == welcome_card.CARD_SIZE

            assert (await post("card", user_id="1"))[0] == 403
            status, body = await post("card", user_id="4")
            card = Image.open(io.BytesIO(base64.b64decode(body["image"]))).convert("RGB")
            assert status == 200 and card.size == welcome_card.CARD_SIZE and card.getpixel((990, 5))[2] > card.getpixel((990, 5))[0]  # the blue upload shows
            # Unsaved card settings can be tried out, and nonsense is refused.
            status, body = await post("card", user_id="4", settings={"welcome_card_background": "gradient", "welcome_card_gradient": "solid", "welcome_card_color_start": "#ff0000"})
            assert Image.open(io.BytesIO(base64.b64decode(body["image"]))).convert("RGB").getpixel((990, 5)) == (255, 0, 0)
            assert (await post("card", user_id="4", settings={"welcome_card_gradient": "spiral"}))[0] == 400
            assert get_guild_settings(storage, 77)["welcome_card_gradient"] == "horizontal"  # trying out saves nothing

            status, body = await post("background", user_id="4", action="remove")
            assert status == 200 and body["settings"]["welcome_card_background"] == "gradient" and body["has_background"] is False

    asyncio.run(run())


# ---------- announcements ----------

def test_an_announcement_is_posted_with_its_sample_and_published(storage):
    from unittest.mock import Mock

    from utils.embeds import EmbedManager

    sent, published = [], []

    def channel(news=True, can_send=True, publish_error=None):
        made = Mock(spec=discord.TextChannel)
        made.id, made.name, made.guild = 55, "updates", SimpleNamespace(name="Home", me=object())
        made.is_news = lambda: news
        made.permissions_for = lambda _me: SimpleNamespace(view_channel=True, send_messages=can_send, embed_links=True, attach_files=True)

        async def send(embed=None, files=None):
            sent.append((embed, files))

            async def publish():
                if publish_error:
                    raise publish_error
                published.append(1)

            return SimpleNamespace(id=900, jump_url="https://discord.com/channels/1/55/900", publish=publish)

        made.send = send
        return made

    channels = {55: channel(), 56: channel(news=False), 57: channel(can_send=False)}

    async def sample(name):
        return (b"GIF", "boom.gif") if name == "animate boom" else None

    bot = SimpleNamespace(storage_path=storage, guilds=[], voice_clients=[], get_guild=lambda _id: None, embeds=EmbedManager(),
                          get_channel=channels.get, samples=SimpleNamespace(get=sample))
    cog = PanelBridge(bot)
    cog._token = TOKEN

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            seen = (await (await client.get("/v1/announce/channel/55", headers=AUTH)).json())["channel"]
            assert seen == {"id": "55", "name": "updates", "guild": "Home", "news": True, "can_post": True, "can_publish": True}
            assert (await client.get("/v1/announce/channel/1", headers=AUTH)).status == 404
            assert (await client.get("/v1/announce/channel/abc", headers=AUTH)).status == 404

            post = {"channel_id": "55", "title": "New: /animate boom", "body": "It goes boom.", "sample": "animate boom"}
            done = await (await client.post("/v1/announce", headers=AUTH, json=post)).json()
            assert done == {"message_id": "900", "url": "https://discord.com/channels/1/55/900", "published": True, "note": ""}
            embed, files = sent[-1]
            assert embed.title == "New: /animate boom" and embed.description == "It goes boom." and embed.image.url == "attachment://boom.gif"
            assert files[0].filename == "boom.gif" and published == [1]

            plain = await (await client.post("/v1/announce", headers=AUTH, json={**post, "channel_id": "56", "sample": "ping"})).json()
            assert plain["published"] is False and "ordinary channel" in plain["note"] and sent[-1][1] == [] and published == [1]
            refused = await client.post("/v1/announce", headers=AUTH, json={**post, "channel_id": "57"})
            assert refused.status == 409 and "Send Messages" in (await refused.json())["error"]
            assert (await client.post("/v1/announce", headers=AUTH, json={**post, "body": " "})).status == 400
            assert len(sent) == 2

    asyncio.run(run())


@pytest.mark.parametrize("key, value", [
    ("logging_channel_id", "9" * 100_000), ("verify_role_id", "²"), ("bot_channels", ["²"]), ("bot_channels", [int("9" * 4000)]),
    ("warn_mute_duration", "1s" * 50_000), ("blocked_commands", ["x" * 20_000]),
])
def test_oversized_and_lookalike_values_are_refused_not_stored(key, value):
    with pytest.raises(SettingsValidationError):
        validate_settings({**DEFAULT_GUILD_SETTINGS, key: value}, DEFAULT_GUILD_SETTINGS)
