"""Forms through the bridge: staff building and reading, people answering, and what happens in Discord."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import discord
from aiohttp.test_utils import TestClient, TestServer

from cogs.panel_bridge import PanelBridge
from modfakes import Member, Role
from utils import forms
from utils.embeds import EmbedManager
from utils.settings import update_guild_settings

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GUILD = 77
ADMIN, STAFF, ROBIN, SAM, OUTSIDER = 4, 5, 100, 101, 900

EVERYONE = Role(1, "@everyone", 0, default=True)
MEMBER_ROLE = Role(2, "Member", 1)
STAFF_ROLE = Role(3, "Staff", 4, discord.Permissions(kick_members=True, manage_messages=True))
ADMIN_ROLE = Role(4, "Admin", 8, discord.Permissions(administrator=True))
BOT_ROLE = Role(5, "Bot", 9)
ROLES = (EVERYONE, MEMBER_ROLE, STAFF_ROLE, ADMIN_ROLE, BOT_ROLE)


class Channel:
    def __init__(self, cid, name, writers=None):
        self.id, self.name, self.sent, self.threads, self.writers = cid, name, [], [], writers
        self.mention = f"<#{cid}>"

    def permissions_for(self, member):
        can = self.writers is None or member.id in self.writers or getattr(member, "bot", False)
        return discord.Permissions(view_channel=True, send_messages=can, embed_links=True)

    async def send(self, content=None, **kwargs):
        self.sent.append((content, kwargs))
        return NS(jump_url=f"https://discord.test/{self.id}/{len(self.sent)}")

    async def create_thread(self, **kwargs):
        thread = Channel(9000 + len(self.threads), kwargs["name"])
        self.threads.append(thread)
        return thread


def world(storage):
    old = datetime.now(timezone.utc) - timedelta(days=400)

    def person(uid, *roles, bot=False):
        member = Member(uid, f"user{uid}", [EVERYONE, *roles], bot=bot)
        member.joined_at, member.display_name, member.created_at = old, f"User {uid}", old
        return member

    people = {uid: person(uid, *roles) for uid, roles in ((ADMIN, [ADMIN_ROLE]), (STAFF, [STAFF_ROLE]), (ROBIN, [MEMBER_ROLE]), (SAM, []))}
    me = person(999, BOT_ROLE, bot=True)
    channels = {10: Channel(10, "responses"), 11: Channel(11, "announcements", writers={ADMIN}), 12: Channel(12, "tickets")}
    banned = {OUTSIDER}

    async def fetch_ban(target):
        if target.id not in banned:
            raise discord.NotFound(NS(status=404, reason="x"), "Unknown Ban")
        return NS()

    async def unban(target, reason=None):
        banned.discard(target.id)

    guild = NS(id=GUILD, name="Server", owner_id=1, me=me, icon=None, members=[*people.values(), me], roles=list(ROLES),
               text_channels=list(channels.values()), get_member=people.get, get_role=lambda rid: next((r for r in ROLES if r.id == rid), None),
               get_channel=channels.get, fetch_ban=fetch_ban, unban=unban, member_count=5, created_at=None, unavailable=False, voice_client=None)
    for member in people.values():
        member.guild = guild
    outsider = NS(id=OUTSIDER, dms=[])

    async def send(*args, **kwargs):
        outsider.dms.append(kwargs.get("embed"))

    outsider.send = send
    bot = NS(storage_path=storage, guilds=[guild], voice_clients=[], get_guild=lambda gid: guild if gid == GUILD else None, user=NS(id=999, name="bot"),
             get_cog=lambda name: None, get_user=lambda uid: outsider if uid == OUTSIDER else None, config=NS(website_url="https://site.test"))
    bot.embeds = EmbedManager(bot)

    async def fetch_user(uid):
        return outsider

    bot.fetch_user = fetch_user
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return NS(cog=cog, guild=guild, people=people, channels=channels, banned=banned, outsider=outsider, storage=storage)


def run(storage, scenario):
    w = world(storage)
    update_guild_settings(storage, GUILD, {"access_forms_view": "manage_messages", "access_forms_manage": "kick_members"})

    async def go():
        async with TestClient(TestServer(w.cog.build_app())) as client:
            async def get(path, **query):
                response = await client.get(f"/v1/guilds/{GUILD}/{path}", headers=AUTH, params={k: str(v) for k, v in query.items()})
                return response.status, await response.json()

            async def post(path, **body):
                response = await client.post(f"/v1/guilds/{GUILD}/{path}", headers=AUTH, json=body)
                return response.status, await response.json()

            await scenario(w, get, post)

    asyncio.run(go())


def test_staff_build_a_form_and_members_answer_it(storage):
    async def scenario(w, get, post):
        assert (await post("forms", user_id=str(ROBIN), action="create", template="event"))[0] == 403     # not staff
        status, made = await post("forms", user_id=str(STAFF), action="create", template="event", title="Game night")
        form_id = made["form"]["id"]
        assert status == 200 and made["form"]["status"] == "closed" and made["form"]["link"] == f"https://site.test/forms/{GUILD}/game-night"
        _, listed = await get("forms", user_id=STAFF)
        assert listed["staff"] and listed["can_manage"] and [f["kind"] for f in listed["forms"]] == ["appeal", "general"]
        assert {t["key"] for t in listed["templates"]} == set(forms.TEMPLATES) and listed["limits"]["forms"] == 20
        assert (await get("forms", user_id=ROBIN))[1] == {"staff": False, "can_manage": False, "forms": []}   # nothing open yet
        assert (await get(f"forms/{form_id}", user_id=ROBIN))[1]["can_respond"] is False

        _, staff_view = await get(f"forms/{form_id}", user_id=STAFF, staff=1)
        items = staff_view["form"]["items"] + [{"kind": "member", "text": "Bringing anyone?"}, {"kind": "role", "text": "Team"}]
        status, saved = await post("forms", user_id=str(STAFF), action="update", form_id=form_id, items=items, status="open",
                                   options={"channel_id": "10", "editable": True})
        assert status == 200 and saved["form"]["questions"] == 6
        assert (await get(f"forms/{form_id}", user_id=ROBIN, staff=1))[0] == 403
        assert [f["title"] for f in (await get("forms", user_id=ROBIN))[1]["forms"]] == ["Game night"]

        _, page = await get("forms/game-night", user_id=ROBIN)                                               # by the name in its link
        assert page["can_respond"] and page["form"]["title"] == "Game night" and "options" not in page["form"]
        assert [role["name"] for role in page["pickers"]["roles"]] == ["Bot", "Admin", "Staff", "Member"]
        assert (await get(f"forms/{form_id}/lookup", user_id=ROBIN, q="user 10"))[1]["members"][0]["id"] == str(ROBIN)
        assert (await get(f"forms/{form_id}", user_id=OUTSIDER))[0] == 404                                   # not a member: no such form
        ids = [item["id"] for item in page["form"]["items"]]
        status, bad = await post(f"forms/{form_id}/respond", user_id=str(ROBIN), user_name="robin", answers={ids[1]: "Yes"})
        assert status == 409 and "Please answer" in bad["error"]
        answers = {ids[0]: "Robin", ids[1]: "Yes", ids[2]: ["Main event"], ids[4]: str(SAM), ids[5]: "3"}
        status, sent = await post(f"forms/{form_id}/respond", user_id=str(ROBIN), user_name="robin", answers=answers)
        assert status == 200 and sent["notified"] and sent["channel"] == "responses"
        embed = w.channels[10].sent[0][1]["embed"]
        assert "Game night" in embed.title and f"<@{ROBIN}> answered." in embed.description and "user101" in embed.description
        assert (await post(f"forms/{form_id}/respond", user_id=str(ROBIN), user_name="robin", answers=answers))[1]["error"] == "You have already answered this form."
        edit = await post(f"forms/{form_id}/respond", user_id=str(ROBIN), user_name="robin", answers={**answers, ids[0]: "Robin H"}, response_id=sent["response_id"])
        assert edit[1]["edited"] is True and len(w.channels[10].sent) == 1

        assert (await get(f"forms/{form_id}/responses", user_id=ROBIN))[0] == 403
        _, read = await get(f"forms/{form_id}/responses", user_id=STAFF, summary=1)
        assert read["total"] == 1 and read["responses"][0]["user_id"] == str(ROBIN) and read["responses"][0]["answers"][0]["answer"] == "Robin H"
        assert read["summary"][1]["counts"][0] == {"option": "Yes", "n": 1}
        _, mine = await get(f"forms/{form_id}", user_id=ROBIN)
        assert mine["mine"][0]["id"] == sent["response_id"] and mine["can_respond"] is False
        assert (await post(f"forms/{form_id}/withdraw", user_id=str(SAM), response_id=sent["response_id"]))[0] == 409   # not theirs
        assert (await post(f"forms/{form_id}/withdraw", user_id=str(ROBIN), response_id=sent["response_id"]))[0] == 200
        assert (await get(f"forms/{form_id}/responses", user_id=STAFF))[1]["total"] == 0
        assert (await post("forms", user_id=str(STAFF), action="delete", form_id=form_id))[0] == 200
        assert (await get(f"forms/{form_id}", user_id=ROBIN))[0] == 404

    run(storage, scenario)


def test_review_actions_and_anonymous_forms(storage):
    async def scenario(w, get, post):
        _, made = await post("forms", user_id=str(ADMIN), action="create", template="staff")
        form_id = made["form"]["id"]
        actions = [{"on": "submit", "do": "thread", "channel_id": "12", "text": "Thanks {user}."},
                   {"on": "approve", "do": "add_role", "role_id": str(STAFF_ROLE.id)}, {"on": "approve", "do": "post", "channel_id": "11", "text": "Welcome {mention} to the team!"},
                   {"on": "deny", "do": "dm", "text": "Sorry, not this time."}]
        # a staff member cannot have the bot hand out a role at their own rank, or post where they cannot
        assert "at or above your own highest role" in (await post("forms", user_id=str(STAFF), action="update", form_id=form_id, options={"actions": actions[1:2]}))[1]["error"]
        assert "can't post in #announcements" in (await post("forms", user_id=str(STAFF), action="update", form_id=form_id, options={"actions": actions[2:3]}))[1]["error"]
        assert "only be given when a response is approved" in (await post(
            "forms", user_id=str(ADMIN), action="update", form_id=form_id, options={"actions": [{"on": "submit", "do": "add_role", "role_id": str(STAFF_ROLE.id)}]}))[1]["error"]
        assert (await post("forms", user_id=str(ADMIN), action="update", form_id=form_id, status="open", options={"actions": actions, "retry_days": 3}))[0] == 200
        _, page = await get(f"forms/{form_id}", user_id=ROBIN)
        required = {item["id"]: {"short": "Robin", "dropdown": "Helper", "paragraph": "Because", "scale": "4"}[item["kind"]]
                    for item in page["form"]["items"] if item.get("required")}
        ids = {}
        for user in (ROBIN, SAM):
            status, sent = await post(f"forms/{form_id}/respond", user_id=str(user), user_name=f"user{user}", answers=required)
            assert status == 200 and sent["review"] is True
            ids[user] = sent["response_id"]
        assert [thread.name for thread in w.channels[12].threads] == ["Staff application - user100", "Staff application - user101"]
        assert w.channels[12].threads[0].sent[0][0].startswith("Thanks user100.")
        assert (await post(f"forms/{form_id}/decide", user_id=str(ROBIN), action="approve", response_id=ids[ROBIN]))[0] == 403
        status, done = await post(f"forms/{form_id}/decide", user_id=str(STAFF), action="approve", response_id=ids[ROBIN], note="Welcome!")
        assert status == 200 and done["detail"] == "Approved the response, and told them"
        assert STAFF_ROLE in w.people[ROBIN].roles and w.channels[11].sent[0][0] == f"Welcome <@{ROBIN}> to the team!"
        assert "was approved" in w.people[ROBIN].dms[0].description and "Welcome!" in w.people[ROBIN].dms[0].description
        assert (await post(f"forms/{form_id}/decide", user_id=str(STAFF), action="approve", response_id=ids[ROBIN]))[0] == 409   # once only
        await post(f"forms/{form_id}/decide", user_id=str(STAFF), action="deny", response_id=ids[SAM])
        assert [dm.description for dm in w.people[SAM].dms] == ["Sorry, not this time.", "Your response to **Staff application** in **Server** was not accepted."]
        assert STAFF_ROLE not in w.people[SAM].roles
        assert "You can send another after" in (await get(f"forms/{form_id}", user_id=SAM))[1]["reason"]

        _, survey = await post("forms", user_id=str(ADMIN), action="create", template="feedback")
        sid = survey["form"]["id"]
        assert "anonymous form can't open one" in (await post("forms", user_id=str(ADMIN), action="update", form_id=sid, options={"actions": actions[:1]}))[1]["error"]
        await post("forms", user_id=str(ADMIN), action="update", form_id=sid, status="open", options={"channel_id": "10"})
        _, page = await get(f"forms/{sid}", user_id=ROBIN)
        await post(f"forms/{sid}/respond", user_id=str(ROBIN), user_name="robin", answers={page["form"]["items"][0]["id"]: "5"})
        assert "Someone answered." in w.channels[10].sent[-1][1]["embed"].description
        _, read = await get(f"forms/{sid}/responses", user_id=ADMIN, summary=1)
        assert read["responses"][0]["user_id"] is None and read["responses"][0]["user_name"] == "Anonymous" and read["summary"][0]["average"] == 5.0
        test = await post(f"forms/{sid}/respond", user_id=str(STAFF), user_name="staff", answers={page["form"]["items"][0]["id"]: "1"}, test=True)
        _, read = await get(f"forms/{sid}/responses", user_id=ADMIN)
        assert read["total"] == 2 and read["responses"][0]["test"] and read["responses"][0]["user_id"] == str(STAFF)          # a test is never anonymous
        assert (await get(f"forms/{sid}/responses", user_id=ADMIN, all=1))[1]["total"] == 1                                    # and never in the export
        assert (await post(f"forms/{sid}/decide", user_id=str(STAFF), action="remove", response_id=test[1]["response_id"]))[0] == 200

    run(storage, scenario)


def test_who_a_form_is_for(storage):
    async def scenario(w, get, post):
        _, made = await post("forms", user_id=str(ADMIN), action="create", template="suggestion")
        form_id = made["form"]["id"]
        await post("forms", user_id=str(ADMIN), action="update", form_id=form_id, status="open", options={"require_role_id": str(MEMBER_ROLE.id)})
        assert (await get(f"forms/{form_id}", user_id=ROBIN))[1]["can_respond"] is True
        assert (await get(f"forms/{form_id}", user_id=SAM))[1]["reason"] == "This form is for members with the Member role."
        await post("forms", user_id=str(ADMIN), action="update", form_id=form_id, options={"require_role_id": "", "min_member_days": 500})
        assert "at least 500 days" in (await get(f"forms/{form_id}", user_id=ROBIN))[1]["reason"]
        await post("forms", user_id=str(ADMIN), action="update", form_id=form_id, options={"min_member_days": 0, "audience": "anyone"})
        assert (await get(f"forms/{form_id}", user_id=OUTSIDER))[1]["can_respond"] is True                 # anyone signed in, with the link
        assert (await get("forms", user_id=OUTSIDER))[0] == 404                                            # but the server's list is for its members
        preview = await get(f"forms/{form_id}", user_id=ADMIN, preview=1)
        assert preview[1]["preview"] is True and (await get(f"forms/{form_id}", user_id=ROBIN, preview=1))[0] == 403

    run(storage, scenario)
