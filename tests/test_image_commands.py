"""Every new image command, run through its real handler with a stand-in for
Discord: the right source is fetched, the work runs, and a file comes back."""

import asyncio
import io
import types

import pytest
from PIL import Image

from cogs.image_animate import ImageAnimate
from cogs.image_edit import ImageEdit
from cogs.image_filters import ImageFilters
from cogs.image_memes import ImageMemes
from utils.imaging import ImageToolkit


def png_bytes(size=(96, 72)) -> bytes:
    image = Image.new("RGBA", size, (200, 60, 40, 255))
    image.paste((40, 60, 200, 255), (0, 0, size[0] // 2, size[1]))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def gif_bytes(count=4) -> bytes:
    frames = [Image.new("RGBA", (64, 48), (index * 60, 120, 200, 255)) for index in range(count)]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return out.getvalue()


class FakeAttachment:
    def __init__(self, data: bytes, filename: str = "upload.png") -> None:
        self.data, self.filename, self.size, self.content_type = data, filename, len(data), "image/png"

    async def read(self) -> bytes:
        return self.data


class FakeAvatar:
    def replace(self, **_):
        return self

    def is_animated(self) -> bool:
        return False

    async def read(self) -> bytes:
        return png_bytes((128, 128))


class Harness:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.errors: list[tuple[str, str]] = []
        self.bot = types.SimpleNamespace()
        tools = ImageToolkit(self.bot)
        self.bot.image_tools = tools
        self.channel_image = (png_bytes(), "recent.png")

        async def allowed(interaction, name):
            return True

        async def nothing(*args, **kwargs):
            return None

        async def from_channel(interaction):
            return self.channel_image

        async def send_file(interaction, *, data, filename, title, description=None):
            self.sent.append({"data": data, "filename": filename, "title": title, "description": description})

        async def send_error(interaction, title, description):
            self.errors.append((title, description))

        tools.ensure_command_allowed, tools.defer, tools.note_heavy_command = allowed, nothing, nothing
        tools.history_image, tools.send_processed_file, tools.send_error = from_channel, send_file, send_error

        async def no_selection(interaction):
            return None

        tools.selected_message_image = no_selection
        self.interaction = types.SimpleNamespace(guild=None, user=types.SimpleNamespace(id=1), channel=None)

    def run(self, cog_type, command: str, **options):
        cog = cog_type(self.bot)
        asyncio.run(getattr(cog, command).callback(cog, self.interaction, **options))
        return self.sent[-1] if self.sent else None


def choice(value: str):
    return types.SimpleNamespace(value=value)


USER = types.SimpleNamespace(display_avatar=FakeAvatar(), display_name="Somebody")

STILL_COMMANDS = [
    (ImageFilters, "invert", {}, "png"), (ImageFilters, "grayscale", {}, "png"), (ImageFilters, "sepia", {}, "png"),
    (ImageFilters, "hue", {"degrees": 90}, "png"), (ImageFilters, "adjust", {"brightness": 150}, "png"),
    (ImageFilters, "posterize", {"levels": 3}, "png"), (ImageFilters, "edges", {"style": choice("neon")}, "png"),
    (ImageFilters, "emboss", {}, "png"), (ImageFilters, "vaporwave", {}, "png"),
    (ImageAnimate, "spin", {"direction": choice("counterclockwise")}, "gif"), (ImageAnimate, "rainbow", {}, "gif"),
    (ImageAnimate, "wave", {"strength": 8}, "gif"), (ImageAnimate, "zoom", {}, "gif"),
    (ImageAnimate, "pet", {}, "gif"), (ImageAnimate, "pet", {"user": USER}, "gif"), (ImageAnimate, "bonk", {"user": USER}, "gif"),
    (ImageMemes, "speechbubble", {"tail": choice("right")}, "png"), (ImageMemes, "jail", {}, "png"), (ImageMemes, "wasted", {}, "png"),
    (ImageMemes, "wanted", {"name": "Somebody", "reward": "$1"}, "png"), (ImageMemes, "wanted", {"user": USER}, "png"),
    (ImageMemes, "drake", {"reject": "this", "approve": "that"}, "png"),
    (ImageMemes, "drake", {"reject_image": FakeAttachment(png_bytes()), "approve": "that"}, "png"),
    (ImageMemes, "changemymind", {"text": "tests are worth it"}, "png"),
    (ImageEdit, "mirror", {"side": choice("top")}, "png"), (ImageEdit, "tile", {"columns": 2}, "png"),
    (ImageEdit, "circle", {}, "png"), (ImageEdit, "circle", {"user": USER}, "png"),
    (ImageEdit, "border", {"color": "#5865F2", "rounding": 20}, "png"), (ImageEdit, "resize", {"percent": 50}, "png"),
    (ImageEdit, "collage", {"image1": FakeAttachment(png_bytes()), "image2": FakeAttachment(png_bytes((50, 90)))}, "png"),
    (ImageEdit, "palette", {"colors": 3}, "png"), (ImageEdit, "removebg", {}, "png"),
    (ImageAnimate, "togif", {"image1": FakeAttachment(png_bytes()), "image2": FakeAttachment(png_bytes((50, 90))), "delay": 200}, "gif"),
]


@pytest.mark.parametrize("cog_type, command, options, kind", STILL_COMMANDS, ids=[f"{c.__name__}.{n}" for c, n, _, _ in STILL_COMMANDS])
def test_command_sends_a_file(cog_type, command, options, kind):
    harness = Harness()
    sent = harness.run(cog_type, command, **options)
    assert harness.errors == [] and sent is not None
    assert sent["filename"].endswith("." + kind)
    result = Image.open(io.BytesIO(sent["data"]))
    assert result.format == kind.upper() and result.width > 0
    if kind == "gif":
        assert result.n_frames > 1


GIF_COMMANDS = [
    (ImageAnimate, "speed", {"percent": 200}, "gif"), (ImageAnimate, "reverse", {}, "gif"), (ImageAnimate, "frames", {"number": 2}, "png"),
    (ImageFilters, "invert", {}, "gif"), (ImageAnimate, "rainbow", {}, "gif"), (ImageMemes, "jail", {}, "gif"),
    (ImageEdit, "circle", {}, "gif"), (ImageEdit, "resize", {"width": 32}, "gif"),
]


@pytest.mark.parametrize("cog_type, command, options, kind", GIF_COMMANDS, ids=[f"{c.__name__}.{n}" for c, n, _, _ in GIF_COMMANDS])
def test_command_handles_a_gif(cog_type, command, options, kind):
    harness = Harness()
    harness.channel_image = (gif_bytes(), "recent.gif")
    sent = harness.run(cog_type, command, **options)
    assert harness.errors == [] and sent["filename"].endswith("." + kind)
    result = Image.open(io.BytesIO(sent["data"]))
    if kind == "gif":
        assert result.n_frames == 4
    if command == "frames":
        assert sent["description"].startswith("Frame 2 of 4")
    if command == "resize":
        assert result.size == (32, 24) and sent["description"] == "64x48 to 32x24"


def test_mistakes_get_a_plain_message_instead_of_a_file():
    cases = [
        (ImageAnimate, "reverse", {}, "needs a GIF"), (ImageAnimate, "speed", {"percent": 200}, "needs a GIF"),
        (ImageFilters, "adjust", {}, "other than 100"), (ImageEdit, "border", {"color": "nope"}, "don't know the color"),
        (ImageEdit, "border", {"thickness": 0}, "thickness"), (ImageEdit, "resize", {}, "width, a height, or a percent"),
        (ImageEdit, "resize", {"percent": 1000, "width": None}, "at most"), (ImageEdit, "tile", {"columns": 1}, "1 by 1"),
        (ImageMemes, "drake", {"reject": "only one"}, "both"),
    ]
    for cog_type, command, options, expected in cases:
        harness = Harness()
        if command == "resize" and options:
            harness.channel_image = (png_bytes((900, 900)), "big.png")
        assert harness.run(cog_type, command, **options) is None, command
        assert len(harness.errors) == 1 and expected in harness.errors[0][1], (command, harness.errors)


def test_an_attachment_wins_over_the_channels_latest_image():
    harness = Harness()
    sent = harness.run(ImageEdit, "resize", percent=100, attachment=FakeAttachment(png_bytes((10, 20))))
    assert Image.open(io.BytesIO(sent["data"])).size == (10, 20)


# ---------- the older /image text commands, which share the text renderer ----------

from pathlib import Path

from cogs.images import Images

HARD_TEXT = "𝓯𝓪𝓷𝓬𝔂 ça 日本 한국 😀 🇺🇸 <@1> ​ ★"


class _Embeds:
    pass


def images_cog(harness: Harness) -> Images:
    harness.bot.project_root = Path(__file__).resolve().parents[1]
    harness.bot.tree = types.SimpleNamespace(add_command=lambda *a, **k: None, remove_command=lambda *a, **k: None)
    return Images(harness.bot)


@pytest.mark.parametrize("command, options", [
    ("toptext", {"text": HARD_TEXT}), ("middletext", {"text": HARD_TEXT, "max_size": 40}), ("bottomtext", {"text": HARD_TEXT}),
    ("caption", {"text": HARD_TEXT}), ("motivate", {"title": HARD_TEXT, "subtitle": HARD_TEXT}),
])
def test_text_commands_draw_text_no_single_font_could(command, options):
    harness = Harness()
    harness.channel_image = (png_bytes((400, 300)), "recent.png")
    cog = images_cog(harness)
    asyncio.run(getattr(cog, command).callback(cog, harness.interaction, **options))
    assert harness.errors == [] and len(harness.sent) == 1
    result = Image.open(io.BytesIO(harness.sent[0]["data"])).convert("RGB")
    plain = Image.open(io.BytesIO(png_bytes((400, 300)))).convert("RGB")
    assert result.size[0] >= 400 and (result.size != plain.size or result.tobytes() != plain.tobytes())


def test_quote_image_handles_markdown_mentions_and_emoji():
    harness = Harness()
    cog = images_cog(harness)
    guild = types.SimpleNamespace(
        get_member=lambda target: types.SimpleNamespace(display_name="Ana"), get_role=lambda target: None, get_channel_or_thread=lambda target: None
    )
    author = types.SimpleNamespace(display_name="Zoë 🌸", display_avatar=FakeAvatar())
    message = types.SimpleNamespace(content="**bold** claim by <@111111111111111111>: ça marche 😀 " + "long " * 150, guild=guild, author=author)
    data, name = asyncio.run(cog.build_quote_image(message))
    assert name == "quote.png" and Image.open(io.BytesIO(data)).size == (400, 400)
    with pytest.raises(ValueError, match="any text"):
        asyncio.run(cog.build_quote_image(types.SimpleNamespace(content="   ", guild=guild, author=author)))
