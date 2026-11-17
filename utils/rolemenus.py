"""Role menus: a message with buttons or a dropdown that members use to give
themselves roles. The newer alternative to emoji reaction roles."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from utils.db import connect

MAX_MENUS_PER_GUILD = 25
MAX_ROLES_PER_MENU = 20
STYLES = ("buttons", "dropdown")
MODES = ("multiple", "single")
ROLE_MENTION = re.compile(r"<@&(\d+)>")
# Permissions a self-assignable role must not carry.
ELEVATED = ("administrator", "manage_guild", "manage_roles", "manage_channels", "manage_messages", "manage_webhooks",
            "kick_members", "ban_members", "moderate_members", "mention_everyone", "manage_nicknames", "view_audit_log")

CREATE_SQL = """
CREATE TABLE IF NOT EXISTS role_menus (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL UNIQUE,
    title TEXT NOT NULL,
    style TEXT NOT NULL,
    mode TEXT NOT NULL,
    role_ids TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
"""


class RoleMenuError(ValueError):
    """Raised with a message that is safe to show to the admin."""


def ensure_table(storage_path: Path) -> None:
    with connect(storage_path) as connection:
        connection.executescript(CREATE_SQL)


def _row(row) -> dict[str, Any]:
    record = dict(row)
    record["role_ids"] = [int(r) for r in json.loads(record["role_ids"])]
    return record


def parse_role_ids(text: str) -> list[int]:
    """Role IDs from a string of role mentions, in order, without repeats."""
    seen: list[int] = []
    for raw in ROLE_MENTION.findall(text or ""):
        if int(raw) not in seen:
            seen.append(int(raw))
    return seen


def add_menu(storage_path: Path, guild_id: int, channel_id: int, message_id: int, title: str, style: str, mode: str,
             role_ids: list[int], created_by: int) -> int:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        cursor = connection.execute(
            "INSERT INTO role_menus (guild_id, channel_id, message_id, title, style, mode, role_ids, created_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (int(guild_id), int(channel_id), int(message_id), title[:200], style, mode, json.dumps([int(r) for r in role_ids]), int(created_by)),
        )
        return int(cursor.lastrowid)


def count_menus(storage_path: Path, guild_id: int) -> int:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        return int(connection.execute("SELECT COUNT(*) FROM role_menus WHERE guild_id = ?", (int(guild_id),)).fetchone()[0])


def get_by_message(storage_path: Path, message_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM role_menus WHERE message_id = ?", (int(message_id),)).fetchone()
    return _row(row) if row else None


def list_menus(storage_path: Path, guild_id: int) -> list[dict[str, Any]]:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        rows = connection.execute("SELECT * FROM role_menus WHERE guild_id = ? ORDER BY id", (int(guild_id),)).fetchall()
    return [_row(row) for row in rows]


def delete_menu(storage_path: Path, guild_id: int, menu_id: int) -> dict[str, Any] | None:
    ensure_table(storage_path)
    with connect(storage_path) as connection:
        row = connection.execute("SELECT * FROM role_menus WHERE id = ? AND guild_id = ?", (int(menu_id), int(guild_id))).fetchone()
        if row is None:
            return None
        connection.execute("DELETE FROM role_menus WHERE id = ?", (int(menu_id),))
    return _row(row)


def plan_changes(menu_roles: list[int], held: set[int], chosen: list[int], *, mode: str, toggle: bool) -> tuple[list[int], list[int]]:
    """Which of a menu's roles to add and remove for one interaction.

    A button press passes the one pressed role with toggle=True: it flips that
    role, and in single mode also drops the member's other roles from the
    menu. A dropdown passes everything selected with toggle=False: the
    member's roles from this menu become exactly the selection."""
    chosen = [role for role in chosen if role in menu_roles]
    in_menu = held & set(menu_roles)
    if toggle:
        role = chosen[0] if chosen else None
        if role is None:
            return [], []
        if role in in_menu:
            return [], [role]
        return [role], sorted(in_menu) if mode == "single" else []
    if mode == "single":
        chosen = chosen[:1]
    return [role for role in chosen if role not in in_menu], sorted(in_menu - set(chosen))
