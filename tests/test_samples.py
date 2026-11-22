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


def test_a_command_with_nothing_to_show_for_the_avatar_is_tried_on_a_face_and_then_a_gif(monkeypatch):
    seen = []

    def needs(kind):
        @app_commands.command(name=f"only{kind}", description="Picky.")
        async def picky(interaction: discord.Interaction, attachment: discord.Attachment) -> None:
            seen.append(attachment.content_type)
            if attachment.content_type != f"image/{kind}":
                await interaction.response.send_message("not for me", ephemeral=True)
                return
            await interaction.response.send_message(file=discord.File(io.BytesIO(await attachment.read()), filename=attachment.filename))
        return picky

    class Avatar:
        def replace(self, **_):
            return self

        async def read(self):
            return b"AVATAR"

    monkeypatch.setattr(samples, "_moving", lambda avatar: b"GIF-OF-" + avatar)
    face, gif, never = needs("jpeg"), needs("gif"), needs("bmp")
    bot = SimpleNamespace(user=SimpleNamespace(id=1, display_avatar=Avatar()), tree=SimpleNamespace(walk_commands=lambda: [face, gif, never]))
    store = samples.SampleStore(bot)

    async def run():
        assert await store.get("onlyjpeg") == (samples.FACE_PICTURE.read_bytes(), "sample.jpeg")
        assert seen == ["image/png", "image/jpeg"]  # the avatar first, then the portrait, and no further
        assert await store.get("onlygif") == (b"GIF-OF-AVATAR", "sample.gif")
        assert await store.get("onlybmp") is None

    asyncio.run(run())


def test_commands_that_need_an_option_set_get_one():
    @app_commands.command(name="resize", description="Resize.")
    async def resize(interaction: discord.Interaction, percent: int | None = None, attachment: discord.Attachment | None = None) -> None:
        await interaction.response.send_message(file=discord.File(io.BytesIO(str(percent).encode()), filename="r.png"))

    group = app_commands.Group(name="edit", description="x")
    group.add_command(resize)
    assert asyncio.run(samples.render(SimpleNamespace(user=None), resize, b"PIC")) == (b"50", "r.png")

    @app_commands.command(name="convert", description="Change the file format.")
    async def convert(interaction: discord.Interaction, attachment: discord.Attachment) -> None: ...

    effects = app_commands.Group(name="effects", description="x", parent=app_commands.Group(name="image", description="x"))
    effects.add_command(convert)
    assert convert.qualified_name in samples.NO_SAMPLE and not samples.can_sample(convert)  # nothing to see in a picture of it


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


def test_a_command_that_changes_only_the_sound_has_no_example():
    from cogs.image_animate import ImageAnimate

    cog = ImageAnimate(SimpleNamespace())
    by_name = {command.qualified_name: command for command in cog.walk_app_commands()}
    assert not samples.can_sample(by_name["animate bitcrush"])   # its result is a sound: nothing to show in a picture
    assert samples.can_sample(by_name["animate datamosh"])


def test_commands_without_a_picture_say_why_instead():
    from cogs.image_animate import ImageAnimate

    cog = ImageAnimate(SimpleNamespace())
    by_name = {command.qualified_name: command for command in cog.walk_app_commands()}
    assert "changes the sound" in samples.sample_note(by_name["animate bitcrush"])
    assert samples.sample_note(by_name["animate datamosh"]) == ""
    assert all(note and len(note) < 300 for note in samples.NO_SAMPLE.values())
