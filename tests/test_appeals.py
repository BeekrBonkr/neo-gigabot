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
    texts = lambda settings: [q["text"] for q in store.questions_for(settings)]  # noqa: E731
    assert texts({}) == list(store.DEFAULT_QUESTIONS)
    assert texts({"appeals_questions": " What happened? \n\n  Will it happen again?\n"}) == ["What happened?", "Will it happen again?"]
    assert len(texts({"appeals_questions": "\n".join(f"q{n}" for n in range(20))})) == store.MAX_QUESTIONS
    assert [q["required"] for q in store.questions_for({})] == [True, False, False]
    # A built form wins over the older text setting.
    form = [{"text": "Which rule?", "type": "choice", "options": ["Spam", "Other"], "required": True}]
    assert store.questions_for({"appeals_form": form, "appeals_questions": "ignored"}) == form


def test_built_forms_are_checked():
    good = store.clean_form([
        {"text": " Why? ", "type": "paragraph"},
        {"text": "Which rule?", "type": "choice", "options": ["Spam", " Spam ", "Other", ""], "required": 1},
        {"text": "Tick all that apply", "type": "checkboxes", "options": ["a", "b", "c"]},
        {"text": "Your old name", "type": "short", "options": ["dropped"]},
    ])
    assert good[0] == {"text": "Why?", "type": "paragraph", "options": [], "required": False}
    assert good[1]["options"] == ["Spam", "Other"] and good[1]["required"] is True and good[3]["options"] == []
    for bad in ("nope", [{"text": ""}], [{"text": "x", "type": "slider"}], [{"text": "x", "type": "choice", "options": ["only one"]}],
                [{"text": "x"}] * (store.MAX_QUESTIONS + 1), [{"text": "x" * 151}], ["text"]):
        with pytest.raises(store.AppealError):
            store.clean_form(bad)


def test_answers_fit_their_question_and_required_ones_are_enforced(storage):
    questions = store.clean_form([
        {"text": "Why?", "type": "paragraph", "required": True},
        {"text": "Which rule?", "type": "choice", "options": ["Spam", "Other"]},
        {"text": "Tick", "type": "checkboxes", "options": ["a", "b", "c"]},
        {"text": "Name", "type": "short"},
    ])
    with pytest.raises(store.AppealError, match="Why"):
        store.submit(storage, 1, 5, "u", questions, ["", "Spam"])
    store.submit(storage, 1, 5, "u", questions, ["sorry", "Made up", ["c", "a", "zzz"], "n" * 500])
    assert [a["answer"] for a in store.latest_appeal(storage, 1, 5)["answers"]] == ["sorry", "", "a, c", "n" * store.MAX_SHORT_ANSWER_LENGTH]


def test_test_appeals_never_count_and_only_they_can_be_removed(storage):
    real = store.submit(storage, 1, 5, "u", ["Why?"], ["sorry"])
    test = store.submit(storage, 1, 5, "u", ["Why?"], ["trying it out"], test=True)   # no "one at a time" for tests
    assert store.latest_appeal(storage, 1, 5)["id"] == real
    assert store.decide(storage, 1, test, True, 9) is None                            # a test can't be approved
    assert store.remove_test(storage, 1, real) is None and store.remove_test(storage, 2, test) is None
    assert store.remove_test(storage, 1, test)["answers"][0]["answer"] == "trying it out"


def test_the_wait_after_a_denial_is_the_servers_choice():
    denied = {"status": "denied", "decided_at": "2026-03-01 12:00:00", "created_at": "2026-03-01 11:00:00"}
    now = datetime(2026, 3, 3, tzinfo=timezone.utc)
    assert store.can_appeal(denied, now)[0] is False and store.can_appeal(denied, now, wait_days=1) == (True, "")
    assert store.can_appeal(denied, now, wait_days=0) == (True, "")
    assert store.reappeal_days({"appeals_reappeal_days": 30}) == 30 and store.reappeal_days({}) == store.REAPPEAL_DAYS


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
            assert form["guild"]["name"] == "Guild" and [q["text"] for q in form["questions"]] == ["What happened?"] and form["can_appeal"] is True
            assert form["preview"] is False and form["intro"] == ""
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


def test_admins_can_preview_the_form_and_send_a_test(storage):
    cog, unbanned, _, posts = _world(storage)
    update_guild_settings(storage, 77, {"appeals_channel_id": "99", "appeals_intro": "Be honest.", "mod_log_channel_id": "12"})

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            # Appeals are still off and the admin isn't banned: the preview works anyway, for admins only.
            form = await (await client.get("/v1/guilds/77/appeal?user_id=4&preview=1", headers=AUTH)).json()
            assert form["preview"] is True and form["intro"] == "Be honest." and form["can_appeal"] is True
            assert (await client.get("/v1/guilds/77/appeal?user_id=5&preview=1", headers=AUTH)).status == 403
            assert (await client.post("/v1/guilds/77/appeal", headers=AUTH, json={"user_id": "5", "test": True, "answers": ["x"]})).status == 403
            sent = await (await client.post("/v1/guilds/77/appeal", headers=AUTH, json={"user_id": "4", "test": True, "answers": ["trying"]})).json()
            assert sent["notified"] is True and posts[0][0] == "🧪 Test Ban Appeal"
            gone = await client.post("/v1/guilds/77/moderation/action", headers=AUTH, json={"user_id": "4", "action": "remove_appeal", "appeal_id": str(sent["appeal_id"])})
            assert gone.status == 200 and (await gone.json())["detail"] == "Removed the test appeal"
            again = await client.post("/v1/guilds/77/moderation/action", headers=AUTH, json={"user_id": "4", "action": "remove_appeal", "appeal_id": str(sent["appeal_id"])})
            assert again.status == 404

    asyncio.run(run())
    assert unbanned == []


def test_the_appeal_form_setting_is_validated(storage):
    from cogs.panel_bridge import SettingsValidationError, validate_settings
    from utils.settings import DEFAULT_GUILD_SETTINGS

    good = dict(DEFAULT_GUILD_SETTINGS, appeals_form=[{"text": "Why?", "type": "choice", "options": ["a", "b"]}])
    assert validate_settings(good, DEFAULT_GUILD_SETTINGS)["appeals_form"][0]["required"] is False
    with pytest.raises(SettingsValidationError, match="Appeal form"):
        validate_settings(dict(DEFAULT_GUILD_SETTINGS, appeals_form=[{"text": "Why?", "type": "choice"}]), DEFAULT_GUILD_SETTINGS)


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
