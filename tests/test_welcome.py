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
    assert get_default_settings(storage)["welcome_style"] == "text"
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7]})
    member, sent = _member()
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    assert len(sent) == 1 and sent[0][0] == "Welcome <@5> to The Den!" and "embed" not in sent[0][1] and "file" not in sent[0][1]


def test_embed_style_builds_a_custom_embed(storage):
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7], "welcome_style": "embed", "join_message": "Glad you're here, {name}. You are member {count}.",
                                       "welcome_embed_title": "Hello {name}", "welcome_embed_color": "#ff8800",
                                       "welcome_embed_image_url": "https://example.com/banner.png", "welcome_embed_footer": "{server}"})
    member, sent = _member()
    asyncio.run(Welcome(SimpleNamespace(storage_path=storage)).on_member_join(member))
    content, kwargs = sent[0]
    embed = kwargs["embed"]
    assert content == "<@5>" and embed.title == "Hello Robin" and "member 1,234" in embed.description and embed.color.value == 0xFF8800
    assert embed.image.url == "https://example.com/banner.png" and embed.footer.text == "The Den" and embed.thumbnail.url.startswith("https://cdn")


def test_image_style_attaches_a_generated_card_and_dm_is_sent_when_possible(storage):
    update_guild_settings(storage, 1, {"welcome_channel_ids": [7], "welcome_style": "image", "welcome_dm_message": "Hi {name}, read the rules in {server}."})
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
    ok = validate_settings({**defaults, "welcome_style": "image", "welcome_embed_color": "#00ff88", "welcome_embed_image_url": " https://example.com/a.png "}, defaults)
    assert ok["welcome_embed_image_url"] == "https://example.com/a.png"
    for bad in ({"welcome_style": "video"}, {"welcome_embed_color": "blue"}, {"welcome_embed_image_url": "http://example.com/a.png"},
                {"welcome_embed_image_url": "javascript:alert(1)"}, {"welcome_embed_image_url": "https://a b"}):
        with pytest.raises(SettingsValidationError):
            validate_settings({**defaults, **bad}, defaults)
