from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from cogs.panel_bridge import PanelBridge
from utils import appeals as store
from utils.settings import update_guild_settings

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def test_questions_are_the_servers_own_or_the_default_set():
    assert store.questions_for({}) == list(store.DEFAULT_QUESTIONS)
    assert store.questions_for({"appeals_questions": " What happened? \n\n  Will it happen again?\n"}) == ["What happened?", "Will it happen again?"]
    assert len(store.questions_for({"appeals_questions": "\n".join(f"q{n}" for n in range(20))})) == store.MAX_QUESTIONS


def test_one_open_appeal_at_a_time_and_a_wait_after_a_denial(storage):
    questions = ["Why?", "Anything else?"]
    with pytest.raises(store.AppealError):
        store.submit(storage, 1, 5, "u", questions, ["", "  "])
    appeal_id = store.submit(storage, 1, 5, "u", questions, ["sorry", "x" * 5000, "extra answer is dropped"])
    latest = store.latest_appeal(storage, 1, 5)
    assert [a["answer"] for a in latest["answers"]] == ["sorry", "x" * store.MAX_ANSWER_LENGTH] and latest["status"] == "pending"
    with pytest.raises(store.AppealError, match="waiting"):
        store.submit(storage, 1, 5, "u", questions, ["again"])
    assert store.decide(storage, 2, appeal_id, True, 9) is None                      # another server can't decide it
    assert store.decide(storage, 1, appeal_id, False, 9, " no ")["decision_note"] == "no"
    assert store.decide(storage, 1, appeal_id, True, 9) is None                      # already decided
    denied = store.latest_appeal(storage, 1, 5)
    allowed, why = store.can_appeal(denied)
    assert not allowed and "denied" in why
    later = datetime.now(timezone.utc) + timedelta(days=store.REAPPEAL_DAYS + 1)
    assert store.can_appeal(denied, later) == (True, "")
    assert store.can_appeal(None) == (True, "") and store.can_appeal({"status": "approved"}) == (True, "")


def _world(storage):
    banned, unbanned, dms, posts = {5}, [], [], []

    async def fetch_ban(obj):
        if obj.id not in banned:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "unknown ban")
        return object()

    async def unban(obj, reason=None):
        unbanned.append(obj.id)

    async def post(embed=None):
        posts.append(embed)

    async def dm(embed=None):
        dms.append(embed)

    admin = SimpleNamespace(id=4, guild_permissions=SimpleNamespace(administrator=True), mention="<@4>")
    guild = SimpleNamespace(id=77, name="Guild", icon=None, owner_id=9, fetch_ban=fetch_ban, unban=unban,
                            get_member=lambda uid: admin if uid == 4 else None, get_channel=lambda cid: SimpleNamespace(send=post) if cid == 99 else None)
    embeds = SimpleNamespace(info_embed=lambda title, text: (title, text), success_color=None)
    bot = SimpleNamespace(storage_path=storage, guilds=[guild], voice_clients=[], get_guild=lambda gid: guild if gid == 77 else None,
                          get_cog=lambda name: None, get_user=lambda uid: SimpleNamespace(id=uid, send=dm), embeds=embeds,
                          config=SimpleNamespace(website_url="https://example.com"))
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return cog, unbanned, dms, posts


def test_only_banned_users_of_a_server_taking_appeals_see_the_form(storage):
    cog, _, _, _ = _world(storage)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await client.get("/v1/guilds/77/appeal?user_id=5", headers=AUTH)).status == 404   # appeals are off
            update_guild_settings(storage, 77, {"appeals_enabled": True, "appeals_questions": "What happened?"})
            form = await (await client.get("/v1/guilds/77/appeal?user_id=5", headers=AUTH)).json()
            assert form["guild"]["name"] == "Guild" and form["questions"] == ["What happened?"] and form["can_appeal"] is True
            assert (await client.get("/v1/guilds/77/appeal?user_id=6", headers=AUTH)).status == 404   # not banned: same answer as "no such server"
            assert (await client.get("/v1/guilds/88/appeal?user_id=5", headers=AUTH)).status == 404

    asyncio.run(run())


def test_submit_notifies_mods_and_approval_unbans(storage):
    cog, unbanned, dms, posts = _world(storage)
    update_guild_settings(storage, 77, {"appeals_enabled": True, "mod_log_channel_id": "99"})

    def decide(client, actor, action, appeal_id, note=""):
        return client.post("/v1/guilds/77/moderation/action", headers=AUTH, json={"user_id": str(actor), "action": action, "appeal_id": str(appeal_id), "note": note})

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            sent = await client.post("/v1/guilds/77/appeal", headers=AUTH, json={"user_id": "5", "user_name": "banned#1", "answers": ["I was rude", "", ""]})
            appeal_id = (await sent.json())["appeal_id"]
            assert sent.status == 200 and posts and posts[0][0] == "📨 New Ban Appeal" and "example.com/app/77/moderation" in posts[0][1]
            again = await client.post("/v1/guilds/77/appeal", headers=AUTH, json={"user_id": "5", "answers": ["please"]})
            assert again.status == 409
            assert (await client.post("/v1/guilds/77/appeal", headers=AUTH, json={"user_id": "6", "answers": ["hi"]})).status == 404
            assert (await decide(client, 5, "approve_appeal", appeal_id)).status == 403          # not an admin
            assert (await decide(client, 4, "approve_appeal", "abc")).status == 400
            done = await (await decide(client, 4, "approve_appeal", appeal_id, "welcome back")).json()
            assert done["detail"].startswith("Approved") and unbanned == [5] and "approved" in dms[0][1] and "welcome back" in dms[0][1]
            assert (await decide(client, 4, "deny_appeal", appeal_id)).status == 404             # already decided
            second = (await (await client.post("/v1/guilds/77/appeal", headers=AUTH, json={"user_id": "5", "answers": ["again"]})).json())["appeal_id"]
            assert (await (await decide(client, 4, "deny_appeal", second)).json())["detail"] == "Denied the appeal" and unbanned == [5]

    asyncio.run(run())
    assert store.latest_appeal(storage, 77, 5)["status"] == "denied"


def test_ban_dm_links_the_form_only_when_appeals_are_on(storage):
    from cogs.moderation import Moderation

    cog = Moderation.__new__(Moderation)
    cog.bot = SimpleNamespace(storage_path=storage, config=SimpleNamespace(website_url="https://example.com"))
    guild = SimpleNamespace(id=77)
    assert cog._appeal_note(guild) == ""
    update_guild_settings(storage, 77, {"appeals_enabled": True})
    assert "https://example.com/appeal/77" in cog._appeal_note(guild)
    cog.bot.config.website_url = ""
    assert cog._appeal_note(guild) == ""
