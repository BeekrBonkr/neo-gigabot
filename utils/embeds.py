from __future__ import annotations

import discord


def info_embed(title: str, description: str) -> discord.Embed:
    return discord.Embed(title=title, description=description)


def error_embed(description: str) -> discord.Embed:
    return discord.Embed(title="Error", description=description)
