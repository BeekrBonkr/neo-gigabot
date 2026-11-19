import asyncio
import io
from types import SimpleNamespace

import discord
from discord import app_commands

from utils import samples


def make_commands():
    @app_commands.command(name="echo", description="Post the picture back with a caption.")
    @app_commands.choices(style=[app_commands.Choice(name="Loud", value="loud")])
    async def echo(interaction: discord.Interaction, text: str, style: app_commands.Choice[str], attachment: discord.Attachment | None = None,
                   size: app_commands.Range[int, 5, 50] = 10) -> None:
        assert interaction.guild is None and interaction.channel is None and not interaction.response.is_done()
        await interaction.response.defer(thinking=True)
        data = await attachment.read()
        caption = f"{text}|{style.value}|{size}|{attachment.content_type}".encode()
        await interaction.followup.send(embed=None, file=discord.File(io.BytesIO(data + caption), filename="echo.png"))

    @app_commands.command(name="ping", description="No picture involved.")
    async def ping(interaction: discord.Interaction) -> None: ...

    @app_commands.command(name="avatar", description="Needs a member.")
    async def avatar(interaction: discord.Interaction, member: discord.Member, attachment: discord.Attachment | None = None) -> None: ...

    @app_commands.command(name="broken", description="Fails.")
    async def broken(interaction: discord.Interaction, attachment: discord.Attachment) -> None:
        raise ValueError("no face found")

    @app_commands.command(name="refuses", description="Answers with an error and no file.")
    async def refuses(interaction: discord.Interaction, attachment: discord.Attachment) -> None:
        await interaction.response.send_message("nope", ephemeral=True)

    return echo, ping, avatar, broken, refuses


def test_a_sample_is_what_the_command_would_have_posted():
    echo, ping, avatar, broken, refuses = make_commands()
    assert samples.can_sample(echo) and samples.can_sample(broken)
    assert not samples.can_sample(ping) and not samples.can_sample(avatar)  # no picture to work on, or it needs a server
    bot = SimpleNamespace(user=SimpleNamespace(id=1))

    async def run():
        made = await samples.render(bot, echo, b"PIC")
        assert made == (b"PIC" + f"{samples.SAMPLE_TEXT}|loud|10|image/png".encode(), "echo.png")  # required options filled, optional ones left alone
        assert await samples.render(bot, ping, b"PIC") is None
        assert await samples.render(bot, broken, b"PIC") is None and await samples.render(bot, refuses, b"PIC") is None

    asyncio.run(run())


def test_the_store_makes_each_sample_once_from_the_bots_own_picture():
    echo, *_ = make_commands()
    reads = []

    class Avatar:
        def replace(self, **_):
            return self

        async def read(self):
            reads.append(1)
            return b"AVATAR"

    bot = SimpleNamespace(user=SimpleNamespace(id=1, display_avatar=Avatar()), tree=SimpleNamespace(walk_commands=lambda: [echo]))
    store = samples.SampleStore(bot)

    async def run():
        first, again, missing = await asyncio.gather(store.get("echo"), store.get("echo"), store.get("nothing"))
        assert first is again and first[0].startswith(b"AVATAR") and missing is None
        assert len(reads) == 1

    asyncio.run(run())
