"""What the global error handler tells people."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from discord import app_commands

import main


def handle(error, monkeypatch, tmp_path):
    said = []

    async def error_interaction(interaction, title, description, **kwargs):
        said.append((title, description, kwargs.get("ephemeral")))

    async def record(*args, **kwargs):
        said.append(("LOG", kwargs.get("status"), None))

    monkeypatch.setattr(main, "record_command", record)
    bot = SimpleNamespace(embeds=SimpleNamespace(error_interaction=error_interaction), storage_path=tmp_path, logger=SimpleNamespace(exception=lambda *a, **k: None))
    handler = next(getattr(main.GigaBot, name) for name in dir(main.GigaBot) if "error" in name.lower() and asyncio.iscoroutinefunction(getattr(main.GigaBot, name)))
    asyncio.run(handler(bot, SimpleNamespace(command=None), error))
    return said


def test_a_missing_bot_permission_is_not_blamed_on_the_person(monkeypatch, tmp_path):
    said = handle(app_commands.BotMissingPermissions(["ban_members"]), monkeypatch, tmp_path)
    assert ("LOG", "BOT_MISSING_PERMISSIONS", None) in said
    title, text, ephemeral = said[-1]
    assert title == "I Need More Permissions" and "**Ban Members**" in text and "my role" in text and ephemeral


def test_permissions_are_named_the_way_discord_names_them(monkeypatch, tmp_path):
    title, text, _ = handle(app_commands.MissingPermissions(["moderate_members", "manage_guild"]), monkeypatch, tmp_path)[-1]
    assert title == "Missing Permissions" and "**Timeout Members** and **Manage Server**" in text
    assert main.permission_names(["kick_members"]) == "**Kick Members**"


def test_server_only_commands_say_so(monkeypatch, tmp_path):
    title, text, _ = handle(app_commands.NoPrivateMessage(), monkeypatch, tmp_path)[-1]
    assert title == "Server Only"
