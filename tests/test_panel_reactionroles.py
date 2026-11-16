from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
from aiohttp.test_utils import TestClient, TestServer

from cogs.panel_bridge import PanelBridge
from utils import reactionroles as store

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GUILD_ID, CHANNEL_ID, MESSAGE_ID = 77, 500, 9001
LINK = f"https://discord.com/channels/{GUILD_ID}/{CHANNEL_ID}/{MESSAGE_ID}"


class Role:
    def __init__(self, role_id, name, position, *, managed=False, default=False):
        self.id, self.name, self.position, self.managed, self._default = role_id, name, position, managed, default
        self.color = SimpleNamespace(value=0)

    def is_default(self):
        return self._default

    def __lt__(self, other):
        return self.position < other.position

    def __ge__(self, other):
        return self.position >= other.position


class Message:
    def __init__(self, message_id, channel):
        self.id, self.channel = message_id, channel
        self.reactions: list[str] = []
        self.deleted = False

    async def add_reaction(self, emoji):
        if "bogus" in str(emoji):
            raise discord.HTTPException(SimpleNamespace(status=400, reason="x"), {"code": 10014, "message": "Unknown Emoji"})
        self.reactions.append(str(emoji))

    async def remove_reaction(self, emoji, user):
        self.reactions.remove(str(emoji))

    async def clear_reaction(self, emoji):
        if str(emoji) in self.reactions:
            self.reactions.remove(str(emoji))

    async def delete(self):
        self.deleted = True


class Channel:
    def __init__(self, channel_id, name, *, allowed=True):
        self.id, self.name, self.position, self.category = channel_id, name, 0, None
        self.allowed = allowed
        self.messages: dict[int, Message] = {}
        self.sent: list[Message] = []

    def permissions_for(self, member):
        return SimpleNamespace(view_channel=True, send_messages=self.allowed, embed_links=True, add_reactions=True, read_message_history=True)

    async def fetch_message(self, message_id):
        if message_id not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="x"), {"code": 10008, "message": "Unknown Message"})
        return self.messages[message_id]

    async def send(self, embed=None):
        message = Message(10_000 + len(self.sent), self)
        message.embed = embed
        self.sent.append(message)
        self.messages[message.id] = message
        return message


def _world(storage):
    """Roles by rank: everyone(0) < member(1) < helper(2) < mod(3) < botrole(5) < admin(6).
    User 1 is an Administrator whose top role is mod; user 2 is a plain member;
    user 9 owns the server."""
    roles = {r.id: r for r in (
        Role(GUILD_ID, "@everyone", 0, default=True), Role(11, "member", 1), Role(12, "helper", 2), Role(13, "mod", 3),
        Role(14, "booster", 1, managed=True), Role(15, "botrole", 5), Role(16, "admin", 6),
    )}
    channel, locked = Channel(CHANNEL_ID, "roles"), Channel(501, "locked", allowed=False)
    channel.messages[MESSAGE_ID] = Message(MESSAGE_ID, channel)
    channels = {c.id: c for c in (channel, locked)}

    def member(uid, top, admin):
        return SimpleNamespace(id=uid, top_role=roles[top], guild_permissions=SimpleNamespace(administrator=admin))

    members = {1: member(1, 13, True), 2: member(2, 11, False), 9: member(9, 11, False)}
    guild = SimpleNamespace(
        id=GUILD_ID, owner_id=9, roles=list(roles.values()), text_channels=list(channels.values()), emojis=[],
        me=SimpleNamespace(top_role=roles[15], guild_permissions=SimpleNamespace(manage_roles=True)),
        get_member=members.get, get_role=roles.get, get_channel=channels.get, get_thread=lambda _id: None,
    )
    bot = SimpleNamespace(storage_path=storage, guilds=[guild], user=SimpleNamespace(id=999), get_guild=lambda gid: guild if gid == GUILD_ID else None)
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return cog, channel


def _post(client, path, **body):
    return client.post(f"/v1/guilds/{GUILD_ID}/reactionroles/{path}", headers=AUTH, json=body)


def test_overview_lists_only_roles_the_admin_may_hand_out(storage):
    cog, _ = _world(storage)
    store.add_mapping(storage, GUILD_ID, MESSAGE_ID, CHANNEL_ID, "✅", 11)
    store.add_mapping(storage, 9999, 555, 1, "✅", 11)  # another server's mapping

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await client.get(f"/v1/guilds/{GUILD_ID}/reactionroles?user_id=2", headers=AUTH)).status == 403  # not an admin
            as_admin = await (await client.get(f"/v1/guilds/{GUILD_ID}/reactionroles?user_id=1", headers=AUTH)).json()
            as_owner = await (await client.get(f"/v1/guilds/{GUILD_ID}/reactionroles?user_id=9", headers=AUTH)).json()
            return as_admin, as_owner

    as_admin, as_owner = asyncio.run(run())
    # below the admin's own top role (mod), below the bot's role, not managed, not @everyone
    assert [r["name"] for r in as_admin["roles"]] == ["helper", "member"]
    # the server owner is only limited by what the bot itself can assign
    assert [r["name"] for r in as_owner["roles"]] == ["mod", "helper", "member"]
    assert [c["name"] for c in as_admin["channels"]] == ["roles"]  # the bot can't post in #locked
    assert len(as_admin["messages"]) == 1 and as_admin["messages"][0]["items"][0]["role_name"] == "member"
    assert as_admin["used"] == 1 and as_admin["messages"][0]["url"] == LINK


def test_add_reacts_and_stores_and_enforces_role_rules(storage):
    cog, channel = _world(storage)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            ok = await _post(client, "add", user_id="1", message_link=LINK, emoji="🎮", role_id="12")
            assert ok.status == 200
            for role_id, fragment in (("13", "your own highest role"), ("16", "above the bot"), ("14", "managed"), (str(GUILD_ID), "@everyone"), ("404", "no longer exists")):
                refused = await _post(client, "add", user_id="1", message_link=LINK, emoji="⭐", role_id=role_id)
                assert refused.status in (403, 404) and fragment in (await refused.json())["error"], role_id
            assert (await _post(client, "add", user_id="2", message_link=LINK, emoji="⭐", role_id="11")).status == 403  # not an admin
            assert (await _post(client, "add", user_id="9", message_link=LINK, emoji="⭐", role_id="13")).status == 200  # owner may
            assert (await _post(client, "add", user_id="1", message_link="not a link", emoji="⭐", role_id="11")).status == 400
            other = LINK.replace(f"/{GUILD_ID}/", "/12345/")
            assert (await _post(client, "add", user_id="1", message_link=other, emoji="⭐", role_id="11")).status == 400
            missing = LINK.replace(str(MESSAGE_ID), "1")
            assert (await _post(client, "add", user_id="1", message_link=missing, emoji="⭐", role_id="11")).status == 404
            bad = await _post(client, "add", user_id="1", message_link=LINK, emoji="bogus", role_id="11")
            assert bad.status == 400 and "isn't an emoji" in (await bad.json())["error"]

    asyncio.run(run())
    assert channel.messages[MESSAGE_ID].reactions == ["🎮", "⭐"]
    assert {(r["emoji"], r["role_id"]) for r in store.list_for_guild(storage, GUILD_ID)} == {("🎮", 12), ("⭐", 13)}


def test_create_posts_a_message_and_validates_before_posting(storage):
    cog, channel = _world(storage)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            base = {"user_id": "1", "channel_id": str(CHANNEL_ID), "title": "Pick roles", "description": "React below"}
            for body in (
                {**base, "pairs": []},
                {**base, "title": "", "pairs": [{"emoji": "✅", "role_id": "11"}]},
                {**base, "color": "blue", "pairs": [{"emoji": "✅", "role_id": "11"}]},
                {**base, "pairs": [{"emoji": "✅", "role_id": "11"}, {"emoji": "✅", "role_id": "12"}]},
                {**base, "pairs": [{"emoji": "✅", "role_id": "11"}, {"emoji": "⭐", "role_id": "16"}]},  # one role not allowed
                {**base, "pairs": [{"emoji": "✅", "role_id": "11"}] * 11},
                {**base, "channel_id": "501", "pairs": [{"emoji": "✅", "role_id": "11"}]},  # bot can't post there
                {**base, "channel_id": "999", "pairs": [{"emoji": "✅", "role_id": "11"}]},
            ):
                assert (await _post(client, "create", **body)).status in (400, 403, 404, 409), body
            assert channel.sent == []  # nothing was posted by any refused request

            ok = await _post(client, "create", **base, color="#5865F2", pairs=[{"emoji": "✅", "role_id": "11"}, {"emoji": "bogus", "role_id": "12"}, {"emoji": "", "role_id": ""}])
            body = await ok.json()
            assert ok.status == 200 and body["added"] == ["✅"] and body["failed"][0]["emoji"] == "bogus"

            useless = await _post(client, "create", **base, pairs=[{"emoji": "bogus", "role_id": "11"}])
            assert useless.status == 400

    asyncio.run(run())
    assert len(channel.sent) == 2 and channel.sent[0].embed.title == "Pick roles" and channel.sent[0].reactions == ["✅"]
    assert channel.sent[1].deleted is True  # a message with no working emoji is not left behind
    assert [(r["emoji"], r["role_id"]) for r in store.list_for_guild(storage, GUILD_ID)] == [("✅", 11)]


def test_remove_is_scoped_to_the_server_and_clears_the_reaction(storage):
    cog, channel = _world(storage)
    message = channel.messages[MESSAGE_ID]
    message.reactions = ["✅", "⭐"]
    store.add_mapping(storage, GUILD_ID, MESSAGE_ID, CHANNEL_ID, "✅", 11)
    store.add_mapping(storage, GUILD_ID, MESSAGE_ID, CHANNEL_ID, "⭐", 12)
    store.add_mapping(storage, 9999, 555, 1, "✅", 11)  # another server

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await _post(client, "remove", user_id="2", message_id=str(MESSAGE_ID), emoji="✅")).status == 403
            assert (await _post(client, "remove", user_id="1", message_id="555", emoji="✅")).status == 404  # other server's message
            assert (await _post(client, "remove", user_id="1", message_id=str(MESSAGE_ID), emoji="✅")).status == 200
            assert (await _post(client, "remove", user_id="1", message_id=str(MESSAGE_ID), emoji="✅")).status == 404
            assert (await _post(client, "remove", user_id="1", message_id=str(MESSAGE_ID))).status == 200  # the rest of the message

    asyncio.run(run())
    assert message.reactions == []
    assert store.list_for_guild(storage, GUILD_ID) == [] and len(store.list_for_guild(storage, 9999)) == 1
