"""Links to the bot's website (the companion neogigabot-panel site).

Everything here is a no-op until WEBSITE_URL is set in .env, so a self-hosted
copy without the site never shows dead links.
"""

from __future__ import annotations

from typing import Any

import discord


def site_url(bot: Any, path: str = "") -> str:
    """The website's address plus `path`, or "" when no website is configured."""
    base = str(getattr(getattr(bot, "config", None), "website_url", "") or "").rstrip("/")
    return f"{base}{path}" if base else ""


def site_link(bot: Any, label: str, path: str = "") -> str:
    """A markdown link for embeds, or "" when no website is configured."""
    url = site_url(bot, path)
    return f"[{label}]({url})" if url else ""


def site_button(bot: Any, label: str, path: str = "", *, row: int | None = None) -> discord.ui.Button | None:
    """A link button for views, or None when no website is configured."""
    url = site_url(bot, path)
    return discord.ui.Button(label=label, url=url, style=discord.ButtonStyle.link, emoji="🌐", row=row) if url else None
