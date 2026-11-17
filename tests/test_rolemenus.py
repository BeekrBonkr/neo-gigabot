from __future__ import annotations

import asyncio
from types import SimpleNamespace

import discord
import pytest

from cogs.rolemenus import BUTTON_PREFIX, SELECT_ID, RoleMenus, build_view
from utils import rolemenus as store

MENU = [1, 2, 3]


def test_role_mentions_are_parsed_in_order_without_repeats():
    assert store.parse_role_ids("<@&5> and <@&7>, <@&5> <@123> @everyone") == [5, 7]
    assert store.parse_role_ids("") == []


@pytest.mark.parametrize("held, pressed, mode, expected", [
    (set(), 1, "multiple", ([1], [])),          # press: get the role
    ({1}, 1, "multiple", ([], [1])),            # press again: lose it
    ({1}, 2, "multiple", ([2], [])),            # several at once is fine
    ({1}, 2, "single", ([2], [1])),             # single: switching drops the old one
    ({1, 99}, 2, "single", ([2], [1])),         # roles outside the menu are never touched
    ({1}, 77, "multiple", ([], [])),            # a role that isn't in the menu does nothing
])
def test_button_presses(held, pressed, mode, expected):
    assert store.plan_changes(MENU, held, [pressed], mode=mode, toggle=True) == expected


@pytest.mark.parametrize("held, chosen, mode, expected", [
    (set(), [1, 3], "multiple", ([1, 3], [])),
    ({1, 2}, [2, 3], "multiple", ([3], [1])),   # the selection replaces what they had from this menu
    ({1, 2, 99}, [], "multiple", ([], [1, 2])), # clearing the dropdown removes them all, and only those
    ({1}, [2, 3], "single", ([2], [1])),        # single mode keeps one even if two are sent
    ({2}, [2, 77], "multiple", ([], [])),
])
def test_dropdown_selections(held, chosen, mode, expected):
    assert store.plan_changes(MENU, held, chosen, mode=mode, toggle=False) == expected


def test_menus_are_stored_per_server(storage):
    menu_id = store.add_menu(storage, 1, 7, 900, "Colors", "buttons", "single", [1, 2], 9)
    assert store.get_by_message(storage, 900)["role_ids"] == [1, 2] and store.count_menus(storage, 1) == 1
    assert store.delete_menu(storage, 2, menu_id) is None
    assert store.delete_menu(storage, 1, menu_id)["title"] == "Colors" and store.get_by_message(storage, 900) is None


class Role:
    def __init__(self, role_id, position=1, managed=False, **perms):
        self.id, self.position, self.managed, self.name, self.mention = role_id, position, managed, f"role{role_id}", f"<@&{role_id}>"
        self.permissions = SimpleNamespace(**{name: perms.get(name, False) for name in store.ELEVATED})

    def is_default(self):
        return False

    def __ge__(self, other):
        return self.position >= other.position

    def __lt__(self, other):
        return self.position < other.position


def test_unsafe_roles_are_refused_when_a_menu_is_created(storage):
    roles = {1: Role(1), 2: Role(2, managed=True), 3: Role(3, ban_members=True), 4: Role(4, position=50), 5: Role(5, position=8)}
    guild = SimpleNamespace(owner_id=999, get_role=roles.get, me=SimpleNamespace(top_role=Role(90, position=20)))
    invoker = SimpleNamespace(id=10, top_role=Role(91, position=5))
    cog = RoleMenus(SimpleNamespace(storage_path=storage))
    assert [r.id for r in cog._check_roles(guild, invoker, [1])] == [1]
    for role_ids, expected in (([], "Mention the roles"), ([404], "doesn't exist"), ([2], "managed by an integration"), ([3], "moderation or admin"),
                               ([4], "above my highest role"), ([5], "not below your own"), (list(range(100, 125)), "up to 20")):
        with pytest.raises(store.RoleMenuError, match=expected):
            cog._check_roles(guild, invoker, role_ids)


def test_views_carry_everything_needed_to_handle_a_press_after_a_restart():
    roles = [Role(1), Role(2)]
    buttons = build_view(roles, "buttons", "multiple").children
    assert [b.custom_id for b in buttons] == [f"{BUTTON_PREFIX}1", f"{BUTTON_PREFIX}2"]
    select = build_view(roles, "dropdown", "single").children[0]
    assert select.custom_id == SELECT_ID and select.max_values == 1 and select.min_values == 0 and [o.value for o in select.options] == ["1", "2"]
    assert build_view(roles, "dropdown", "multiple").children[0].max_values == 2


def test_pressing_changes_only_roles_the_bot_can_manage(storage, monkeypatch):
    monkeypatch.setattr(discord, "Member", SimpleNamespace)
    store.add_menu(storage, 1, 7, 900, "Colors", "buttons", "single", [1, 2, 3], 9)
    roles = {1: Role(1), 2: Role(2), 3: Role(3, position=99)}
    added, removed, replies = [], [], []

    async def add_roles(*given, reason=None):
        added.extend(r.id for r in given)

    async def remove_roles(*given, reason=None):
        removed.extend(r.id for r in given)

    async def send_message(text, **_):
        replies.append(text)

    guild = SimpleNamespace(id=1, get_role=roles.get, me=SimpleNamespace(top_role=Role(90, position=20)))
    member = SimpleNamespace(roles=[roles[1]], add_roles=add_roles, remove_roles=remove_roles)

    def press(custom_id, message_id=900, values=()):
        return SimpleNamespace(guild=guild, user=member, message=SimpleNamespace(id=message_id), data={"custom_id": custom_id, "values": list(values)},
                               response=SimpleNamespace(send_message=send_message))

    cog = RoleMenus(SimpleNamespace(storage_path=storage))
    asyncio.run(cog.handle_press(press(f"{BUTTON_PREFIX}2"), f"{BUTTON_PREFIX}2"))
    assert added == [2] and removed == [1] and "Added <@&2>" in replies[-1]
    asyncio.run(cog.handle_press(press(f"{BUTTON_PREFIX}3"), f"{BUTTON_PREFIX}3"))
    assert added == [2] and "couldn't be changed" in replies[-1]              # role 3 is above the bot
    asyncio.run(cog.handle_press(press(f"{BUTTON_PREFIX}2", message_id=555), f"{BUTTON_PREFIX}2"))
    assert "no longer active" in replies[-1] and added == [2]                  # a deleted menu's old message does nothing
