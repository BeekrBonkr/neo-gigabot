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
from utils.imaging import MIN_GIF_FRAME_MS, Frames, load_font

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
    base = loaded.frames[0]
    frames = []
    for (left, top, width, height), drop, hand in zip(PET_BOXES, PET_HAND_DROP, _pet_hands()):
        canvas = Image.new("RGBA", (PET_SIZE, PET_SIZE), (0, 0, 0, 0))
        squashed = base.resize((width * PET_SCALE, height * PET_SCALE), Image.LANCZOS)
        canvas.alpha_composite(squashed, (left * PET_SCALE, top * PET_SCALE))
        canvas.alpha_composite(hand, (0, drop * PET_SCALE))
        frames.append(canvas)
    return frames, [speed_ms] * PET_FRAMES


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
    base = Image.new("RGBA", (side, side), (255, 255, 255, 255))
    base.alpha_composite(ImageOps.fit(loaded.frames[0], (side, side), Image.LANCZOS))
    base = Image.blend(base, Image.new("RGBA", base.size, (255, 40, 0, 255)), 0.22)
    label = Image.open(TRIGGERED_LABEL).convert("RGBA").resize((side, TRIGGERED_LABEL_HEIGHT + margin), Image.LANCZOS)
    frames = []
    for index, (dx, dy) in enumerate(TRIGGERED_SHAKES):
        frame = base.crop((margin + dx, margin + dy, margin + dx + size, margin + dy + size))
        label_dx, label_dy = TRIGGERED_SHAKES[(index + 3) % len(TRIGGERED_SHAKES)]
        frame.paste(label, (label_dx // 3 - margin, size - TRIGGERED_LABEL_HEIGHT + label_dy // 2))
        frames.append(frame)
    return frames, [speed_ms] * len(frames)


# ---------- bonk ----------

BONK_CANVAS = (340, 330)
BONK_TARGET_BOX = (15, 175, 185, 325)  # where the picture stands
BONK_PIVOT = (286, 226)  # the hand holding the bat
BONK_BAT_LENGTH = 190
# The bat's angle in each frame, in degrees counterclockwise from straight up:
# wind back, swing, hit and hold, lift away.
BONK_ANGLES = (-6, -12, -12, 14, 50, 76, 76, 76, 60, 30, 6)
BONK_HIT_ANGLE = 76


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


def bonk(loaded: Frames, speed_ms: int = 50) -> tuple[list[Image.Image], list[int]]:
    """A bat comes down on the picture, which squashes when hit."""
    base = loaded.frames[0]
    left, top, right, bottom = BONK_TARGET_BOX
    box_w, box_h = right - left, bottom - top
    scale = min(box_w / base.width, box_h / base.height)
    target = base.resize((max(1, int(base.width * scale)), max(1, int(base.height * scale))), Image.LANCZOS)
    bat = _bat_sprite(BONK_BAT_LENGTH)
    font = load_font("impact.ttf", 54)
    frames = []
    for angle in BONK_ANGLES:
        hit = angle >= BONK_HIT_ANGLE
        canvas = Image.new("RGBA", BONK_CANVAS, (0, 0, 0, 0))
        shown = target.resize((int(target.width * 1.12), int(target.height * 0.68)), Image.LANCZOS) if hit else target
        canvas.alpha_composite(shown, (left + (box_w - shown.width) // 2, bottom - shown.height))
        swung = bat.rotate(angle, resample=Image.BICUBIC)
        canvas.alpha_composite(swung, (BONK_PIVOT[0] - swung.width // 2, BONK_PIVOT[1] - swung.height // 2),
                               (0, 0, swung.width, min(swung.height, BONK_CANVAS[1] - (BONK_PIVOT[1] - swung.height // 2))))
        if hit:
            label = Image.new("RGBA", (220, 90), (0, 0, 0, 0))
            ImageDraw.Draw(label).text((110, 45), "BONK!", font=font, anchor="mm", fill=(255, 221, 51, 255), stroke_width=4, stroke_fill=(30, 20, 20, 255))
            canvas.alpha_composite(label.rotate(12, resample=Image.BICUBIC, expand=True), (4, 40))
        frames.append(canvas)
    durations = [speed_ms * (4 if angle >= BONK_HIT_ANGLE and index == BONK_ANGLES.index(BONK_HIT_ANGLE) + 2 else 1)
                 for index, angle in enumerate(BONK_ANGLES)]
    return frames, durations


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


def join(stills: list[Image.Image], delay_ms: int) -> tuple[list[Image.Image], list[int]]:
    """Several pictures as the frames of one GIF, each centered on a canvas
    the size of the first."""
    width, height = stills[0].size
    frames = []
    for still in stills:
        scale = min(width / still.width, height / still.height)
        fitted = still.resize((max(1, int(still.width * scale)), max(1, int(still.height * scale))), Image.LANCZOS)
        canvas = Image.new("RGBA", (width, height), (0, 0, 0, 0))
        canvas.alpha_composite(fitted, ((width - fitted.width) // 2, (height - fitted.height) // 2))
        frames.append(canvas)
    return frames, [delay_ms] * len(frames)
