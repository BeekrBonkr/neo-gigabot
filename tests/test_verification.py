from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import discord

from cogs.verification import Verification, verification_problem
from utils.settings import update_guild_settings


class Role:
    def __init__(self, role_id, position, managed=False):
        self.id, self.position, self.managed, self.name = role_id, position, managed, f"role{role_id}"

    def is_default(self):
        return self.position == 0

    def __ge__(self, other):
        return self.position >= other.position


def test_account_age_gate():
    now = datetime(2026, 1, 10, tzinfo=timezone.utc)
    assert verification_problem({"verify_min_account_age_days": 0}, now, now) is None
    assert verification_problem({"verify_min_account_age_days": 3}, now - timedelta(days=5), now) is None
    assert "at least 3 day" in verification_problem({"verify_min_account_age_days": 3}, now - timedelta(days=1), now)


def _press(storage, monkeypatch, *, age_days=100, roles=(), manage_roles=True):
    monkeypatch.setattr(discord, "Member", SimpleNamespace)
    verified, top = Role(50, 5), Role(90, 9)
    replies, granted = [], []

    async def add_roles(role, reason=None):
        granted.append(role.id)

    async def send_message(text, ephemeral=False):
        replies.append(text)

    guild = SimpleNamespace(id=1, name="G", get_role={50: verified, 60: Role(60, 20), 70: Role(70, 3, managed=True)}.get,
                            me=SimpleNamespace(top_role=top, guild_permissions=SimpleNamespace(manage_roles=manage_roles)))
    member = SimpleNamespace(id=5, roles=[verified if r == "verified" else r for r in roles], add_roles=add_roles, created_at=datetime.now(timezone.utc) - timedelta(days=age_days))
    interaction = SimpleNamespace(guild=guild, user=member, response=SimpleNamespace(send_message=send_message))
    cog = Verification(SimpleNamespace(storage_path=storage))
    asyncio.run(cog.handle_verify(interaction))
    return replies[0], granted, verified


def test_pressing_the_button_grants_the_role_once(storage, monkeypatch):
    update_guild_settings(storage, 1, {"verify_role_id": "50"})
    reply, granted, verified = _press(storage, monkeypatch)
    assert "You're verified" in reply and granted == [50]
    reply, granted, _ = _press(storage, monkeypatch, roles=("verified",))
    assert "already verified" in reply and granted == []
    update_guild_settings(storage, 1, {"verify_min_account_age_days": 30})
    reply, granted, _ = _press(storage, monkeypatch, age_days=2)
    assert "at least 30 day" in reply and granted == []


def test_a_broken_setup_explains_itself_instead_of_failing(storage, monkeypatch):
    reply, granted, _ = _press(storage, monkeypatch)
    assert "isn't set up yet" in reply and granted == []
    for role_id, expected in (("60", "below my highest role"), ("70", "can't be given out"), ("999", "isn't set up yet")):
        update_guild_settings(storage, 1, {"verify_role_id": role_id})
        reply, granted, _ = _press(storage, monkeypatch)
        assert expected in reply and granted == []
    update_guild_settings(storage, 1, {"verify_role_id": "50"})
    reply, granted, _ = _press(storage, monkeypatch, manage_roles=False)
    assert "Manage Roles" in reply and granted == []
