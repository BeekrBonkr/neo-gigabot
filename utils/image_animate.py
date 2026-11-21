"""Animations for the /animate commands: stills turned into GIFs, and tools
for GIFs people already have. Functions take a decoded `Frames` and return
(frames, durations) ready for `encode_frames`."""

from __future__ import annotations

import math
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

from utils.image_filters import hue_shift
from utils.imaging import MAX_GIF_FRAMES, MIN_GIF_FRAME_MS, VIDEO_MAX_FRAMES, Frames, frame_budget, load_font, thin_frames

# Stills are shrunk to this before being animated: a GIF stores every frame
# whole, so size is what decides whether the result fits Discord's upload cap.
STILL_MAX_EDGE = 320
GIF_MAX_EDGE = 480
STILL_FRAMES = 18

NEEDS_GIF = "That's a still image. This command needs a GIF."


def _cycle(loaded: Frames) -> list[tuple[Image.Image, float]]:
    """Each output frame with its position (0..1) in one loop: a GIF's own
    frames, or STILL_FRAMES copies of a still."""
    if loaded.animated:
        count = len(loaded.frames)
        return [(frame, index / count) for index, frame in enumerate(loaded.frames)]
    return [(loaded.frames[0], index / STILL_FRAMES) for index in range(STILL_FRAMES)]


def _durations(loaded: Frames, speed_ms: int) -> list[int]:
    return loaded.durations if loaded.animated else [speed_ms] * STILL_FRAMES


def _steps(loaded: Frames, step_ms: list[int]) -> list[tuple[Image.Image, int, int]]:
    """For an effect that is a short loop of its own (a pat, a bonk): each
    frame of the result as (the picture to use, which step of the effect, how
    long it shows). A still gets one pass of the effect. A GIF or a video
    keeps playing underneath while the effect repeats, until the input has
    played through and the effect is back at its start."""
    count = len(step_ms)
    if not loaded.animated:
        return [(loaded.frames[0], step, duration) for step, duration in enumerate(step_ms)]
    total = max(1, sum(loaded.durations))
    steps: list[tuple[Image.Image, int, int]] = []
    clock = 0
    # These are small pictures, so a video may have twice the usual frames. If the input
    # still outlasts them, the effect is slowed just enough to reach its end.
    most = frame_budget(MAX_GIF_FRAMES, VIDEO_MAX_FRAMES * 2)
    loops = max(1, most // count)
    stretch = total / (loops * sum(step_ms))
    if stretch > 1:
        step_ms = [math.ceil(duration * stretch) for duration in step_ms]
    while (clock < total or len(steps) % count) and len(steps) + (count - len(steps) % count) % count < most + 1:
        at, picture = clock % total, loaded.frames[-1]
        for frame, duration in zip(loaded.frames, loaded.durations):
            if at < duration:
                picture = frame
                break
            at -= duration
        step = len(steps) % count
        steps.append((picture, step, step_ms[step]))
        clock += step_ms[step]
    return steps


def spin(loaded: Frames, speed_ms: int = 50, clockwise: bool = True) -> tuple[list[Image.Image], list[int]]:
    """The picture, cropped to a circle, turning once per loop."""
    frames = []
    for frame, position in _cycle(loaded):
        side = min(frame.size)
        left, top = (frame.width - side) // 2, (frame.height - side) // 2
        square = frame.crop((left, top, left + side, top + side))
        angle = 360.0 * position * (-1 if clockwise else 1)
        turned = square.rotate(angle, resample=Image.BICUBIC)
        mask = Image.new("L", (side * 2, side * 2), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, side * 2 - 1, side * 2 - 1), fill=255)
        alpha = Image.composite(turned.getchannel("A"), Image.new("L", (side, side), 0), mask.resize((side, side), Image.LANCZOS))
        turned.putalpha(alpha)
        frames.append(turned)
    return frames, _durations(loaded, speed_ms)


def rainbow(loaded: Frames, speed_ms: int = 60) -> tuple[list[Image.Image], list[int]]:
    """Colors travel once around the color wheel per loop."""
    frames = [frame if position == 0 else hue_shift(frame, 360.0 * position) for frame, position in _cycle(loaded)]
    return frames, _durations(loaded, speed_ms)


def wave(loaded: Frames, speed_ms: int = 50, strength: int = 5) -> tuple[list[Image.Image], list[int]]:
    """Rows slide left and right in a ripple that travels down the picture.
    `strength` is the sideways travel as a percent of the width."""
    width, height = loaded.size
    amplitude = width * strength / 100.0
    rows = np.arange(height, dtype=np.float32)[:, None]
    columns = np.tile(np.arange(width, dtype=np.float32), (height, 1))
    map_y = np.tile(rows, (1, width))
    frames = []
    for frame, position in _cycle(loaded):
        offset = amplitude * np.sin(2 * math.pi * (rows / max(1.0, height / 2.0) + position))
        warped = cv2.remap(np.asarray(frame), columns + offset.astype(np.float32), map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        frames.append(Image.fromarray(warped, "RGBA"))
    return frames, _durations(loaded, speed_ms)


def zoom(loaded: Frames, speed_ms: int = 50, amount: int = 40) -> tuple[list[Image.Image], list[int]]:
    """Pulse in toward the center and back out. `amount` is the extra zoom at the peak, in percent."""
    frames = []
    for frame, position in _cycle(loaded):
        scale = 1.0 + (amount / 100.0) * (0.5 - 0.5 * math.cos(2 * math.pi * position))
        crop_w, crop_h = frame.width / scale, frame.height / scale
        left, top = (frame.width - crop_w) / 2, (frame.height - crop_h) / 2
        frames.append(frame.resize(frame.size, Image.BICUBIC, box=(left, top, left + crop_w, top + crop_h)))
    return frames, _durations(loaded, speed_ms)


# ---------- pet ----------

# The hand is the sprite sheet from the original petpet generator
# (benisland.neocities.org/petpet): five 112px frames side by side. The numbers
# below are that generator's defaults, so the result matches the usual petpet GIF.
PET_SPRITE = Path(__file__).resolve().parents[1] / "assets" / "images" / "petpet.png"
PET_CELL = 112
PET_SCALE = 2  # drawn at twice the original size so avatars stay sharp
PET_SIZE = PET_CELL * PET_SCALE
PET_FRAMES = 5
# Where the picture sits in each frame, in the original's pixels: (left, top, width, height).
PET_BOXES = ((14, 20, 98, 98), (12, 33, 102, 84), (8, 40, 111, 78), (10, 33, 102, 84), (12, 20, 98, 98))
PET_HAND_DROP = (0, 4, 9, 4, 0)  # how far the hand comes down in each frame


@lru_cache(maxsize=1)
def _pet_hands() -> list[Image.Image]:
    sheet = Image.open(PET_SPRITE).convert("RGBA")
    return [sheet.crop((index * PET_CELL, 0, (index + 1) * PET_CELL, PET_CELL)).resize((PET_SIZE, PET_SIZE), Image.LANCZOS)
            for index in range(PET_FRAMES)]


def pet(loaded: Frames, speed_ms: int = 60) -> tuple[list[Image.Image], list[int]]:
    """The petpet GIF: a hand pats the picture, which squashes under each pat."""
    hands = _pet_hands()
    frames, durations = [], []
    for base, step, duration in _steps(loaded, [speed_ms] * PET_FRAMES):
        left, top, width, height = PET_BOXES[step]
        canvas = Image.new("RGBA", (PET_SIZE, PET_SIZE), (0, 0, 0, 0))
        squashed = base.resize((width * PET_SCALE, height * PET_SCALE), Image.LANCZOS)
        canvas.alpha_composite(squashed, (left * PET_SCALE, top * PET_SCALE))
        canvas.alpha_composite(hands[step], (0, PET_HAND_DROP[step] * PET_SCALE))
        frames.append(canvas)
        durations.append(duration)
    return frames, durations


# ---------- triggered ----------

TRIGGERED_LABEL = Path(__file__).resolve().parents[1] / "assets" / "images" / "triggered.png"
TRIGGERED_SIZE = 256
TRIGGERED_LABEL_HEIGHT = 56
TRIGGERED_MARGIN = 12  # spare picture around the edge, so shaking never shows a gap
# How far the picture jumps in each frame. The label jumps too, out of step.
TRIGGERED_SHAKES = ((0, 0), (-9, 6), (8, -7), (-6, -9), (10, 5), (-10, -4), (6, 9), (-4, 8))


def triggered(loaded: Frames, speed_ms: int = 50) -> tuple[list[Image.Image], list[int]]:
    """The picture shakes under a red cast, over the TRIGGERED label."""
    margin, size = TRIGGERED_MARGIN, TRIGGERED_SIZE
    side = size + margin * 2
    red = Image.new("RGBA", (side, side), (255, 40, 0, 255))
    label = Image.open(TRIGGERED_LABEL).convert("RGBA").resize((side, TRIGGERED_LABEL_HEIGHT + margin), Image.LANCZOS)
    tinted: dict[int, Image.Image] = {}  # a still is tinted once, a GIF once per frame it has
    frames, durations = [], []
    for picture, index, duration in _steps(loaded, [speed_ms] * len(TRIGGERED_SHAKES)):
        base = tinted.get(id(picture))
        if base is None:
            base = Image.new("RGBA", (side, side), (255, 255, 255, 255))
            base.alpha_composite(ImageOps.fit(picture, (side, side), Image.LANCZOS))
            base = tinted[id(picture)] = Image.blend(base, red, 0.22)
        dx, dy = TRIGGERED_SHAKES[index]
        frame = base.crop((margin + dx, margin + dy, margin + dx + size, margin + dy + size))
        label_dx, label_dy = TRIGGERED_SHAKES[(index + 3) % len(TRIGGERED_SHAKES)]
        frame.paste(label, (label_dx // 3 - margin, size - TRIGGERED_LABEL_HEIGHT + label_dy // 2))
        frames.append(frame)
        durations.append(duration)
    return frames, durations


# ---------- bonk ----------

BONK_SCENE = Path(__file__).resolve().parents[1] / "assets" / "images" / "bonk.jpg"  # the dog, without its bat or the dog it hits
BONK_TARGET_BOX = (301, 164, 461, 324)  # where the picture stands
BONK_PIVOT = (225, 282)  # the paw holding the bat
BONK_BAT_LENGTH = 250
BONK_HIT_ANGLE = -58  # the bat as it lands, in degrees counterclockwise from straight up
BONK_LABEL_AT = (232, 4)
# How far the bat is lifted off the picture in each frame, in degrees:
# wind back, swing, hit and hold, lift away.
BONK_LIFTS = (43, 51, 51, 31, 11, 0, 0, 0, 9, 23, 37)
BONK_HELD = BONK_LIFTS.index(0) + 2  # the frame that lingers on the hit


@lru_cache(maxsize=1)
def _bat_sprite(length: int) -> Image.Image:
    """A baseball bat standing on its handle, on a square sprite whose center
    is the end of the handle, so rotating the sprite swings the bat."""
    scale = 4
    side = length * 2 * scale
    sprite = Image.new("RGBA", (side, side), (0, 0, 0, 0))
    draw = ImageDraw.Draw(sprite)
    cx, cy, reach = side // 2, side // 2, length * scale
    outline, wood, grip, line = (48, 30, 18, 255), (196, 140, 80, 255), (120, 80, 44, 255), 3 * scale
    handle, barrel = 5 * scale, 15 * scale
    top = cy - reach
    draw.polygon([(cx - handle, cy), (cx - barrel, top + barrel * 3), (cx - barrel, top + barrel), (cx + barrel, top + barrel),
                  (cx + barrel, top + barrel * 3), (cx + handle, cy)], fill=wood, outline=outline, width=line)
    draw.ellipse((cx - barrel, top, cx + barrel, top + barrel * 2), fill=wood, outline=outline, width=line)
    draw.rectangle((cx - barrel + line, top + barrel, cx + barrel - line, top + barrel + line * 2), fill=wood)
    draw.polygon([(cx - handle, cy), (cx - handle - scale, cy - reach * 0.28), (cx + handle + scale, cy - reach * 0.28), (cx + handle, cy)],
                 fill=grip, outline=outline, width=line)
    draw.ellipse((cx - handle * 2, cy - handle, cx + handle * 2, cy + handle), fill=grip, outline=outline, width=line)
    return sprite.resize((length * 2, length * 2), Image.LANCZOS)


@lru_cache(maxsize=1)
def _bonk_label() -> Image.Image:
    label = Image.new("RGBA", (220, 90), (0, 0, 0, 0))
    ImageDraw.Draw(label).text((110, 45), "BONK!", font=load_font("impact.ttf", 54), anchor="mm", fill=(255, 221, 51, 255),
                               stroke_width=4, stroke_fill=(30, 20, 20, 255))
    return label.rotate(12, resample=Image.BICUBIC, expand=True)


def bonk(loaded: Frames, speed_ms: int = 50) -> tuple[list[Image.Image], list[int]]:
    """The bonk meme: the dog brings a bat down on the picture, which squashes when hit."""
    scene, bat, label = Image.open(BONK_SCENE).convert("RGBA"), _bat_sprite(BONK_BAT_LENGTH), _bonk_label()
    left, top, right, bottom = BONK_TARGET_BOX
    box_w, box_h = right - left, bottom - top
    scale = min(box_w / loaded.size[0], box_h / loaded.size[1])
    width, height = max(1, int(loaded.size[0] * scale)), max(1, int(loaded.size[1] * scale))
    frames, durations = [], []
    step_ms = [speed_ms * (4 if index == BONK_HELD else 1) for index in range(len(BONK_LIFTS))]
    for base, index, duration in _steps(loaded, step_ms):
        lift = BONK_LIFTS[index]
        hit = lift == 0
        canvas = scene.copy()
        shown = base.resize((max(1, int(width * 1.1)), max(1, int(height * 0.75))) if hit else (width, height), Image.LANCZOS)
        canvas.paste(shown, (left + (box_w - shown.width) // 2, bottom - shown.height), shown)
        swung = bat.rotate(BONK_HIT_ANGLE + lift, resample=Image.BICUBIC)
        canvas.paste(swung, (BONK_PIVOT[0] - swung.width // 2, BONK_PIVOT[1] - swung.height // 2), swung)
        if hit:
            canvas.paste(label, BONK_LABEL_AT, label)
        frames.append(canvas)
        durations.append(duration)
    return frames, durations


# ---------- boom ----------

# The explosion is the second half of the "exploding cat" GIF on Klipy: 22
# frames that open on the flash, kept as a sheet of 480px cells.
BOOM_SHEET = Path(__file__).resolve().parents[1] / "assets" / "images" / "boom.jpg"
BOOM_CELL = 480
BOOM_COLUMNS = 6
BOOM_FRAMES = 22
BOOM_FRAME_MS = 40
BOOM_SWELL_FRAMES = 8  # how many frames a still takes to swell; a short GIF repeats until it has this many
BOOM_BULGE = 1.6  # how hard the middle is pushed out at the end
BOOM_ZOOM = 0.3  # how much closer the whole picture has come by the end
BOOM_CURVE = (0.6, 0.0, 0.9, 0.35)  # the swell's easing, as a CSS cubic-bezier: barely moving, then all at once


@lru_cache(maxsize=1)
def _boom_cells() -> list[Image.Image]:
    sheet = Image.open(BOOM_SHEET).convert("RGBA")
    return [sheet.crop((column * BOOM_CELL, row * BOOM_CELL, (column + 1) * BOOM_CELL, (row + 1) * BOOM_CELL))
            for row, column in (divmod(index, BOOM_COLUMNS) for index in range(BOOM_FRAMES))]


def _bezier_ease(progress: float, curve: tuple[float, float, float, float] = BOOM_CURVE) -> float:
    """How far along an eased move is at `progress` (0..1) through its time,
    for the cubic bezier from (0, 0) to (1, 1) with these two control points."""
    x1, y1, x2, y2 = curve

    def along(a: float, b: float, t: float) -> float:
        return 3 * a * t * (1 - t) ** 2 + 3 * b * t * t * (1 - t) + t ** 3

    low, high = 0.0, 1.0
    for _ in range(30):  # the curve's x only ever rises, so halving finds the point at this time
        middle = (low + high) / 2
        if along(x1, x2, middle) < progress:
            low = middle
        else:
            high = middle
    return along(y1, y2, (low + high) / 2)


def _bulge(frame: Image.Image, amount: float) -> Image.Image:
    """The picture swollen from its center, as if something behind it is
    pushing out. `amount` runs from 0 (untouched) to 1."""
    if amount <= 0:
        return frame
    width, height = frame.size
    across = (np.arange(width, dtype=np.float32) - (width - 1) / 2) / (width / 2)
    down = (np.arange(height, dtype=np.float32) - (height - 1) / 2) / (height / 2)
    x, y = np.meshgrid(across, down)
    radius = np.sqrt(x * x + y * y)
    # inside the circle each point shows what was nearer the center, so the middle grows
    pull = np.where(radius < 1.0, np.power(np.maximum(radius, 1e-6), BOOM_BULGE * amount), 1.0) / (1.0 + BOOM_ZOOM * amount)
    map_x = (x * pull * (width / 2) + (width - 1) / 2).astype(np.float32)
    map_y = (y * pull * (height / 2) + (height - 1) / 2).astype(np.float32)
    return Image.fromarray(cv2.remap(np.asarray(frame), map_x, map_y, cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE), "RGBA")


def boom(loaded: Frames, speed_ms: int = 40) -> tuple[list[Image.Image], list[int]]:
    """The picture swells, slowly and then all at once, and goes up in a
    nuclear explosion. A GIF swells over its own frames, playing at its own speed."""
    if loaded.animated:
        repeats = -(-BOOM_SWELL_FRAMES // len(loaded.frames))
        sources, durations = loaded.frames * repeats, list(loaded.durations) * repeats
    else:
        sources, durations = [loaded.frames[0]] * BOOM_SWELL_FRAMES, [speed_ms] * BOOM_SWELL_FRAMES
    count = len(sources)
    frames = [_bulge(frame, _bezier_ease(index / (count - 1))) for index, frame in enumerate(sources)]
    frames.extend(ImageOps.fit(cell, loaded.size, Image.LANCZOS) for cell in _boom_cells())
    return frames, durations + [BOOM_FRAME_MS] * BOOM_FRAMES


# ---------- tools for existing GIFs ----------

def speed(loaded: Frames, factor: float) -> tuple[list[Image.Image], list[int]]:
    """Play `factor` times as fast (below 1 is slower). GIF frames can't be
    shorter than MIN_GIF_FRAME_MS, so past that point frames are dropped."""
    if not loaded.animated:
        raise ValueError(NEEDS_GIF)
    frames: list[Image.Image] = []
    durations: list[int] = []
    owed = 0.0  # time of dropped frames, given to the frame kept before them
    for frame, duration in zip(loaded.frames, loaded.durations):
        scaled = max(1.0, duration) / factor
        if frames and durations[-1] + owed < MIN_GIF_FRAME_MS:
            owed += scaled
            continue
        if frames:
            durations[-1] = int(round(durations[-1] + owed))
        owed = 0.0
        frames.append(frame)
        durations.append(scaled)
    durations[-1] = int(round(durations[-1] + owed))
    durations = [max(MIN_GIF_FRAME_MS, int(round(d))) for d in durations]
    if len(frames) < 2:
        raise ValueError("That would leave a single frame. Try a smaller speed-up.")
    return frames, durations


def reverse(loaded: Frames) -> tuple[list[Image.Image], list[int]]:
    if not loaded.animated:
        raise ValueError(NEEDS_GIF)
    return loaded.frames[::-1], loaded.durations[::-1]


def pick_frame(loaded: Frames, number: int) -> tuple[Image.Image, str]:
    """Frame `number` (counting from 1) and a line describing the GIF."""
    if not loaded.animated:
        raise ValueError(NEEDS_GIF)
    count = len(loaded.frames)
    if number > count:
        raise ValueError(f"That GIF has {count} frames, so there is no frame {number}.")
    seconds = sum(loaded.durations) / 1000.0
    width, height = loaded.size
    return loaded.frames[number - 1], f"Frame {number} of {count} · {width}x{height} · {seconds:.1f}s per loop"


def join(pictures: list[Frames], delay_ms: int) -> tuple[list[Image.Image], list[int]]:
    """Several pictures one after another as one GIF, each centered on a
    canvas the size of the first. A still shows for `delay_ms`; a GIF or a
    video plays through at its own pace. When there is more than a GIF can
    reasonably hold, every clip gives up frames evenly."""
    width, height = pictures[0].size
    share = max(1, MAX_GIF_FRAMES // len(pictures))
    frames, durations = [], []
    for picture in pictures:
        if picture.animated:
            shown, times = thin_frames(picture.frames, picture.durations, share)
        else:
            shown, times = picture.frames[:1], [delay_ms]
        for still, time in zip(shown, times):
            scale = min(width / still.width, height / still.height)
            fitted = still.resize((max(1, int(still.width * scale)), max(1, int(still.height * scale))), Image.LANCZOS)
            canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
            canvas.alpha_composite(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
            frames.append(canvas)
            durations.append(time)
    return frames, durations
