"""Who may open which part of a server's pages on the website.

Each area of the site needs a Discord permission to view it, and some need one
to change things in it. A server's administrators choose those permissions;
until they do, every area needs Administrator, as it always has. There are no
role lists: whoever holds the permission in Discord has the access.

The server's owner and anyone with Administrator can always do everything, and
only they can change these choices.
"""

from __future__ import annotations

from typing import Any

VIEW, MANAGE = "view", "manage"
# The areas, and what can be chosen for each. Logs, Members and Stats are read-only pages.
AREAS: dict[str, tuple[str, ...]] = {
    "moderation": (VIEW, MANAGE),
    "logs": (VIEW,),
    "members": (VIEW,),
    "stats": (VIEW,),
    "modules": (VIEW, MANAGE),
    "commands": (VIEW, MANAGE),
    "forms": (VIEW, MANAGE),  # view: read responses; manage: build forms and decide responses
}
# The permissions an area can be opened to, strongest first.
PERMISSIONS: tuple[str, ...] = (
    "administrator", "manage_guild", "ban_members", "kick_members", "moderate_members", "manage_messages", "manage_roles", "view_audit_log",
)
DEFAULT_PERMISSION = "administrator"
PREFIX = "access_"


def setting_key(area: str, level: str) -> str:
    return f"{PREFIX}{area}_{level}"


KEYS: tuple[str, ...] = tuple(setting_key(area, level) for area, levels in AREAS.items() for level in levels)


def full_access() -> dict[str, dict[str, bool]]:
    return {area: {VIEW: True, MANAGE: True} for area in AREAS}


def no_access() -> dict[str, dict[str, bool]]:
    return {area: {VIEW: False, MANAGE: False} for area in AREAS}


def access_for(permissions: Any, settings: dict[str, Any], *, is_owner: bool = False) -> dict[str, dict[str, bool]]:
    """What someone holding `permissions` in a server may do on its pages.
    Being able to manage an area includes being able to view it. A setting that
    is missing or not one of PERMISSIONS counts as Administrator."""
    if is_owner or getattr(permissions, "administrator", False):
        return full_access()
    access = no_access()
    for area, levels in AREAS.items():
        for level in levels:
            needed = settings.get(setting_key(area, level))
            if needed not in PERMISSIONS:
                needed = DEFAULT_PERMISSION
            access[area][level] = bool(getattr(permissions, needed, False))
        access[area][VIEW] = access[area][VIEW] or access[area][MANAGE]
    return access
