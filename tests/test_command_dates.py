import asyncio
import datetime
import json

import discord
from discord import app_commands
from discord.ext import commands

import main
from utils import command_dates


def registered_commands(tmp_path) -> set[str]:
    async def load() -> set[str]:
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
        bot.project_root = main.Path(main.__file__).resolve().parent
        bot.storage_path = tmp_path
        for extension in main.COGS:
            await bot.load_extension(extension)
        names = {command.qualified_name for command in bot.tree.walk_commands() if isinstance(command, app_commands.Command)}
        for kind in (discord.AppCommandType.message, discord.AppCommandType.user):
            names |= {menu.name for menu in bot.tree.get_commands(type=kind)}
        for extension in main.COGS:  # stops the background loops the cogs started
            await bot.unload_extension(extension)
        return names

    return asyncio.run(load())


def test_every_command_has_the_day_it_was_added(tmp_path):
    names = registered_commands(tmp_path)
    dates = json.loads(command_dates.DATES_FILE.read_text(encoding="utf-8"))
    today = datetime.date.today().isoformat()
    undated = sorted(names - set(dates))
    assert not undated, f"add these to utils/command_dates.json with the day they ship (today is {today}): {undated}"
    assert sorted(set(dates) - names) == []  # a command that is gone should lose its line
    assert list(dates) == sorted(dates)
    for name, date in dates.items():
        assert datetime.date.fromisoformat(date).isoformat() == date <= today, name
    assert command_dates.added_on("animate boom") == "2026-11-19" and command_dates.added_on("no such command") is None
