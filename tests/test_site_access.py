"""Which Discord permission opens which part of a server's pages on the website."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import discord
from aiohttp.test_utils import TestClient, TestServer

from cogs.panel_bridge import PanelBridge
from utils import site_access
from utils.settings import DEFAULT_GUILD_SETTINGS, get_guild_settings, update_guild_settings

TOKEN = "t" * 40
AUTH = {"Authorization": f"Bearer {TOKEN}"}
GUILD = 77
OWNER, ADMIN, MOD, HELPER, MEMBER = 1, 4, 5, 6, 7


def test_every_area_needs_administrator_until_an_admin_chooses_otherwise():
    assert all(DEFAULT_GUILD_SETTINGS[key] == "administrator" for key in site_access.KEYS) and len(site_access.KEYS) == 11
    moderator = discord.Permissions(moderate_members=True, ban_members=True, manage_guild=True)
    locked = site_access.access_for(moderator, dict(DEFAULT_GUILD_SETTINGS))
    assert locked == site_access.no_access()
    assert site_access.access_for(discord.Permissions(administrator=True), {}) == site_access.full_access()
    assert site_access.access_for(discord.Permissions.none(), {}, is_owner=True) == site_access.full_access()


def test_an_area_opens_to_whoever_holds_the_permission_chosen_for_it():
    settings = {**DEFAULT_GUILD_SETTINGS, "access_moderation_view": "moderate_members", "access_moderation_manage": "ban_members",
                "access_logs_view": "view_audit_log", "access_commands_manage": "manage_guild"}
    helper = site_access.access_for(discord.Permissions(moderate_members=True), settings)
    assert helper["moderation"] == {"view": True, "manage": False} and helper["logs"]["view"] is False
    moderator = site_access.access_for(discord.Permissions(ban_members=True, view_audit_log=True), settings)
    assert moderator["moderation"] == {"view": True, "manage": True}   # managing an area includes seeing it
    assert moderator["logs"]["view"] is True and moderator["modules"] == {"view": False, "manage": False}
    assert site_access.access_for(discord.Permissions(manage_guild=True), settings)["commands"] == {"view": True, "manage": True}
    # a value that is not one of the choices is read as Administrator, never as "anyone"
    assert site_access.access_for(discord.Permissions.all_channel(), {**settings, "access_logs_view": "send_messages"})["logs"]["view"] is False


def _world(storage):
    def person(uid, **permissions):
        return NS(id=uid, display_name=f"m{uid}", display_avatar=None, mention=f"<@{uid}>", roles=[], top_role=NS(position=1),
                  guild_permissions=discord.Permissions(**permissions), is_timed_out=lambda: False, timed_out_until=None, joined_at=None)

    people = {
        OWNER: person(OWNER), ADMIN: person(ADMIN, administrator=True), MOD: person(MOD, ban_members=True, moderate_members=True),
        HELPER: person(HELPER, moderate_members=True, manage_guild=True), MEMBER: person(MEMBER),
    }
    guild = NS(id=GUILD, name="Server", owner_id=OWNER, get_member=people.get, members=list(people.values()), icon=None, member_count=5,
               me=NS(guild_permissions=discord.Permissions.all(), joined_at=None, top_role=NS(position=9)), channels=[], roles=[], text_channels=[],
               get_channel=lambda cid: None, get_role=lambda rid: None, voice_client=None, premium_tier=0, banner=None, description=None, created_at=None, unavailable=False)
    bot = NS(storage_path=storage, guilds=[guild], voice_clients=[], get_guild=lambda gid: guild if gid == GUILD else None, user=NS(id=999), get_cog=lambda name: None)
    cog = PanelBridge(bot)
    cog._token = TOKEN
    return cog


def test_the_bot_itself_refuses_what_the_server_has_not_opened(storage):
    cog = _world(storage)

    async def run():
        async with TestClient(TestServer(cog.build_app())) as client:
            async def standing(uid):
                return (await (await client.get(f"/v1/guilds/{GUILD}/members/{uid}", headers=AUTH)).json())

            async def moderation(uid):
                return (await client.get(f"/v1/guilds/{GUILD}/moderation?user_id={uid}", headers=AUTH)).status

            async def save(uid, **changes):
                current = get_guild_settings(storage, GUILD)
                return (await client.put(f"/v1/guilds/{GUILD}/settings", headers=AUTH, json={"settings": {**current, **changes}, "user_id": str(uid)})).status

            assert (await standing(MOD))["access"]["moderation"] == {"view": False, "manage": False}  # locked to admins at first
            assert (await standing(ADMIN))["access"]["modules"] == {"view": True, "manage": True}
            assert (await standing(OWNER))["access"]["logs"]["view"] is True
            assert [await moderation(uid) for uid in (ADMIN, MOD, HELPER, MEMBER)] == [200, 403, 403, 403]

            # only an administrator can change who may open what, whatever else they were given
            update_guild_settings(storage, GUILD, {"access_modules_manage": "manage_guild", "access_modules_view": "manage_guild"})
            assert await save(HELPER, access_moderation_view="moderate_members") == 403
            assert get_guild_settings(storage, GUILD)["access_moderation_view"] == "administrator"
            assert await save(HELPER, listen_to_bots=True) == 200          # the Modules area is theirs to manage now
            assert await save(MOD, listen_to_bots=False) == 403            # and not this person's
            assert await save(MEMBER, blocked_commands=["ping"]) == 403
            assert await save(ADMIN, access_moderation_view="moderate_members", access_moderation_manage="ban_members") == 200
            assert await save(ADMIN, access_logs_view="send_messages") == 400   # not one of the permissions offered

            assert [await moderation(uid) for uid in (ADMIN, MOD, HELPER, MEMBER)] == [200, 200, 200, 403]
            act = lambda uid: client.post(f"/v1/guilds/{GUILD}/moderation/action", headers=AUTH, json={"user_id": str(uid), "action": "nonsense"})  # noqa: E731
            assert (await act(HELPER)).status == 403                        # may look, may not act
            assert (await act(MOD)).status != 403                           # gets as far as the action itself
            assert (await standing(HELPER))["access"]["moderation"] == {"view": True, "manage": False}

            # content tools were not opened to anyone: still administrators only
            tags = lambda uid: client.post(f"/v1/guilds/{GUILD}/tags", headers=AUTH, json={"user_id": str(uid), "action": "x"})  # noqa: E731
            assert (await tags(MOD)).status == 403 and (await tags(HELPER)).status == 403

    asyncio.run(run())
