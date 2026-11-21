"""Who a moderator may act on, and which roles they may have the bot hand out."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import discord
import pytest
from discord import app_commands

import cogs.moderation as moderation
from cogs.moderation import Moderation
from utils.checks import may_manage_channel
from utils.settings import get_guild_settings, update_guild_settings

from modfakes import Member, Role, make_bot, make_guild, make_interaction, said

EVERYONE = Role(1, "@everyone", 0, default=True)
HELPER = Role(2, "Helper", 2, discord.Permissions(manage_roles=True, kick_members=True))
MEMBER_ROLE = Role(5, "Member", 1)
ADMIN = Role(3, "Admin", 5, discord.Permissions(administrator=True))
BOT_ROLE = Role(4, "Bot", 9)
BOOSTER = Role(6, "Booster", 1, managed=True)
ROLES = (EVERYONE, HELPER, MEMBER_ROLE, ADMIN, BOT_ROLE, BOOSTER)


@pytest.fixture()
def server(storage, monkeypatch):
    monkeypatch.setattr(moderation, "MASS_ACTION_DELAY_SECONDS", 0, raising=False)
    helper = Member(10, "helper", [EVERYONE, HELPER])
    admin = Member(11, "admin", [EVERYONE, ADMIN])
    plain = Member(12, "plain", [EVERYONE])
    me = Member(999, "bot", [EVERYONE, BOT_ROLE], bot=True)
    guild = make_guild(helper, admin, plain, me, roles=ROLES)
    return NS(cog=Moderation(make_bot(storage)), guild=guild, helper=helper, admin=admin, plain=plain, storage=storage)


def choice(value):
    return app_commands.Choice(name=value, value=value)


def test_role_refuses_a_role_at_or_above_the_moderators_own(server):
    interaction = make_interaction(server.guild, server.helper)
    asyncio.run(Moderation.role.callback(server.cog, interaction, choice("add"), ADMIN))
    assert "above (or equal to) your highest role" in said(interaction)
    assert ADMIN not in server.helper.roles and ADMIN not in server.plain.roles

    interaction = make_interaction(server.guild, server.helper)
    asyncio.run(Moderation.role.callback(server.cog, interaction, choice("remove"), ADMIN))
    assert ADMIN in server.admin.roles  # nor may they strip it from the admins

    for role in (BOOSTER, EVERYONE):
        interaction = make_interaction(server.guild, server.helper)
        asyncio.run(Moderation.role.callback(server.cog, interaction, choice("add"), role))
        assert "can't be given or removed by a bot" in said(interaction)


def test_role_still_works_for_roles_below_the_moderator(server):
    interaction = make_interaction(server.guild, server.helper)
    asyncio.run(Moderation.role.callback(server.cog, interaction, choice("add"), MEMBER_ROLE))
    assert MEMBER_ROLE in server.plain.roles and MEMBER_ROLE in server.admin.roles


def test_the_server_owner_is_only_held_to_the_bots_rank(server):
    server.guild.owner_id = server.helper.id
    assert server.cog._role_problem(make_interaction(server.guild, server.helper), ADMIN) is None
    assert "my highest role" in server.cog._role_problem(make_interaction(server.guild, server.helper), BOT_ROLE)


def test_autoroles_refuse_roles_above_the_moderator_and_roles_with_moderation_permissions(server):
    for user, role, expected in ((server.helper, ADMIN, "your highest role"), (server.admin, HELPER, "moderation permissions")):
        interaction = make_interaction(server.guild, user)
        asyncio.run(Moderation.autorole.callback(server.cog, interaction, choice("add"), role))
        assert expected in said(interaction)
    assert get_guild_settings(server.storage, server.guild.id)["autoroles"] == []

    interaction = make_interaction(server.guild, server.helper)
    asyncio.run(Moderation.autorole.callback(server.cog, interaction, choice("add"), MEMBER_ROLE))
    assert get_guild_settings(server.storage, server.guild.id)["autoroles"] == [MEMBER_ROLE.id]


def test_an_admin_role_saved_as_an_autorole_some_other_way_is_not_given_on_join(server):
    update_guild_settings(server.storage, server.guild.id, {"autoroles": [ADMIN.id, MEMBER_ROLE.id, BOOSTER.id]})
    newcomer = Member(13, "newcomer", [EVERYONE], guild=server.guild)
    asyncio.run(server.cog.on_member_join(newcomer))
    assert MEMBER_ROLE in newcomer.roles and ADMIN not in newcomer.roles and BOOSTER not in newcomer.roles


def test_prune_leaves_alone_anyone_the_moderator_could_not_kick(server, monkeypatch):
    class Yes:
        value, message = True, None

        def __init__(self, **kwargs): pass

        async def wait(self): pass

    monkeypatch.setattr(moderation, "ConfirmView", Yes)
    interaction = make_interaction(server.guild, server.helper)
    asyncio.run(Moderation.prune.callback(server.cog, interaction, choice("specific"), ADMIN))
    assert not server.admin.kicked and "at or above your highest role" in said(interaction)

    server.plain.roles.append(MEMBER_ROLE)
    server.admin.roles.append(MEMBER_ROLE)
    interaction = make_interaction(server.guild, server.helper)
    asyncio.run(Moderation.prune.callback(server.cog, interaction, choice("specific"), MEMBER_ROLE))
    assert server.plain.kicked and not server.admin.kicked
    assert "1 more matched" in said(interaction)


def test_a_permission_on_one_channel_does_not_pass_a_server_wide_check(server):
    for command in (Moderation.role, Moderation.autorole):
        (check,) = [each for each in command.checks if each.__module__ == "utils.checks"]
        with pytest.raises(app_commands.MissingPermissions) as caught:
            check(NS(user=server.plain, permissions=discord.Permissions(manage_roles=True)))  # the overwrite Discord reports for this channel
        assert caught.value.missing_permissions == ["manage_roles"]
        assert check(NS(user=server.helper, permissions=discord.Permissions.none())) is True


def test_lock_and_slowmode_ask_about_the_channel_being_changed(server):
    edits = []

    class Other(discord.TextChannel):  # passes isinstance without Discord
        def __init__(self): pass
        id, mention, slowmode_delay = 8, "<#8>", 0

        def permissions_for(self, member):
            return discord.Permissions(manage_channels=member is server.admin)

        async def edit(self, **kwargs):
            edits.append(kwargs)

        def overwrites_for(self, role):
            return discord.PermissionOverwrite()

        async def set_permissions(self, role, **kwargs):
            edits.append(kwargs)

    other = Other()
    assert not may_manage_channel(NS(user=server.helper), other)
    for callback, args in ((Moderation.slowmode.callback, ("10s", other)), (Moderation.lock.callback, (other,)), (Moderation.unlock.callback, (other,))):
        interaction = make_interaction(server.guild, server.helper)
        asyncio.run(callback(server.cog, interaction, *args))
        assert "You need Manage Channel in <#8>" in said(interaction)
    assert edits == []
    server.guild.default_role = EVERYONE
    asyncio.run(Moderation.slowmode.callback(server.cog, make_interaction(server.guild, server.admin), "10s", other))
    asyncio.run(Moderation.lock.callback(server.cog, make_interaction(server.guild, server.admin), other))
    assert len(edits) == 2
