import asyncio
import io
from pathlib import Path

import discord
import numpy as np
import pytest
from discord.ext import commands
from PIL import Image

from utils import image_filters as fx


def sample() -> Image.Image:
    """Red, green and blue bars with a half-transparent right edge."""
    pixels = np.zeros((20, 30, 4), dtype=np.uint8)
    pixels[:, :10] = (200, 30, 30, 255)
    pixels[:, 10:20] = (30, 200, 30, 255)
    pixels[:, 20:] = (30, 30, 200, 128)
    return Image.fromarray(pixels, "RGBA")


FILTERS = {
    "invert": fx.invert, "grayscale": fx.grayscale, "sepia": fx.sepia, "hue": lambda f: fx.hue_shift(f, 120),
    "adjust": lambda f: fx.adjust(f, 130, 70, 40), "posterize": lambda f: fx.posterize(f, 2), "sketch": fx.edges,
    "neon": lambda f: fx.edges(f, "neon"), "emboss": fx.emboss, "vaporwave": fx.vaporwave,
}


@pytest.mark.parametrize("name", sorted(FILTERS))
def test_filters_keep_size_and_transparency_and_change_the_picture(name):
    image = sample()
    result = FILTERS[name](image)
    assert result.mode == "RGBA" and result.size == image.size
    assert result.getchannel("A").tobytes() == image.getchannel("A").tobytes()
    assert result.convert("RGB").tobytes() != image.convert("RGB").tobytes()


def test_filters_do_what_they_say():
    image = sample()
    assert fx.invert(image).getpixel((0, 0)) == (55, 225, 225, 255)
    assert fx.invert(fx.invert(image)).tobytes() == image.tobytes()
    gray = fx.grayscale(image).getpixel((0, 0))
    assert gray[0] == gray[1] == gray[2]
    red, green, blue, _ = fx.hue_shift(image, 120).getpixel((0, 0))  # red turned a third of the way round is green
    assert green > red and green > blue
    assert set(np.unique(np.asarray(fx.posterize(image, 2).convert("RGB")))) <= {0, 255}
    assert fx.adjust(image, saturation=0).getpixel((0, 0))[0] == fx.adjust(image, saturation=0).getpixel((0, 0))[1]
    assert fx.adjust(image).tobytes() == image.tobytes()


def payload_size(node: dict) -> int:
    total = len(node.get("name", "")) + len(node.get("description", ""))
    total += sum(len(str(c["name"])) + len(str(c["value"])) for c in node.get("choices") or [])
    return total + sum(payload_size(option) for option in node.get("options") or [])


IMAGE_COGS = ["cogs.images", "cogs.image_filters", "cogs.image_animate", "cogs.image_memes"]


def test_image_command_groups_fit_discords_limits():
    async def scenario():
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.none())
        bot.project_root = Path(__file__).resolve().parents[1]
        for extension in IMAGE_COGS:
            await bot.load_extension(extension)
        groups = [command for command in bot.tree.get_commands() if isinstance(command, discord.app_commands.Group)]
        assert {"image", "filter", "animate", "memegen"} <= {group.name for group in groups}
        for group in groups:
            data = group.to_dict(bot.tree)
            assert payload_size(data) <= 8000, group.name  # Discord's cap on one command's names and descriptions
            assert len(data["options"]) <= 25, group.name
            for child in data["options"]:
                assert len(child.get("options") or []) <= 25, f"{group.name} {child['name']}"
                assert len(child["description"]) <= 100
        leaves = [c.name for c in bot.tree.walk_commands() if not isinstance(c, discord.app_commands.Group)]
        assert len(leaves) == len(set(leaves))  # blocking a command by name must not hit two commands

    asyncio.run(scenario())
