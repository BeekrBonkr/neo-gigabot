"""The generated welcome image: avatar, name and member number on a gradient."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from utils import richtext

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
CARD_SIZE = (1000, 360)
AVATAR_SIZE = 220
DEFAULT_ACCENT = 0x5865F2


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


def render_card(avatar_png: bytes | None, *, name: str, server: str, member_number: int, accent: int | None = None) -> bytes:
    """A PNG welcome card. `avatar_png` is the member's avatar image, or None
    for a plain circle. CPU-bound: call it off the event loop."""
    accent = DEFAULT_ACCENT if accent is None else accent
    red, green, blue = (accent >> 16) & 255, (accent >> 8) & 255, accent & 255
    width, height = CARD_SIZE
    card = Image.new("RGB", CARD_SIZE)
    draw = ImageDraw.Draw(card)
    for x in range(width):  # dark on the left fading into the accent color
        mix = (x / width) ** 1.4
        draw.line([(x, 0), (x, height)], fill=(int(18 + (red * 0.55 - 18) * mix), int(20 + (green * 0.55 - 20) * mix), int(32 + (blue * 0.55 - 32) * mix)))

    left, top = 60, (height - AVATAR_SIZE) // 2
    ring = 8
    draw.ellipse([left - ring, top - ring, left + AVATAR_SIZE + ring, top + AVATAR_SIZE + ring], fill=(red, green, blue))
    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, AVATAR_SIZE, AVATAR_SIZE], fill=255)
    avatar = None
    if avatar_png:
        try:
            avatar = Image.open(io.BytesIO(avatar_png)).convert("RGB").resize((AVATAR_SIZE, AVATAR_SIZE))
        except Exception:  # noqa: BLE001 - a broken avatar just means the plain circle
            avatar = None
    card.paste(avatar or Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), (54, 57, 63)), (left, top), mask)

    text_left, max_width = left + AVATAR_SIZE + 50, width - (left + AVATAR_SIZE + 50) - 40
    draw.text((text_left, 70), "WELCOME", font=_font("impact.ttf", 44), fill=(red, green, blue) if (red + green + blue) > 240 else (235, 235, 245))
    # Names are whatever people chose: emoji, other scripts, fancy letters. richtext draws all of those,
    # shrinking each line to 60% at most and then cutting it short.
    richtext.fit_line(name, "arial.ttf", 76, max_width, min_size=46).draw(card, text_left, 125, align="left", fill=(255, 255, 255))
    richtext.fit_line(f"Member #{member_number:,} of {server}", "arial.ttf", 34, max_width, min_size=20).draw(
        card, text_left, 235, align="left", fill=(200, 203, 215)
    )

    out = io.BytesIO()
    card.save(out, format="PNG", optimize=True)
    return out.getvalue()
