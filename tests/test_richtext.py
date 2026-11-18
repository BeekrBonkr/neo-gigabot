import asyncio
import io
import types

import numpy as np
import pytest
from PIL import Image

from utils import richtext as rt
from utils.imaging import ImageToolkit

IMPACT, ARIAL = "impact.ttf", "arial.ttf"


def pieces(text, primary=IMPACT, images=()):
    return [piece for paragraph in rt._parse(text, primary, frozenset(images)) for word in paragraph for piece in word]


def test_clusters_keep_what_reads_as_one_character_together():
    assert rt.clusters("é👍🏽👨‍👩‍👧🇺🇸a1️⃣") == ["é", "👍🏽", "👨‍👩‍👧", "🇺🇸", "a", "1️⃣"]
    assert rt.clusters("🇺🇸🇯🇵") == ["🇺🇸", "🇯🇵"]
    assert rt.clusters("") == []


def test_each_character_goes_to_a_font_that_can_draw_it():
    assert [(p.kind, p.font) for p in pieces("HELLO")] == [("text", IMPACT)]
    kinds = {p.text: (p.kind, p.font) for p in pieces("A 日本 한국 ไทย हिंदी ★ 😀")}
    assert kinds["A"] == ("text", IMPACT)
    assert kinds["★"] == ("text", "DejaVuSans-Bold.ttf")
    assert kinds["😀"][0] == "emoji"
    assert kinds["한"][1] == "NotoSansKR-Bold.otf" and kinds["ไทย"][1] == "NotoSansThai-Bold.ttf" and kinds["हिंदी"][1] == "NotoSansDevanagari-Bold.ttf"
    assert kinds["日"][1] in rt.FALLBACK_FONTS
    # a font that lists a character but draws nothing for it does not count as having it
    assert not rt._has(ARIAL, ord("♥")) and rt._has("DejaVuSans-Bold.ttf", ord("♥"))
    assert not rt._has("DroidSansFallbackFull.ttf", ord("한"))


def test_emoji_are_told_apart_from_symbols():
    def kinds(text):
        return [p.kind for p in pieces(text)]

    assert kinds("😀") == ["emoji"] and kinds("🇺🇸") == ["emoji"] and kinds("1️⃣") == ["emoji"]
    assert kinds("❤️") == ["emoji"] and kinds("❤") == ["text"]  # the same heart, asked for as emoji or as text
    assert kinds("123 #tag *") == ["text", "text", "text"]  # plain digits and signs are never emoji
    assert kinds("👨‍👩‍👧") == ["emoji"]  # a joined family is one emoji, not three


def test_fancy_letters_come_out_in_the_commands_own_font():
    assert [(p.text, p.font) for p in pieces("𝓗𝓮𝓵𝓵𝓸 ｗｉｄｅ ⓐⓑ")] == [("Hello", IMPACT), ("wide", IMPACT), ("ab", IMPACT)]
    assert rt.RichText("𝓫𝓸𝓽 ñ").upper().text == "BOT Ñ"


def test_invisible_and_control_characters_draw_nothing():
    assert [p.text for p in pieces("a​b\x07c﻿d")] == ["abcd"]
    unknown = pieces("\U00013000")  # a real character none of the fonts have
    assert [(p.text, p.font) for p in unknown] == [("\N{WHITE SQUARE}", rt.FALLBACK_FONTS[0])]
    assert rt.layout("\x07​", IMPACT, 40).width == 0


def test_wrapping_stays_inside_the_width():
    text = "the quick brown fox jumps over the lazy dog 😀 and keeps going for a while"
    arranged = rt.layout(text, IMPACT, 40, 300)
    assert len(arranged.lines) > 2 and all(line.width <= 300 for line in arranged.lines) and arranged.width <= 300
    words = sum(1 for line in arranged.lines for item in line.items)
    assert words == len(text.split())
    one_line = rt.layout(text, IMPACT, 40)
    assert len(one_line.lines) == 1 and one_line.width > 300
    assert len(rt.layout("a\n\nb", IMPACT, 40).lines) == 3  # blank lines are kept


def test_words_too_long_for_a_line_and_scripts_without_spaces_are_broken():
    arranged = rt.layout("x" * 120, IMPACT, 40, 300)
    assert len(arranged.lines) > 3 and all(line.width <= 300 for line in arranged.lines)
    assert "".join(item.piece.text for line in arranged.lines for item in line.items) == "x" * 120
    chinese = rt.layout("中文没有空格所以可以在任何地方换行" * 2, ARIAL, 40, 300)
    assert len(chinese.lines) > 3 and all(line.width <= 300 for line in chinese.lines)


def test_fit_picks_the_largest_size_that_fits_the_box():
    short = rt.fit("HI", IMPACT, 400, 200, max_size=300)
    long = rt.fit("a much longer line of text that has to shrink to fit in the same box", IMPACT, 400, 200, max_size=300)
    assert short.size > long.size and short.height <= 200 and long.height <= 200 and long.width <= 400
    assert rt.fit("HI", IMPACT, 400, 200, max_size=50).size == 50
    hopeless = rt.fit("word " * 400, IMPACT, 60, 20, min_size=12)
    assert hopeless.size == 12  # can't fit: the smallest size is returned, never an error
    outlined = rt.fit("OUTLINED TEXT", IMPACT, 300, 80, stroke=3)
    assert outlined.width <= 300 and outlined.height <= 80


def test_fit_line_shrinks_then_cuts_with_an_ellipsis():
    assert rt.fit_line("Short", ARIAL, 60, 600).size == 60
    shrunk = rt.fit_line("A somewhat longer name here", ARIAL, 60, 600, min_size=30)
    assert 30 <= shrunk.size < 60 and shrunk.width <= 600
    cut = rt.fit_line("An extremely long name 🐸 that goes on and on and on and on and on forever", ARIAL, 60, 400, min_size=40)
    text = "".join(item.piece.text for item in cut.lines[0].items)
    assert cut.width <= 400 and len(cut.lines) == 1 and text.endswith(rt.ELLIPSIS)


def painted(image):
    return np.asarray(image.convert("RGBA"))


def test_drawing_puts_letters_emoji_and_custom_images_on_the_picture():
    canvas = Image.new("RGBA", (400, 100), (0, 0, 0, 255))
    rt.draw_in_box(canvas, (0, 0, 400, 100), "HI 😀", IMPACT, fill=(255, 255, 255, 255), max_size=80)
    pixels = painted(canvas)[..., :3].astype(int)
    assert (pixels.min(axis=2) > 240).any()  # white letters
    yellow = (pixels[..., 0] > 200) & (pixels[..., 1] > 150) & (pixels[..., 2] < 120)
    assert yellow.sum() > 200  # the emoji is in color, not a box

    key = chr(rt.PLACEHOLDER_BASE)
    blob = Image.new("RGBA", (64, 64), (10, 200, 60, 255))
    canvas = Image.new("RGBA", (400, 100), (0, 0, 0, 255))
    rt.draw_in_box(canvas, (0, 0, 400, 100), rt.RichText(f"go {key}", {key: blob}), IMPACT, fill=(255, 255, 255, 255), max_size=80)
    pixels = painted(canvas)[..., :3].astype(int)
    assert ((pixels[..., 1] > 180) & (pixels[..., 0] < 60)).sum() > 500  # the custom emoji's own picture

    outlined = Image.new("RGBA", (400, 100), (128, 128, 128, 255))
    rt.draw_in_box(outlined, (0, 0, 400, 100), "HI", IMPACT, fill=(255, 255, 255, 255), stroke=4, stroke_fill=(0, 0, 0, 255), max_size=80)
    pixels = painted(outlined)[..., :3].astype(int)
    assert (pixels.max(axis=2) < 10).any() and (pixels.min(axis=2) > 245).any()  # black outline, white fill
    rgb = Image.new("RGB", (300, 80), (0, 0, 0))  # pictures without transparency work too
    rt.layout("ok 😀", ARIAL, 40).draw(rgb, 5, 5, fill=(255, 255, 255))
    assert np.asarray(rgb).max() > 200


def test_right_to_left_lines_read_from_the_right():
    arranged = rt.layout("שלום עולם", ARIAL, 40)
    first, second = arranged.lines[0].items
    assert first.piece.text == "עולם" and second.piece.text == "שלום" and first.x < second.x
    mixed = rt.layout("hello world", ARIAL, 40).lines[0].items
    assert [item.piece.text for item in mixed] == ["hello", "world"]


class _Guild:
    def get_member(self, target):
        return types.SimpleNamespace(display_name="Ana 🌸") if target == 111111111111111111 else None

    def get_role(self, target):
        return types.SimpleNamespace(name="Mods") if target == 222222222222222222 else None

    def get_channel_or_thread(self, target):
        return types.SimpleNamespace(name="general") if target == 333333333333333333 else None


def test_prepare_text_names_mentions_and_fetches_custom_emoji():
    tools = ImageToolkit(bot=None)
    fetched = []

    async def fetch(url):
        fetched.append(url)
        if "999999999999999999" in url:
            raise ValueError("gone")
        out = io.BytesIO()
        Image.new("RGBA", (32, 32), (255, 0, 0, 255)).save(out, format="PNG")
        return out.getvalue()

    tools.fetch_bytes_from_url = fetch
    raw = ("hi <@111111111111111111> <@!444444444444444444> <@&222222222222222222> in <#333333333333333333> "
           "<:blob:555555555555555555> <a:spin:555555555555555555> <:dead_one:999999999999999999> 😀")
    result = asyncio.run(tools.prepare_text(raw, _Guild()))
    key = chr(rt.PLACEHOLDER_BASE)
    assert result.text == f"hi @Ana 🌸 @unknown @Mods in #general {key} {key} :dead_one: 😀"
    assert list(result.images) == [key] and result.images[key].size == (32, 32)
    assert len(fetched) == 2 and all(url.startswith("https://cdn.discordapp.com/emojis/") for url in fetched)  # one fetch per emoji
    assert [p.kind for p in pieces(result.text, images=result.images)].count("image") == 2

    plain = asyncio.run(tools.prepare_text("no markup <@111111111111111111>", None))
    assert plain.text == "no markup <@111111111111111111>" and plain.images == {}  # no server to look names up in


def test_rich_text_behaves_like_text_where_commands_treat_it_as_text():
    key = chr(rt.PLACEHOLDER_BASE)
    rich = rt.RichText(f"  hi {key} ", {key: Image.new("RGBA", (4, 4))})
    assert rich.strip().text == f"hi {key}" and rich.upper().text == f"  HI {key} " and rich.upper().images == rich.images
    assert ("REWARD " + rich.strip()).text == f"REWARD hi {key}" and bool(rich) and not rt.RichText("   ")
    assert rt.as_rich("x").text == "x" and rt.as_rich(rich) is rich
