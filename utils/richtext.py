"""Text drawing that copes with whatever people type.

Pillow draws a string with one font and shows an empty box for anything that
font lacks, which is most of what Discord users write: emoji, accents the meme
font never had, Chinese, Japanese and Korean, "fancy" Unicode letters, custom
server emoji. This module draws text as a sequence of pieces instead:

- each character goes to the first font that has it: the font the command
  asked for, then the fallbacks below;
- emoji (including flags, skin tones and joined sequences) are drawn in color
  from an emoji font;
- custom Discord emoji are drawn from their images (see `RichText.images`);
- fancy letters such as 𝓗𝓮𝓵𝓵𝓸 or ｆｕｌｌｗｉｄｔｈ become plain ones, so they
  appear in the font the command uses;
- invisible and control characters are dropped.

Wrapping and fitting work on the same pieces, so measuring always agrees with
drawing. Scripts that need shaping (Arabic, Hebrew, Indic) are kept in whole
runs and left to Pillow's shaping engine.
"""

from __future__ import annotations

import functools
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).resolve().parents[1] / "assets" / "fonts"
# Tried in order after the command's own font. DejaVu covers Latin, Greek,
# Cyrillic, Arabic, Hebrew and most symbols; Noto Sans KR covers Korean,
# Japanese and the Chinese characters Korean uses; Droid Sans Fallback picks
# up the rest of Chinese.
FALLBACK_FONTS = ("DejaVuSans-Bold.ttf", "NotoSansKR-Bold.otf", "DroidSansFallbackFull.ttf", "NotoSansThai-Bold.ttf", "NotoSansDevanagari-Bold.ttf")
# Ranges a font claims but draws as empty boxes.
BROKEN_RANGES = {"DroidSansFallbackFull.ttf": ((0x1100, 0x11FF), (0x3130, 0x318F), (0xAC00, 0xD7AF))}
EMOJI_FONT = "NotoColorEmoji.ttf"
EMOJI_STRIKE = 109  # the one size this bitmap emoji font can be opened at
ELLIPSIS = "…"

# Custom emoji stand in the text as characters from this private range, one per
# image, so the string can be cut, upper-cased and wrapped like any other.
PLACEHOLDER_BASE = 0xF0000

_ZWJ, _VS16, _KEYCAP = 0x200D, 0xFE0F, 0x20E3


@dataclass(slots=True)
class RichText:
    """Text plus the images its placeholder characters stand for."""

    text: str
    images: dict[str, Image.Image] = field(default_factory=dict)

    def upper(self) -> "RichText":
        # Styled letters (𝓼𝓬𝓻𝓲𝓹𝓽, ｗｉｄｅ) have no capitals of their own, so they are made plain first.
        plain = "".join(_plain_form(cluster) or cluster for cluster in clusters(self.text))
        return RichText(plain.upper(), self.images)

    def strip(self) -> "RichText":
        return RichText(self.text.strip(), self.images)

    def __bool__(self) -> bool:
        return bool(self.text.strip())

    def __len__(self) -> int:
        return len(self.text)

    def __getitem__(self, item: slice) -> "RichText":
        return RichText(self.text[item], self.images)

    def __add__(self, other: "RichText | str") -> "RichText":
        other = as_rich(other)
        return RichText(self.text + other.text, {**self.images, **other.images})

    def __radd__(self, other: str) -> "RichText":
        return RichText(other + self.text, self.images)


def as_rich(text: "RichText | str") -> RichText:
    return text if isinstance(text, RichText) else RichText(str(text))


# ---------- fonts ----------

@functools.lru_cache(maxsize=16)
def _coverage(name: str) -> frozenset[int] | None:
    """The code points a font has glyphs for, or None if that can't be read
    (then the font is assumed to have everything, as Pillow alone would)."""
    path = FONTS_DIR / name
    if not path.exists():
        return frozenset()
    try:
        from fontTools.ttLib import TTFont

        with TTFont(str(path), lazy=True, fontNumber=0) as font:
            return frozenset(font.getBestCmap() or {})
    except Exception:
        return None


@functools.lru_cache(maxsize=8192)
def _has(name: str, code_point: int) -> bool:
    """Whether the font really draws this character. Some fonts list a
    character and give it an empty glyph, so the outline is checked too."""
    covered = _coverage(name)
    if covered is None:
        return True
    if code_point not in covered or any(low <= code_point <= high for low, high in BROKEN_RANGES.get(name, ())):
        return False
    char = chr(code_point)
    if char.isspace():
        return True
    try:
        left, top, right, bottom = _font(name, 32).getbbox(char)
    except (AttributeError, OSError, ValueError):
        return True
    return right > left and bottom > top


@functools.lru_cache(maxsize=256)
def _font(name: str, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    try:
        return ImageFont.truetype(str(FONTS_DIR / name), size)
    except OSError:
        return ImageFont.load_default(size)


@functools.lru_cache(maxsize=1)
def _emoji_font() -> ImageFont.FreeTypeFont | None:
    try:
        return ImageFont.truetype(str(FONTS_DIR / EMOJI_FONT), EMOJI_STRIKE)
    except OSError:
        return None


@functools.lru_cache(maxsize=512)
def _emoji_tile(cluster: str) -> Image.Image | None:
    """One emoji, drawn in color at the font's native size."""
    font = _emoji_font()
    if font is None:
        return None
    left, top, right, bottom = font.getbbox(cluster)
    if right <= left or bottom <= top:
        return None
    tile = Image.new("RGBA", (right - left, bottom - top), (0, 0, 0, 0))
    ImageDraw.Draw(tile).text((-left, -top), cluster, font=font, embedded_color=True)
    return tile


# ---------- splitting text into pieces ----------

def _is_regional(code_point: int) -> bool:
    return 0x1F1E6 <= code_point <= 0x1F1FF


def _sticks_to_previous(char: str) -> bool:
    """Characters that modify the one before them and never stand alone."""
    code_point = ord(char)
    return (
        unicodedata.category(char) in ("Mn", "Me", "Mc")
        or 0xFE00 <= code_point <= 0xFE0F or 0xE0100 <= code_point <= 0xE01EF  # variation selectors
        or 0x1F3FB <= code_point <= 0x1F3FF  # skin tones
        or 0xE0020 <= code_point <= 0xE007F  # tags (subdivision flags)
        or code_point in (_KEYCAP, 0x200C)
    )


def clusters(text: str) -> list[str]:
    """Split into what a reader sees as single characters: a letter with its
    accents, a whole flag, a family emoji joined by invisible joiners."""
    result: list[str] = []
    index, length = 0, len(text)
    while index < length:
        char = text[index]
        end = index + 1
        if _is_regional(ord(char)) and end < length and _is_regional(ord(text[end])):
            end += 1
        while end < length:
            if _sticks_to_previous(text[end]):
                end += 1
            elif ord(text[end]) == _ZWJ and end + 1 < length:
                end += 2
            else:
                break
        result.append(text[index:end])
        index = end
    return result


def _is_emoji(cluster: str, primary: str) -> bool:
    emoji = _coverage(EMOJI_FONT)
    if not emoji:
        return False
    first = ord(cluster[0])
    points = [ord(char) for char in cluster]
    if _KEYCAP in points:
        return True
    if first < 0x80 or first not in emoji:
        return False
    if first >= 0x1F000 or _VS16 in points or _ZWJ in points or len(cluster) > 1:
        return True
    # A symbol that can be either (☺, ❤, ™): emoji only if no text font draws it.
    return not any(_has(name, first) for name in (primary, *FALLBACK_FONTS))


def _breaks_anywhere(char: str) -> bool:
    """Chinese, Japanese and Korean have no spaces: a line can end after any character."""
    code_point = ord(char)
    return (
        0x2E80 <= code_point <= 0x9FFF or 0xAC00 <= code_point <= 0xD7AF or 0xF900 <= code_point <= 0xFAFF
        or 0xFF00 <= code_point <= 0xFFEF or 0x20000 <= code_point <= 0x2FA1F
    )


def _plain_form(cluster: str) -> str | None:
    """The ordinary letters behind "styled" Unicode ones (math alphabets,
    full-width, circled), or None if the cluster is not that kind."""
    if not any(unicodedata.decomposition(char).startswith(("<font>", "<wide>", "<narrow>", "<circle>", "<square>")) for char in cluster):
        return None
    plain = unicodedata.normalize("NFKC", cluster)
    return plain if plain != cluster else None


@dataclass(slots=True)
class Piece:
    kind: str  # "text", "emoji" or "image"
    text: str
    font: str = ""  # for "text": the font file that draws it
    breakable: bool = False  # a line may end right after this piece


def _font_for(cluster: str, stack: tuple[str, ...]) -> str | None:
    first = ord(cluster[0])
    return next((name for name in stack if _has(name, first)), None)


@functools.lru_cache(maxsize=256)
def _parse(text: str, primary: str, image_keys: frozenset[str]) -> tuple[tuple[tuple[Piece, ...], ...], ...]:
    """Paragraphs of words of pieces. Size does not matter here, so one parse
    serves every size a fit tries."""
    stack = (primary, *FALLBACK_FONTS)
    paragraphs: list[tuple[tuple[Piece, ...], ...]] = []
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        words: list[list[Piece]] = []
        current: list[Piece] = []

        def end_word() -> None:
            nonlocal current
            if current:
                words.append(current)
                current = []

        def add_text(cluster: str, font: str) -> None:
            if current and current[-1].kind == "text" and current[-1].font == font and not current[-1].breakable:
                current[-1].text += cluster
            else:
                current.append(Piece("text", cluster, font))
            if _breaks_anywhere(cluster[0]):
                current[-1].breakable = True

        for cluster in clusters(paragraph):
            if cluster in image_keys:
                current.append(Piece("image", cluster))
                continue
            if cluster.isspace():
                end_word()
                continue
            if _is_emoji(cluster, primary):
                current.append(Piece("emoji", cluster))
                continue
            plain = _plain_form(cluster)
            font = _font_for(cluster, stack)
            if plain is not None and (font is None or font != primary) and all(_has(primary, ord(char)) for char in plain):
                add_text(plain, primary)  # fancy letters: show them in the command's own font
            elif font is not None:
                add_text(cluster, font)
            elif plain is not None and all(_font_for(char, stack) for char in plain):
                for char in plain:
                    add_text(char, _font_for(char, stack))
            elif unicodedata.category(cluster[0]) in ("Cc", "Cf", "Cn", "Co", "Cs", "Zl", "Zp"):
                continue  # invisible or meaningless here: draw nothing rather than a box
            else:
                add_text("\N{WHITE SQUARE}", FALLBACK_FONTS[0])  # a real character no bundled font has
        end_word()
        paragraphs.append(tuple(tuple(word) for word in words))
    return tuple(paragraphs)


# ---------- layout ----------

@dataclass(slots=True)
class Placed:
    piece: Piece
    x: float
    width: float


@dataclass(slots=True)
class Line:
    items: list[Placed]
    width: float


@dataclass(slots=True)
class Layout:
    """Text arranged at one size, ready to draw."""

    lines: list[Line]
    primary: str
    size: int
    width: int
    height: int
    line_height: int
    gap: int
    ascent: int
    stroke: int
    images: dict[str, Image.Image]

    def draw(
        self,
        image: Image.Image,
        left: int,
        top: int,
        box_width: int | None = None,
        *,
        fill=(255, 255, 255, 255),
        stroke_fill=(0, 0, 0, 255),
        align: str = "center",
        shadow: tuple[int, tuple] | None = None,
    ) -> None:
        """Draw onto an RGBA image with the block's top-left corner at
        (left, top). Lines are aligned inside `box_width` (default: the block's
        own width). `shadow` is (offset in pixels, color): a copy of the letters
        nudged toward each corner, drawn underneath."""
        box_width = self.width if box_width is None else box_width
        draw = ImageDraw.Draw(image)
        em = self.emoji_size

        def origin(line: Line, row: int) -> tuple[float, float]:
            slack = box_width - line.width - self.stroke * 2
            offset = {"left": 0.0, "right": slack}.get(align, slack / 2.0)
            return left + self.stroke + offset, top + self.stroke + row * (self.line_height + self.gap)

        def letters(dx: int, dy: int, color, stroke: int, stroke_color) -> None:
            for row, line in enumerate(self.lines):
                x0, y0 = origin(line, row)
                for item in line.items:
                    if item.piece.kind == "text":
                        draw.text((x0 + item.x + dx, y0 + self.ascent + dy), item.piece.text, font=_font(item.piece.font, self.size),
                                  fill=color, anchor="ls", stroke_width=stroke, stroke_fill=stroke_color)

        if shadow:
            distance, shadow_color = shadow
            for dx, dy in ((-distance, -distance), (distance, distance), (distance, -distance), (-distance, distance)):
                letters(dx, dy, shadow_color, 0, None)
        if self.stroke:
            letters(0, 0, stroke_fill, self.stroke, stroke_fill)  # every outline first, so none covers a neighbor's letter
        letters(0, 0, fill, 0, None)

        for row, line in enumerate(self.lines):
            x0, y0 = origin(line, row)
            for item in line.items:
                if item.piece.kind == "text":
                    continue
                picture = self.images.get(item.piece.text) if item.piece.kind == "image" else _emoji_tile(item.piece.text)
                if picture is None:
                    continue
                scale = min(em / picture.width, em / picture.height)
                sized = picture.resize((max(1, round(picture.width * scale)), max(1, round(picture.height * scale))), Image.LANCZOS)
                x = round(x0 + item.x + (item.width - sized.width) / 2)
                y = round(y0 + (self.line_height - sized.height) / 2)
                image.paste(sized, (x, y), sized)

    @property
    def emoji_size(self) -> int:
        return max(1, round(self.size * 1.05))


def _is_rtl(pieces: list[Piece]) -> bool:
    right = left = 0
    for piece in pieces:
        if piece.kind != "text":
            continue
        for char in piece.text:
            direction = unicodedata.bidirectional(char)
            if direction in ("R", "AL"):
                right += 1
            elif direction == "L":
                left += 1
    return right > left


def layout(text: "RichText | str", primary: str, size: int, max_width: int | None = None, *, stroke: int = 0, gap: int | None = None) -> Layout:
    """Arrange the text at `size`, wrapping at `max_width` if given. A word
    wider than a whole line is broken where it has to be."""
    rich = as_rich(text)
    paragraphs = _parse(rich.text, primary, frozenset(rich.images))
    main = _font(primary, size)
    try:
        ascent, descent = main.getmetrics()
    except AttributeError:  # Pillow's built-in bitmap font
        ascent, descent = size, max(1, size // 4)
    line_height = ascent + descent
    gap = max(0, round(size * 0.08)) if gap is None else gap
    space = main.getlength(" ")
    emoji_width = round(size * 1.05) + max(1, size // 12)
    room = None if max_width is None else max(1.0, float(max_width - stroke * 2))

    def width_of(piece: Piece) -> float:
        return _font(piece.font, size).getlength(piece.text) if piece.kind == "text" else float(emoji_width)

    def split(piece: Piece) -> list[Piece]:
        """A text piece cut into pieces that each fit on a line."""
        parts: list[Piece] = []
        current = ""
        font = _font(piece.font, size)
        for cluster in clusters(piece.text):
            if current and font.getlength(current + cluster) > room:
                parts.append(Piece("text", current, piece.font, True))
                current = cluster
            else:
                current += cluster
        if current:
            parts.append(Piece("text", current, piece.font, piece.breakable))
        return parts

    lines: list[Line] = []
    for words in paragraphs:
        row: list[tuple[Piece, float, bool]] = []  # piece, width, whether a space comes before it
        used = 0.0

        def flush() -> None:
            nonlocal row, used
            pieces = [piece for piece, _, _ in row]
            ordered = row[::-1] if _is_rtl(pieces) else row
            items: list[Placed] = []
            x = 0.0
            for position, (piece, width, _) in enumerate(ordered):
                gap_before = row[len(row) - position][2] if ordered is not row and position else (ordered[position][2] if position else False)
                if gap_before:
                    x += space
                items.append(Placed(piece, x, width))
                x += width
            lines.append(Line(items, x))
            row, used = [], 0.0

        for word in words:
            # Units that have to stay together: a run of pieces up to and including one a line may end after.
            units: list[list[Piece]] = [[]]
            for piece in word:
                parts = split(piece) if room is not None and piece.kind == "text" and width_of(piece) > room else [piece]
                for part in parts:
                    units[-1].append(part)
                    if part.breakable:
                        units.append([])
            units = [unit for unit in units if unit]
            for number, unit in enumerate(units):
                unit_width = sum(width_of(piece) for piece in unit)
                lead = space if number == 0 and row else 0.0
                if room is not None and row and used + lead + unit_width > room:
                    flush()
                    lead = 0.0
                for position, piece in enumerate(unit):
                    row.append((piece, width_of(piece), bool(lead) and position == 0))
                used += lead + unit_width
        flush()

    width = round(max((line.width for line in lines), default=0.0)) + stroke * 2
    height = len(lines) * line_height + max(0, len(lines) - 1) * gap + stroke * 2
    return Layout(lines, primary, size, width, height, line_height, gap, ascent, stroke, rich.images)


def fit(text: "RichText | str", primary: str, max_width: int, max_height: int, *, min_size: int = 10, max_size: int = 200,
        stroke: int = 0, gap: int | None = None) -> Layout:
    """The largest size at which the wrapped text fits the box. If it can't
    fit even at `min_size`, the `min_size` layout is returned anyway."""
    max_width, max_height = max(1, max_width), max(1, max_height)
    min_size = max(1, min_size)
    max_size = max(min_size, max_size)
    best = layout(text, primary, min_size, max_width, stroke=stroke, gap=gap)
    low, high = min_size + 1, max_size
    while low <= high:
        size = (low + high) // 2
        candidate = layout(text, primary, size, max_width, stroke=stroke, gap=gap)
        if candidate.width <= max_width and candidate.height <= max_height:
            best = candidate
            low = size + 1
        else:
            high = size - 1
    return best


def fit_line(text: "RichText | str", primary: str, size: int, max_width: int, *, min_size: int | None = None) -> Layout:
    """One line: shrunk down to `min_size` if it has to be, then cut short
    with an ellipsis if it still doesn't fit."""
    rich = as_rich(text)
    rich = RichText(" ".join(rich.text.split()), rich.images)
    floor = max(1, min_size if min_size is not None else size)
    for candidate in range(size, floor - 1, -2):
        arranged = layout(rich, primary, candidate)
        if arranged.width <= max_width:
            return arranged
    kept = clusters(rich.text)
    while len(kept) > 1:
        kept.pop()
        arranged = layout(RichText("".join(kept).rstrip() + ELLIPSIS, rich.images), primary, floor)
        if arranged.width <= max_width:
            return arranged
    return layout(RichText("".join(kept), rich.images), primary, floor)


def draw_in_box(image: Image.Image, box: tuple[int, int, int, int], text: "RichText | str", primary: str, *, fill, max_size: int = 200,
                min_size: int = 10, stroke: int = 0, stroke_fill=(0, 0, 0, 255), gap: int | None = None) -> Layout:
    """Fit the text to the box and draw it centered there, both ways."""
    left, top, right, bottom = box
    arranged = fit(text, primary, right - left, bottom - top, min_size=min_size, max_size=max_size, stroke=stroke, gap=gap)
    arranged.draw(image, left, top + max(0, (bottom - top - arranged.height) // 2), right - left, fill=fill, stroke_fill=stroke_fill)
    return arranged
