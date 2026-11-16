from __future__ import annotations

import asyncio
from types import SimpleNamespace

from aiohttp.test_utils import TestClient, TestServer

from cogs.panel_bridge import PanelBridge
from utils import playlists as store

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
ME, OTHER = 11, 22


def _song(n):
    return {"title": f"Song {n}", "webpage_url": f"https://youtu.be/{n}", "uploader": "U", "duration": 100 + n, "thumbnail": None}


def _cog(storage):
    async def extract(query, requester=None, guild_id=None):
        if query == "bad":
            raise RuntimeError("No results found.")
        return SimpleNamespace(title=query, to_storage_dict=lambda: {"title": query, "webpage_url": f"https://youtu.be/{query}", "uploader": None, "duration": 60, "thumbnail": None})

    music = SimpleNamespace(extract_track=extract)
    users = {ME: "me#0", OTHER: "other#0"}
    bot = SimpleNamespace(
        storage_path=storage, guilds=[], voice_clients=[], get_guild=lambda _id: None,
        get_cog=lambda name: music if name == "Music" else None, get_user=lambda uid: users.get(uid),
    )
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return cog


def _act(client, user, **body):
    return client.post(f"/v1/users/{user}/playlists/action", headers=AUTH, json=body)


def test_move_song_reorders_and_validates(storage):
    store.create_playlist(storage, ME, "mix")
    store.add_songs(storage, ME, "mix", [_song(1), _song(2), _song(3)])
    assert [s["title"] for s in store.move_song(storage, ME, "mix", 3, 1)["songs"]] == ["Song 3", "Song 1", "Song 2"]
    assert [s["title"] for s in store.get_playlist(storage, ME, "mix")["songs"]] == ["Song 3", "Song 1", "Song 2"]
    for args in ((0, 1), (1, 4), (9, 1)):
        try:
            store.move_song(storage, ME, "mix", *args)
            raise AssertionError("expected PlaylistError")
        except store.PlaylistError:
            pass


def test_playlist_lifecycle_through_the_bridge(storage):
    cog = _cog(storage)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await _act(client, ME, action="create", name="road trip")).status == 400  # spaces not allowed
            assert (await _act(client, ME, action="create", name="RoadTrip")).status == 200
            assert (await _act(client, ME, action="create", name="roadtrip")).status == 400  # duplicate
            for query in ("one", "two", "three"):
                assert (await _act(client, ME, action="add_song", name="roadtrip", query=query)).status == 200
            failed = await _act(client, ME, action="add_song", name="roadtrip", query="bad")
            assert failed.status == 409 and "No results" in (await failed.json())["error"]
            assert (await _act(client, ME, action="add_song", name="nope", query="x")).status == 404
            assert (await _act(client, ME, action="move_song", name="roadtrip", position="3", to="1")).status == 200
            assert (await _act(client, ME, action="remove_song", name="roadtrip", position="2")).status == 200
            assert (await _act(client, ME, action="remove_song", name="roadtrip", position="9")).status == 400
            assert (await _act(client, ME, action="details", name="roadtrip", status="private", description="For the car")).status == 200
            assert (await _act(client, ME, action="details", name="roadtrip", status="secret", description="")).status == 400
            assert (await _act(client, ME, action="rename", name="roadtrip", new_name="Car_Mix")).status == 200
            assert (await _act(client, ME, action="shuffle", name="car_mix")).status == 200
            assert (await _act(client, ME, action="explode", name="car_mix")).status == 400
            listing = await (await client.get(f"/v1/users/{ME}/playlists", headers=AUTH)).json()
            view = await (await client.get(f"/v1/users/{ME}/playlists/{ME}/car_mix", headers=AUTH)).json()
            return listing, view

    listing, view = asyncio.run(run())
    assert [(p["display_name"], p["status"], p["song_count"], p["mine"]) for p in listing["own"]] == [("Car_Mix", "private", 2, True)]
    assert listing["limits"]["playlists"] == 5 and listing["liked"] == []
    assert sorted(s["title"] for s in view["playlist"]["songs"]) == ["three", "two"]
    assert view["playlist"]["description"] == "For the car" and view["playlist"]["owner_name"] == "me#0"


def test_nobody_can_touch_or_see_what_is_not_theirs(storage):
    cog = _cog(storage)
    store.create_playlist(storage, OTHER, "theirs")
    store.add_songs(storage, OTHER, "theirs", [_song(1)])
    store.create_playlist(storage, OTHER, "secret")
    store.set_status(storage, OTHER, "secret", "private")

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            # every edit is applied to the acting user's own playlists, where "theirs" does not exist
            for body in ({"action": "delete"}, {"action": "rename", "new_name": "mine"}, {"action": "remove_song", "position": "1"},
                         {"action": "details", "status": "private", "description": "x"}, {"action": "shuffle"}):
                assert (await _act(client, ME, name="theirs", owner_id=str(OTHER), **body)).status == 400, body
            assert (await client.get(f"/v1/users/{ME}/playlists/{OTHER}/theirs", headers=AUTH)).status == 200  # public: viewable
            assert (await client.get(f"/v1/users/{ME}/playlists/{OTHER}/secret", headers=AUTH)).status == 404  # private looks missing
            assert (await client.get(f"/v1/users/{ME}/playlists/{OTHER}/nope", headers=AUTH)).status == 404
            assert (await _act(client, ME, action="like", name="secret", owner_id=str(OTHER))).status == 400
            assert (await _act(client, ME, action="like", name="theirs", owner_id=str(OTHER))).status == 200
            liked = (await (await client.get(f"/v1/users/{ME}/playlists", headers=AUTH)).json())["liked"]
            assert [(p["display_name"], p["owner_name"], p["liked"], p["mine"]) for p in liked] == [("theirs", "other#0", True, False)]
            assert (await _act(client, ME, action="unlike", name="theirs", owner_id=str(OTHER))).status == 200
            assert (await (await client.get(f"/v1/users/{ME}/playlists", headers=AUTH)).json())["liked"] == []

    asyncio.run(run())
    assert len(store.get_playlist(storage, OTHER, "theirs")["songs"]) == 1 and store.get_playlist(storage, OTHER, "secret")["status"] == "private"


def test_queueing_a_playlist_from_the_web_player(storage, monkeypatch):
    import sys
    from collections import deque

    # the bridge imports Track from the music cog; give it a light stand-in
    def from_storage_dict(song, requester=None):
        return SimpleNamespace(title=song["title"], webpage_url=song["webpage_url"], uploader=None, duration=song["duration"],
                               thumbnail=None, requested_by_name="me")

    fake_music_module = SimpleNamespace(Track=SimpleNamespace(from_storage_dict=from_storage_dict))
    monkeypatch.setitem(sys.modules, "cogs.music", fake_music_module)

    store.create_playlist(storage, ME, "mix")
    store.add_songs(storage, ME, "mix", [_song(1), _song(2), _song(3)])
    store.create_playlist(storage, ME, "empty")
    store.create_playlist(storage, OTHER, "secret")
    store.add_songs(storage, OTHER, "secret", [_song(9)])
    store.set_status(storage, OTHER, "secret", "private")

    channel = SimpleNamespace(name="Lounge", members=[])
    member = SimpleNamespace(id=ME, bot=False, display_name="me", roles=[], voice=SimpleNamespace(channel=channel), guild_permissions=SimpleNamespace(administrator=False))
    outsider = SimpleNamespace(id=OTHER, bot=False, display_name="other", roles=[], voice=None, guild_permissions=SimpleNamespace(administrator=False))
    voice = SimpleNamespace(channel=channel, is_connected=lambda: True, is_playing=lambda: True, is_paused=lambda: False, source=None)
    state = SimpleNamespace(queue=deque(), current=None, volume=0.5, loop_mode="off")

    async def enqueue(gid, tracks):
        room = 2 - len(state.queue)
        state.queue.extend(tracks[:room])
        return state, min(len(tracks), room)

    async def start(_guild):
        return None

    music = SimpleNamespace(guild_states={77: state}, get_state=lambda gid: state, enqueue_tracks=enqueue, start_playback_if_needed=start,
                            effective_queue_limit=lambda gid: 2)
    guild = SimpleNamespace(id=77, name="G", icon=None, owner_id=1, voice_client=voice, get_member={ME: member, OTHER: outsider}.get,
                            member_count=2, created_at=None, me=None, unavailable=False)
    bot = SimpleNamespace(storage_path=storage, guilds=[guild], voice_clients=[voice], get_guild=lambda gid: guild if gid == 77 else None,
                          get_cog=lambda name: music if name == "Music" else None)
    cog = PanelBridge(bot)
    cog._token = TOKEN

    def act(client, user, **body):
        return client.post("/v1/guilds/77/music/action", headers=AUTH, json={"user_id": str(user), "action": "playlist", **body})

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await act(client, OTHER, name="mix", owner_id=str(ME))).status == 403  # not in the voice channel
            assert (await act(client, ME, name="secret", owner_id=str(OTHER))).status == 400  # someone else's private playlist
            assert (await act(client, ME, name="empty")).status == 409
            ok = await act(client, ME, name="mix")
            assert ok.status == 200 and "2 of 3" in (await ok.json())["detail"]  # the queue had room for two
            full = await act(client, ME, name="mix")
            assert full.status == 409 and "full" in (await full.json())["error"]
            state.queue.clear()
            single = await act(client, ME, name="mix", position="3", expect_title="Song 3")
            assert single.status == 200 and (await single.json())["detail"] == "Queued Song 3"
            assert (await act(client, ME, name="mix", position="3", expect_title="Something else")).status == 409  # stale page
            assert (await act(client, ME, name="mix", position="9")).status == 409
            guilds = await (await client.get(f"/v1/users/{ME}/guilds", headers=AUTH)).json()
            away = await (await client.get(f"/v1/users/{OTHER}/guilds", headers=AUTH)).json()
            assert [g["can_control"] for g in guilds["guilds"]] == [True] and [g["can_control"] for g in away["guilds"]] == [False]

    asyncio.run(run())
    assert [t.title for t in state.queue] == ["Song 3"]
