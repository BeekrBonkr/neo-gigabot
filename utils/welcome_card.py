"""The generated welcome image: avatar, name and two lines of text on a
gradient or on a picture the server uploaded."""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont, ImageOps

from utils import richtext

FONT_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
CARD_SIZE = (1000, 360)
AVATAR_SIZE = 220
DEFAULT_ACCENT = 0x5865F2
DEFAULT_START = 0x121420
GRADIENTS = ("horizontal", "vertical", "diagonal", "radial", "solid")
# Uploaded backgrounds: what is accepted, and what is kept.
MAX_UPLOAD_BYTES = 8 * 1024 * 1024
MAX_UPLOAD_PIXELS = 40_000_000
BACKGROUND_DIR = "welcome_cards"


class BackgroundError(ValueError):
    """An uploaded background that can't be used; the message is safe to show."""


def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONT_DIR / name), size)
    except OSError:
        return ImageFont.load_default()


def _rgb(value: int) -> tuple[int, int, int]:
    return (value >> 16) & 255, (value >> 8) & 255, value & 255


def default_end(accent: int) -> int:
    """The accent color, darkened: what the right-hand side fades into when no end color is chosen."""
    red, green, blue = _rgb(accent)
    return (int(red * 0.55) << 16) | (int(green * 0.55) << 8) | int(blue * 0.55)


def gradient(start: int, end: int, kind: str = "horizontal") -> Image.Image:
    """The card's background as a blend from `start` to `end`."""
    width, height = CARD_SIZE
    xs, ys = np.meshgrid(np.linspace(0.0, 1.0, width), np.linspace(0.0, 1.0, height))
    if kind == "vertical":
        mix = ys
    elif kind == "diagonal":
        mix = (xs + ys) / 2
    elif kind == "radial":
        # Centered on the avatar, so the start color glows around it.
        cx, cy = (60 + AVATAR_SIZE / 2) / width, 0.5
        distance = np.sqrt(((xs - cx) * width) ** 2 + ((ys - cy) * height) ** 2)
        mix = np.clip(distance / (width * 0.75), 0.0, 1.0)
    elif kind == "solid":
        mix = np.zeros_like(xs)
    else:
        mix = xs ** 1.4  # stays on the start color a little longer, as the card always has
    first, second = np.array(_rgb(start), dtype=np.float32), np.array(_rgb(end), dtype=np.float32)
    pixels = first + (second - first) * mix[..., None]
    return Image.fromarray(pixels.astype(np.uint8), "RGB")


def prepare_background(data: bytes) -> bytes:
    """Check an uploaded picture and return it cut to the card's shape as a
    JPEG. Raises BackgroundError with a message for the person uploading."""
    if not data:
        raise BackgroundError("Choose an image to upload.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise BackgroundError(f"That image is too large. The limit is {MAX_UPLOAD_BYTES // (1024 * 1024)} MB.")
    try:
        with Image.open(io.BytesIO(data)) as probe:
            if probe.format not in ("PNG", "JPEG", "WEBP", "GIF"):
                raise BackgroundError("Use a PNG, JPEG, WebP or GIF image.")
            if probe.width * probe.height > MAX_UPLOAD_PIXELS:
                raise BackgroundError("That image has too many pixels. Use one under 40 megapixels.")
            probe.seek(0)
            picture = ImageOps.exif_transpose(probe).convert("RGB")
    except BackgroundError:
        raise
    except Exception as exc:  # noqa: BLE001 - anything Pillow can't read is simply not an image
        raise BackgroundError("That file could not be read as an image.") from exc
    fitted = ImageOps.fit(picture, CARD_SIZE, Image.LANCZOS)
    out = io.BytesIO()
    fitted.save(out, format="JPEG", quality=90, optimize=True)
    return out.getvalue()


def background_path(storage_path: Path, guild_id: int | str) -> Path:
    return Path(storage_path) / BACKGROUND_DIR / f"{int(guild_id)}.jpg"


def read_background(storage_path: Path, guild_id: int | str) -> bytes | None:
    path = background_path(storage_path, guild_id)
    try:
        return path.read_bytes()
    except OSError:
        return None


def save_background(storage_path: Path, guild_id: int | str, data: bytes) -> None:
    """Store an uploaded background (already through prepare_background)."""
    path = background_path(storage_path, guild_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_bytes(data)
    temp.replace(path)


def delete_background(storage_path: Path, guild_id: int | str) -> bool:
    try:
        background_path(storage_path, guild_id).unlink()
        return True
    except OSError:
        return False


def render_card(
    avatar_png: bytes | None, *, name: str, server: str, member_number: int, accent: int | None = None,
    start: int | None = None, end: int | None = None, gradient_kind: str = "horizontal", text_color: int | None = None,
    title: str = "WELCOME", subtitle: str | None = None, background: bytes | None = None, dim: int = 45,
) -> bytes:
    """A PNG welcome card. `avatar_png` is the member's avatar image, or None
    for a plain circle. `background` is a stored picture to use instead of the
    gradient, darkened by `dim` percent so the text stays readable. `subtitle`
    is the finished line under the name. CPU-bound: call it off the event loop."""
    accent = DEFAULT_ACCENT if accent is None else accent
    width, height = CARD_SIZE
    card = None
    if background:
        try:
            picture = ImageOps.fit(Image.open(io.BytesIO(background)).convert("RGB"), CARD_SIZE, Image.LANCZOS)
            shade = max(0, min(90, int(dim))) / 100
            card = Image.blend(picture, Image.new("RGB", CARD_SIZE, (0, 0, 0)), shade) if shade else picture
        except Exception:  # noqa: BLE001 - a broken stored picture falls back to the gradient
            card = None
    if card is None:
        card = gradient(DEFAULT_START if start is None else start, default_end(accent) if end is None else end,
                        gradient_kind if gradient_kind in GRADIENTS else "horizontal")
    draw = ImageDraw.Draw(card)

    left, top = 60, (height - AVATAR_SIZE) // 2
    ring = 8
    draw.ellipse([left - ring, top - ring, left + AVATAR_SIZE + ring, top + AVATAR_SIZE + ring], fill=_rgb(accent))
    mask = Image.new("L", (AVATAR_SIZE, AVATAR_SIZE), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, AVATAR_SIZE, AVATAR_SIZE], fill=255)
    avatar = None
    if avatar_png:
        try:
            avatar = Image.open(io.BytesIO(avatar_png)).convert("RGB").resize((AVATAR_SIZE, AVATAR_SIZE))
        except Exception:  # noqa: BLE001 - a broken avatar just means the plain circle
            avatar = None
    card.paste(avatar or Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), (54, 57, 63)), (left, top), mask)

    main = (255, 255, 255) if text_color is None else _rgb(text_color)
    # The small lines are the same color pulled a fifth of the way toward mid-gray, so the name leads.
    soft = tuple(int(channel + (150 - channel) * 0.22) for channel in main)
    text_left, max_width = left + AVATAR_SIZE + 50, width - (left + AVATAR_SIZE + 50) - 40
    # Everything here is whatever people chose: emoji, other scripts, fancy letters. richtext draws all of
    # those, shrinking each line so far and then cutting it short.
    if title.strip():
        richtext.fit_line(title.strip(), "impact.ttf", 44, max_width, min_size=26).draw(card, text_left, 70, align="left", fill=soft)
    richtext.fit_line(name, "arial.ttf", 76, max_width, min_size=46).draw(card, text_left, 125, align="left", fill=main)
    subtitle = f"Member #{member_number:,} of {server}" if subtitle is None else subtitle
    if subtitle.strip():
        richtext.fit_line(subtitle.strip(), "arial.ttf", 34, max_width, min_size=20).draw(card, text_left, 235, align="left", fill=soft)

    out = io.BytesIO()
    card.save(out, format="PNG", optimize=True)
    return out.getvalue()


def card_text(template: str, *, name: str, user: str, server: str, count: int) -> str:
    """Fill a card line's placeholders. Nothing can be pinged from a picture, so {mention} is the name."""
    return (template.replace("{mention}", name).replace("{name}", name).replace("{user}", user)
            .replace("{server}", server).replace("{count}", f"{count:,}"))


def card_options(storage_path: Path, guild_id: int | str, settings: dict) -> dict:
    """render_card's look for a server, from its settings and its stored background."""
    from utils.settings import get_embed_color_value, parse_hex_color

    def color(key: str) -> int | None:
        return parse_hex_color(str(settings.get(key) or ""))

    use_image = settings.get("welcome_card_background") == "image"
    return {
        "accent": color("welcome_embed_color") or get_embed_color_value(storage_path, guild_id),
        "start": color("welcome_card_color_start"),
        "end": color("welcome_card_color_end"),
        "gradient_kind": str(settings.get("welcome_card_gradient") or "horizontal"),
        "text_color": color("welcome_card_text_color"),
        "background": read_background(storage_path, guild_id) if use_image else None,
        "dim": int(settings.get("welcome_card_dim", 45) or 0),
    }
