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


def test_public_playlists_are_discoverable_only_through_a_shared_server(storage):
    FRIEND, LONER = 33, 44

    def guild(name, *member_ids):
        members = {uid: SimpleNamespace(id=uid, display_name=f"m{uid}") for uid in member_ids}
        return SimpleNamespace(id=abs(hash(name)) % 10**6, name=name, get_member=members.get)

    cog = _cog(storage)
    cog.bot.guilds = [guild("Alpha", ME, FRIEND), guild("Beta", FRIEND, OTHER), guild("Gamma", LONER)]
    for owner, name, status, songs in ((FRIEND, "open", "public", 2), (FRIEND, "empty", "public", 0), (FRIEND, "hidden", "private", 1),
                                       (OTHER, "theirs", "public", 1), (LONER, "alone", "public", 1), (ME, "mine", "public", 1)):
        store.create_playlist(storage, owner, name)
        if songs:
            store.add_songs(storage, owner, name, [_song(n) for n in range(songs)])
        store.set_status(storage, owner, name, status)

    async def get(client, path):
        response = await client.get(path, headers=AUTH)
        return response.status, await response.json()

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            _, found = await get(client, f"/v1/users/{ME}/playlists/discover")
            # Not my own, not private, not empty, and not from people I share no server with.
            assert [(p["owner_id"], p["name"]) for p in found["playlists"]] == [(str(FRIEND), "open")]
            assert found["profiles"] == [{"user_id": str(FRIEND), "name": "m33", "avatar": None, "servers": ["Alpha"], "playlist_count": 1}]

            status, profile = await get(client, f"/v1/users/{ME}/profiles/{FRIEND}")
            assert status == 200 and [p["name"] for p in profile["playlists"]] == ["open"]
            assert profile["profile"]["servers"] == ["Alpha"] and profile["profile"]["mine"] is False  # Beta is not named: I'm not in it

            assert (await get(client, f"/v1/users/{ME}/profiles/{OTHER}"))[0] == 404  # in the bot's servers, but none of mine
            assert (await get(client, f"/v1/users/{ME}/profiles/999"))[0] == 404
            assert (await get(client, f"/v1/users/{ME}/profiles/abc"))[0] == 404
            status, own = await get(client, f"/v1/users/{ME}/profiles/{ME}")
            assert status == 200 and own["profile"]["mine"] is True and [p["name"] for p in own["playlists"]] == ["mine"]
            # Someone in no server with anyone still sees their own profile, and nobody else's playlists.
            assert (await get(client, f"/v1/users/{LONER}/playlists/discover"))[1] == {"profiles": [], "playlists": [], "popular": []}
            # A server can take its members out of each other's discovery.
            from utils.settings import update_guild_settings
            update_guild_settings(storage, cog.bot.guilds[0].id, {"web_discovery_enabled": False})
            assert (await get(client, f"/v1/users/{ME}/playlists/discover"))[1]["playlists"] == []
            assert (await get(client, f"/v1/users/{ME}/profiles/{FRIEND}"))[0] == 404

    asyncio.run(run())


def test_search_and_most_liked_cover_public_playlists_across_the_bot(storage):
    cog = _cog(storage)
    for owner, name, status, songs in ((OTHER, "roadtrip", "public", [_song(1)]), (OTHER, "hidden", "private", [_song(1)]),
                                       (33, "gym", "public", [_song(7)]), (33, "empty", "public", []), (ME, "mine", "public", [_song(1)])):
        store.create_playlist(storage, owner, name)
        if songs:
            store.add_songs(storage, owner, name, songs)
        store.set_status(storage, owner, name, status)
    store.toggle_like(storage, 33, "gym", ME, desired=True)

    async def names(client, query):
        response = await client.get(f"/v1/users/{ME}/playlists/search", headers=AUTH, params={"q": query})
        return [p["name"] for p in (await response.json())["playlists"]]

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert await names(client, "ROAD") == ["roadtrip"]          # by name, any case
            assert await names(client, "song 7") == ["gym"]             # by a song in it
            assert await names(client, "other#0") == ["roadtrip"]       # by owner
            assert await names(client, "song") == ["gym", "roadtrip"]   # most liked first; never private, empty or my own
            assert await names(client, "road gym") == [] and await names(client, "  ") == []
            found = await (await client.get(f"/v1/users/{ME}/playlists/discover", headers=AUTH)).json()
            assert [p["name"] for p in found["popular"]] == ["gym"]     # only playlists somebody liked

    asyncio.run(run())


def test_importing_a_playlist_link_fills_up_to_the_cap(storage):
    cog = _cog(storage)
    asked = []

    async def extract_playlist_songs(url, limit):
        asked.append((url, limit))
        if "bad" in url:
            raise RuntimeError("I couldn't find any songs at that link. Is it a public playlist?")
        return [_song(n) for n in range(limit)]

    cog.bot.get_cog("Music").extract_playlist_songs = extract_playlist_songs
    store.create_playlist(storage, ME, "big")
    store.add_songs(storage, ME, "big", [_song(n) for n in range(5)])

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await _act(client, ME, action="import", name="nope", url="https://youtube.com/playlist?list=x")).status == 404
            failed = await _act(client, ME, action="import", name="big", url="https://youtube.com/bad")
            assert failed.status == 409 and "public playlist" in (await failed.json())["error"]
            done = await (await _act(client, ME, action="import", name="big", url="https://youtube.com/playlist?list=x")).json()
            assert done["detail"] == "Imported 45 songs" and done["playlist"]["song_count"] == 50
            assert asked[-1] == ("https://youtube.com/playlist?list=x", 45)  # only asks for what fits
            assert (await _act(client, ME, action="import", name="big", url="https://youtube.com/playlist?list=x")).status == 409  # full

    asyncio.run(run())


def test_saving_from_the_player_needs_only_membership(storage):
    from collections import deque

    cog = _cog(storage)
    track = lambda title: SimpleNamespace(title=title, to_storage_dict=lambda: _song(1) | {"title": title})  # noqa: E731
    state = SimpleNamespace(current=track("now"), queue=deque([track("next"), track("later")]))
    cog.bot.get_cog("Music").guild_states = {77: state}
    guild = SimpleNamespace(id=77, get_member=lambda uid: object() if uid == ME else None)
    cog.bot.get_guild = lambda gid: guild if gid == 77 else None
    store.create_playlist(storage, ME, "keep")
    store.create_playlist(storage, OTHER, "keep")

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            saved = await (await _act(client, ME, action="save_song", name="keep", guild_id="77", position="0", expect_title="now")).json()
            assert saved["detail"] == "Saved now to keep"
            assert (await _act(client, ME, action="save_song", name="keep", guild_id="77", position="2", expect_title="later")).status == 200
            assert (await _act(client, ME, action="save_song", name="keep", guild_id="77", position="1", expect_title="stale")).status == 409
            assert (await _act(client, ME, action="save_song", name="keep", guild_id="77", position="9")).status == 409
            assert (await _act(client, OTHER, action="save_song", name="keep", guild_id="77", position="0")).status == 404  # not in that server
            whole = await (await _act(client, ME, action="save_queue", name="tonight", guild_id="77")).json()
            assert whole["detail"] == "Saved 3 songs as tonight"
            assert (await _act(client, ME, action="save_queue", name="tonight", guild_id="77")).status == 400  # the name is taken
            state.current, state.queue = None, deque()
            assert (await _act(client, ME, action="save_queue", name="nothing", guild_id="77")).status == 409

    asyncio.run(run())
    assert [s["title"] for s in store.get_playlist(storage, ME, "keep")["songs"]] == ["now", "later"]
    assert [s["title"] for s in store.get_playlist(storage, ME, "tonight")["songs"]] == ["now", "next", "later"]
    assert store.get_playlist(storage, ME, "nothing") is None and store.get_playlist(storage, OTHER, "keep")["songs"] == []
