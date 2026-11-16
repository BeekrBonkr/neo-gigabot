from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from cogs.panel_bridge import PanelBridge
from utils import newsletter as store
from utils import newsletter_sender as sender_module
from utils.newsletter_sender import NewsletterSender, NewsletterView, build_embed, collect_recipients

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}


def _member(uid, *, admin=False, bot=False):
    return SimpleNamespace(id=uid, bot=bot, guild_permissions=SimpleNamespace(administrator=admin))


def _guild(owner_id, members):
    by_id = {m.id: m for m in members}
    return SimpleNamespace(owner_id=owner_id, members=members, get_member=by_id.get)


def _guilds():
    return [
        _guild(1, [_member(1), _member(2, admin=True), _member(3), _member(90, admin=True, bot=True)]),
        _guild(1, [_member(1), _member(4, admin=True)]),  # same owner again: counted once
        _guild(5, [_member(6, admin=True)]),  # owner 5 is not in the member cache
        _guild(91, [_member(91, bot=True), _member(7, admin=True)]),  # owned by a bot
    ]


def _http_error(cls, status, code):
    return cls(SimpleNamespace(status=status, reason="x"), {"code": code, "message": "x"})


# ---------- who receives it ----------

def test_owners_audience_is_deduplicated_and_skips_bots():
    assert collect_recipients(_guilds(), "owners", set())["user_ids"] == [1, 5]


def test_admins_audience_adds_administrators_but_never_bots_or_plain_members():
    assert collect_recipients(_guilds(), "admins", set())["user_ids"] == [1, 2, 4, 5, 6, 7]


def test_opted_out_people_are_left_out_and_counted():
    found = collect_recipients(_guilds(), "admins", {2, 5, 999})
    assert found["user_ids"] == [1, 4, 6, 7] and found["opted_out"] == 2


# ---------- storage ----------

def test_validate_rejects_empty_oversized_and_unknown():
    assert store.validate(" Hi ", " body ", "owners") == ("Hi", "body", "owners")
    for args in (("", "b", "owners"), ("t", "", "owners"), ("t" * 201, "b", "owners"), ("t", "b" * 3501, "owners"), ("t", "b", "everyone")):
        with pytest.raises(store.NewsletterError):
            store.validate(*args)


def test_opt_out_round_trip(storage):
    store.opt_out(storage, 5)
    store.opt_out(storage, 5)
    assert store.opted_out_ids(storage) == {5}
    store.opt_in(storage, 5)
    assert store.opted_out_ids(storage) == set()


def test_send_bookkeeping_and_restart_marks_interrupted(storage):
    send_id = store.create_send(storage, "T", "B", "owners", [1, 2, 3])
    store.mark_delivery(storage, send_id, 1, "sent")
    store.mark_delivery(storage, send_id, 2, "failed", "DMs closed or bot blocked")
    send = store.get_send(storage, send_id)
    assert send["total"] == 3 and send["counts"] == {"pending": 1, "sent": 1, "failed": 1, "skipped": 0}
    assert store.pending_user_ids(storage, send_id) == [3]
    assert store.failure_summary(storage, send_id) == [{"error": "DMs closed or bot blocked", "n": 1}]
    assert store.mark_running_as_interrupted(storage) == 1
    assert store.get_send(storage, send_id)["state"] == "interrupted"
    assert store.mark_running_as_interrupted(storage) == 0


# ---------- delivery ----------

class _FakeBot:
    """Records DMs. `refuse` maps a user ID to the exception their DM raises."""

    def __init__(self, storage, refuse=None, bots=()):
        self.storage_path, self.user = storage, SimpleNamespace(name="TestBot")
        self.sent: list[int] = []
        self.refuse = dict(refuse or {})
        self.bots = set(bots)

    def get_user(self, user_id):
        bot = self

        async def send(embed=None, view=None):
            assert isinstance(view, NewsletterView) and embed.title
            error = bot.refuse.get(user_id)
            if isinstance(error, list):  # a queue of outcomes for retry tests
                error = error.pop(0) if error else None
            if error is not None:
                raise error
            bot.sent.append(user_id)

        return SimpleNamespace(id=user_id, bot=user_id in self.bots, send=send)


@pytest.fixture(autouse=True)
def _no_waiting(monkeypatch):
    monkeypatch.setattr(sender_module, "SECONDS_BETWEEN_DMS", 0)
    monkeypatch.setattr(sender_module, "TOO_FAST_PAUSE_SECONDS", 0)


def _run(sender, send_id):
    async def go():
        sender.start(send_id)
        await sender._task

    asyncio.run(go())


def test_send_delivers_to_everyone_and_records_failures(storage):
    bot = _FakeBot(storage, refuse={2: _http_error(discord.Forbidden, 403, 50007), 3: _http_error(discord.NotFound, 404, 10013)}, bots={4})
    store.opt_out(storage, 5)  # unsubscribed after the recipient list was built
    send_id = store.create_send(storage, "T", "B", "admins", [1, 2, 3, 4, 5, 6])
    _run(NewsletterSender(bot), send_id)
    send = store.get_send(storage, send_id)
    assert bot.sent == [1, 6]
    assert send["state"] == "done" and send["finished_at"]
    assert send["counts"] == {"pending": 0, "sent": 2, "failed": 2, "skipped": 2}


def test_resume_only_messages_people_not_yet_reached(storage):
    bot = _FakeBot(storage)
    send_id = store.create_send(storage, "T", "B", "owners", [1, 2, 3, 4])
    store.mark_delivery(storage, send_id, 1, "sent")
    store.mark_delivery(storage, send_id, 2, "sent")
    store.set_state(storage, send_id, "interrupted")
    _run(NewsletterSender(bot), send_id)
    assert bot.sent == [3, 4]  # 1 and 2 are not messaged a second time
    assert store.get_send(storage, send_id)["state"] == "done"


def test_too_fast_error_pauses_and_retries_the_same_person(storage):
    too_fast = _http_error(discord.HTTPException, 400, 40003)
    bot = _FakeBot(storage, refuse={1: [too_fast, None]})
    send_id = store.create_send(storage, "T", "B", "owners", [1, 2])
    _run(NewsletterSender(bot), send_id)
    assert bot.sent == [1, 2] and store.get_send(storage, send_id)["state"] == "done"


def test_persistent_too_fast_error_stops_and_stays_resumable(storage):
    too_fast = _http_error(discord.HTTPException, 400, 40003)
    bot = _FakeBot(storage, refuse={1: [too_fast] * 10})
    send_id = store.create_send(storage, "T", "B", "owners", [1, 2])
    _run(NewsletterSender(bot), send_id)
    send = store.get_send(storage, send_id)
    assert bot.sent == [] and send["state"] == "interrupted" and send["counts"]["pending"] == 2


def test_only_one_send_runs_at_a_time_and_cancel_stops_it(storage, monkeypatch):
    monkeypatch.setattr(sender_module, "SECONDS_BETWEEN_DMS", 0.05)
    bot = _FakeBot(storage)
    first = store.create_send(storage, "T", "B", "owners", list(range(1, 50)))
    second = store.create_send(storage, "T2", "B", "owners", [100])

    async def go():
        sender = NewsletterSender(bot)
        sender.start(first)
        await asyncio.sleep(0.01)
        with pytest.raises(store.NewsletterError):
            sender.start(second)
        assert sender.cancel() is True
        await sender._task
        assert sender.cancel() is False

    asyncio.run(go())
    send = store.get_send(storage, first)
    assert send["state"] == "cancelled" and 0 < send["counts"]["sent"] < 49 and 100 not in bot.sent


def test_embed_says_why_and_marks_tests():
    assert "own or administer" in build_embed("T", "B", "TestBot").footer.text
    assert build_embed("T", "B", "TestBot", test=True).footer.text.startswith("TEST")


def test_unsubscribe_button_records_the_opt_out(storage):
    replies = []

    async def send_message(content, view=None):
        replies.append(content)

    interaction = SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(send_message=send_message))

    async def go():
        view = NewsletterView(storage)
        await view.unsubscribe.callback(interaction)

    asyncio.run(go())
    assert store.opted_out_ids(storage) == {42} and "won't get" in replies[0]


# ---------- bridge endpoints ----------

def _bridge(storage, bot):
    bot.guilds, bot.config = _guilds(), SimpleNamespace(owner_id=1)
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return cog


def test_overview_reports_audience_sizes_and_history(storage):
    cog = _bridge(storage, _FakeBot(storage))
    store.opt_out(storage, 2)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            assert (await client.get("/v1/newsletter")).status == 401
            return await (await client.get("/v1/newsletter", headers=AUTH)).json()

    body = asyncio.run(run())
    assert body["audiences"]["owners"]["recipients"] == 2
    assert body["audiences"]["admins"] == {"recipients": 5, "opted_out": 1, "guilds": 4}
    assert body["opted_out_total"] == 1 and body["sends"] == [] and body["running_id"] is None


def test_test_send_goes_to_the_owner_only(storage):
    bot = _FakeBot(storage)
    cog = _bridge(storage, bot)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            bad = await client.post("/v1/newsletter/test", headers=AUTH, json={"title": "", "body": "x"})
            ok = await client.post("/v1/newsletter/test", headers=AUTH, json={"title": "Hi", "body": "x"})
            return bad.status, ok.status

    assert asyncio.run(run()) == (400, 200)
    assert bot.sent == [1] and store.list_sends(storage) == []


def test_send_endpoint_starts_a_send_and_guards_the_confirmed_count(storage):
    bot = _FakeBot(storage)
    cog = _bridge(storage, bot)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            grew = await client.post("/v1/newsletter/send", headers=AUTH, json={"title": "T", "body": "B", "audience": "admins", "expected": 0})
            assert grew.status == 409 and "grew" in (await grew.json())["error"]
            bad = await client.post("/v1/newsletter/send", headers=AUTH, json={"title": "T", "body": "B", "audience": "nope"})
            assert bad.status == 400
            ok = await client.post("/v1/newsletter/send", headers=AUTH, json={"title": "T", "body": "B", "audience": "owners", "expected": 2})
            assert ok.status == 200 and (await ok.json())["recipients"] == 2
            await cog.newsletter._task
            again = await client.post(f"/v1/newsletter/{(await ok.json())['id']}/resume", headers=AUTH)
            assert again.status == 409  # nothing left to send
            assert (await client.post("/v1/newsletter/cancel", headers=AUTH)).status == 409
            assert (await client.post("/v1/newsletter/999/resume", headers=AUTH)).status == 404

    asyncio.run(run())
    assert bot.sent == [1, 5]
    assert store.list_sends(storage)[0]["state"] == "done"


def test_refused_test_dm_explains_the_privacy_setting(storage):
    bot = _FakeBot(storage, refuse={1: _http_error(discord.Forbidden, 403, 50278)})
    cog = _bridge(storage, bot)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            response = await client.post("/v1/newsletter/test", headers=AUTH, json={"title": "Hi", "body": "x"})
            return response.status, (await response.json())["error"]

    status, message = asyncio.run(run())
    assert status == 409 and "50278" in message and "Privacy Settings" in message
