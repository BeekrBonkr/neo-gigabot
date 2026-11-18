from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace

import discord
import pytest
from PIL import Image

from cogs.welcome import Welcome, _substitute
from utils.settings import get_default_settings, update_guild_settings
from utils.welcome_card import CARD_SIZE, render_card


class FakeText(discord.TextChannel):
    """Passes the cog's isinstance check without needing Discord's channel data."""

    __slots__ = ("_send",)

    def __init__(self, send):
        self._send = send

    async def send(self, content=None, **kwargs):
        await self._send(content, **kwargs)


def _member(dms=None):
    sent = []

    async def send(content=None, **kwargs):
        sent.append((content, kwargs))

    async def dm(content=None, **kwargs):
        if dms is None:
            raise discord.HTTPException(SimpleNamespace(status=403, reason="Forbidden"), "closed")
        dms.append(content)

    async def read():
        out = io.BytesIO()
        Image.new("RGB", (64, 64), (200, 30, 30)).save(out, format="PNG")
        return out.getvalue()

    channel = FakeText(send)
    guild = SimpleNamespace(id=1, name="The Den", member_count=1234, get_channel=lambda cid: channel if cid == 7 else None)
    avatar = SimpleNamespace(url="https://cdn.discordapp.com/a.png", read=read)
    avatar.replace = lambda **kw: avatar
    member = SimpleNamespace(bot=False, guild=guild, mention="<@5>", display_name="Robin", display_avatar=avatar, send=dm, __str__=lambda: "robin")
    return member, sent


def test_placeholders_include_the_member_count_and_display_name():
    member, _ = _member()
    assert _substitute("{mention} {name} is #{count} in {server}", member) == "<@5> Robin is #1,234 in The Den"


def test_text_style_is_unchanged_and_the_default(storage):
    assert get_default_settings(storage)["welcome_styles"] == ["text"]
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7]})
    member, sent = _member()
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    assert len(sent) == 1 and sent[0][0] == "Welcome <@5> to The Den!" and "embed" not in sent[0][1] and "file" not in sent[0][1]


def test_embed_style_builds_a_custom_embed(storage):
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7], "welcome_styles": ["embed"], "join_message": "Glad you're here, {name}. You are member {count}.",
                                       "welcome_embed_title": "Hello {name}", "welcome_embed_color": "#ff8800",
                                       "welcome_embed_image_url": "https://example.com/banner.png", "welcome_embed_footer": "{server}"})
    member, sent = _member()
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    content, kwargs = sent[0]
    embed = kwargs["embed"]
    assert content == "<@5>" and embed.title == "Hello Robin" and "member 1,234" in embed.description and embed.color.value == 0xFF8800
    assert embed.image.url == "https://example.com/banner.png" and embed.footer.text == "The Den" and embed.thumbnail.url.startswith("https://cdn")


def test_image_style_attaches_a_generated_card_and_dm_is_sent_when_possible(storage):
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7], "welcome_styles": ["image"], "welcome_dm_message": "Hi {name}, read the rules in {server}."})
    dms = []
    member, sent = _member(dms)
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    assert dms == ["Hi Robin, read the rules in The Den."]
    content, kwargs = sent[0]
    assert content.startswith("Welcome <@5>") and kwargs["file"].filename == "welcome.png"
    assert Image.open(kwargs["file"].fp).size == CARD_SIZE
    closed, sent = _member(None)  # DMs closed: the channel greeting still goes out
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(closed))
    assert len(sent) == 1


def test_card_renders_without_an_avatar_and_with_long_or_odd_names():
    for name in ("Robin", "x" * 200, "名前 🎉", ""):
        image = Image.open(io.BytesIO(render_card(None, name=name, server="S" * 150, member_number=10**7, accent=0x00FF88)))
        assert image.size == CARD_SIZE and image.format == "PNG"
    assert Image.open(io.BytesIO(render_card(b"not an image", name="a", server="b", member_number=1))).size == CARD_SIZE


def test_panel_validates_the_welcome_color_and_image_link(storage):
    from cogs.panel_bridge import SettingsValidationError, validate_settings

    defaults = get_default_settings(storage)
    ok = validate_settings({**defaults, "welcome_styles": ["image", "text"], "welcome_embed_color": "#00ff88", "welcome_embed_image_url": " https://example.com/a.png "}, defaults)
    assert ok["welcome_embed_image_url"] == "https://example.com/a.png" and ok["welcome_styles"] == ["text", "image"]
    for bad in ({"welcome_styles": ["video"]}, {"welcome_styles": "text"}, {"welcome_card_gradient": "spiral"}, {"welcome_card_color_start": "red"},
                {"welcome_card_dim": 100}, {"welcome_card_title": "x" * 41}, {"welcome_embed_color": "blue"}, {"welcome_embed_image_url": "http://example.com/a.png"},
                {"welcome_embed_image_url": "javascript:alert(1)"}, {"welcome_embed_image_url": "https://a b"}):
        with pytest.raises(SettingsValidationError):
            validate_settings({**defaults, **bad}, defaults)


def _join(storage, **settings):
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7], **settings})
    member, sent = _member()
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    return sent


def test_every_chosen_form_goes_out_in_one_message(storage):
    content, kwargs = _join(storage, welcome_styles=["text", "embed", "image"], welcome_embed_title="Hello {name}",
                            welcome_embed_image_url="https://example.com/banner.png")[0]
    embed = kwargs["embed"]
    assert content == "Welcome <@5> to The Den!" and embed.title == "Hello Robin"
    assert embed.description is None            # the message is the text above, not repeated in the embed
    assert embed.image.url == "attachment://welcome.png" and kwargs["file"].filename == "welcome.png"  # the card sits inside the embed
    # Text and embed without the image: the banner link is used again.
    content, kwargs = _join(storage, welcome_styles=["text", "embed"])[0]
    assert content.startswith("Welcome") and kwargs["embed"].image.url == "https://example.com/banner.png" and "file" not in kwargs


def test_no_form_chosen_posts_nothing_but_the_dm_still_goes(storage):
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7], "welcome_styles": [], "welcome_dm_message": "Hi {name}"})
    dms = []
    member, sent = _member(dms)
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    assert sent == [] and dms == ["Hi Robin"]


def test_welcome_and_leave_are_switched_separately(storage):
    cog = Welcome(SimpleNamespace(storage_path=storage))
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7]})  # turns welcome on, and only welcome
    member, sent = _member()
    asyncio.run(cog.on_member_join(member))
    asyncio.run(cog.on_member_remove(member))
    assert [content for content, _ in sent] == ["Welcome <@5> to The Den!"]
    update_guild_settings(storage, 1, {"welcome_enabled": False, "leave_enabled": True})
    member, sent = _member()
    asyncio.run(cog.on_member_join(member))
    asyncio.run(cog.on_member_remove(member))
    assert [content for content, _ in sent] == ["<@5> has left the server."]  # falls back to the welcome channel


def test_older_settings_keep_their_style_and_their_leave_messages(storage):
    import json

    from utils.db import connect
    from utils.settings import DEFAULT_GUILD_SETTINGS, clear_settings_caches, ensure_settings_database, get_guild_settings

    def stored(guild_id, **changes):
        old = {k: v for k, v in DEFAULT_GUILD_SETTINGS.items() if k not in ("welcome_styles", "leave_enabled") and not k.startswith("welcome_card_")}
        old.update(welcome_style="image", **changes)
        ensure_settings_database(storage)
        with connect(storage) as connection:
            connection.execute("INSERT INTO guild_settings (guild_id, data) VALUES (?, ?)", (guild_id, json.dumps(old)))
            connection.commit()
        clear_settings_caches()
        return get_guild_settings(storage, guild_id)

    had_both = stored(11, welcome_enabled=True, welcome_channel_ids=[7])
    assert had_both["welcome_styles"] == ["image"] and "welcome_style" not in had_both and had_both["leave_enabled"] is True
    assert stored(12, welcome_enabled=True, welcome_channel_ids=[7], leave_message="")["leave_enabled"] is False
    assert stored(13, welcome_enabled=False, welcome_channel_ids=[7])["leave_enabled"] is False


def test_card_backgrounds_gradients_and_text_can_be_customized(storage):
    from utils import welcome_card

    def pixel(image_bytes, x, y):
        return Image.open(io.BytesIO(image_bytes)).convert("RGB").getpixel((x, y))

    plain = dict(name="Robin", server="The Den", member_number=5)
    solid = render_card(None, start=0x102030, end=0xFFFFFF, gradient_kind="solid", **plain)
    assert pixel(solid, 990, 5) == (0x10, 0x20, 0x30)
    across = render_card(None, start=0x000000, end=0xFF0000, gradient_kind="horizontal", **plain)
    down = render_card(None, start=0x000000, end=0xFF0000, gradient_kind="vertical", **plain)
    assert pixel(across, 995, 5)[0] > 240 and pixel(across, 5, 355)[0] < 10
    assert pixel(down, 995, 5)[0] < 10 and pixel(down, 995, 355)[0] > 240
    for kind in welcome_card.GRADIENTS + ("nonsense",):
        assert Image.open(io.BytesIO(render_card(None, gradient_kind=kind, title="", subtitle="", text_color=0x112233, **plain))).size == CARD_SIZE

    # An uploaded picture is cut to the card's shape, stored, and darkened behind the text.
    upload = io.BytesIO()
    Image.new("RGB", (2000, 2000), (200, 100, 50)).save(upload, format="PNG")
    prepared = welcome_card.prepare_background(upload.getvalue())
    assert Image.open(io.BytesIO(prepared)).size == CARD_SIZE and Image.open(io.BytesIO(prepared)).format == "JPEG"
    welcome_card.save_background(storage, 1, prepared)
    update_guild_settings(storage, 1, {"welcome_card_background": "image", "welcome_card_dim": 50, "welcome_card_color_start": "#00ff00"})
    from utils.settings import get_guild_settings

    options = welcome_card.card_options(storage, 1, get_guild_settings(storage, 1))
    assert options["background"] == prepared and options["dim"] == 50 and options["start"] == 0x00FF00
    red, green, blue = pixel(render_card(None, **plain, **options), 990, 5)
    assert abs(red - 100) < 6 and abs(green - 50) < 6 and abs(blue - 25) < 6
    for bad in (b"", b"not an image", b"x" * (welcome_card.MAX_UPLOAD_BYTES + 1)):
        with pytest.raises(welcome_card.BackgroundError):
            welcome_card.prepare_background(bad)
    assert welcome_card.delete_background(storage, 1) and welcome_card.read_background(storage, 1) is None
    assert welcome_card.card_text("#{count} {name} {mention} {server}", name="Robin", user="robin", server="Den", count=1234) == "#1,234 Robin Robin Den"
