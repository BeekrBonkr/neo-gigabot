"""Content-aware scaling (seam carving) for /image effects contentaware.

A seam is a path of low-detail pixels running from one edge to the other, one
pixel per row. Taking seams out narrows a picture while leaving its busy parts
alone, which is what gives the effect its melted look.

Finding the cheapest seam means scoring every pixel, so seams are taken out a
handful at a time from one scoring pass rather than one pass each. What comes
out of carving is a map of which source pixel each output pixel shows, so a
plan made for one frame of a GIF can be used on others with two lookups each,
and two plans can be blended for the frames between them."""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np
from PIL import Image

from utils.imaging import encode_frames, load_frames

# One scoring pass takes out this share of the current width at most. Smaller
# is closer to carving one seam at a time, and slower.
SEAMS_PER_PASS = 1 / 40


@dataclass(slots=True)
class CarveMap:
    """How a carved picture is read out of its source. `columns[y, x]` is the
    source column shown at (x, y) once the width is carved; `rows[x, y]` is then
    the row of that narrowed picture shown at (x, y) of the result."""

    source_size: tuple[int, int]
    columns: np.ndarray
    rows: np.ndarray

    @property
    def size(self) -> tuple[int, int]:
        return self.rows.shape[0], self.rows.shape[1]


def _energy(pixels: np.ndarray) -> np.ndarray:
    """How much detail is at each pixel: the strength of the edges through it."""
    gray = cv2.cvtColor(pixels, cv2.COLOR_RGB2GRAY)
    return np.abs(cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)) + np.abs(cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3))


def _path_costs(energy: np.ndarray) -> np.ndarray:
    """For each pixel, the cost of the cheapest path down to it from the top row."""
    cost = energy.copy()
    for row in range(1, cost.shape[0]):
        above = cost[row - 1]
        cheapest = above.copy()
        np.minimum(cheapest[1:], above[:-1], out=cheapest[1:])
        np.minimum(cheapest[:-1], above[1:], out=cheapest[:-1])
        cost[row] += cheapest
    return cost


def _pick_seams(cost: np.ndarray, wanted: int) -> np.ndarray:
    """`wanted` seams that share no pixel, cheapest first, as a mask of the
    pixels to keep. Each is traced up from the bottom row, stepping around
    pixels an earlier seam has taken; one that gets boxed in jumps to the
    nearest free pixel in the row rather than being thrown away."""
    height, width = cost.shape
    rows = cost.tolist()
    taken = [bytearray(width) for _ in range(height)]
    for column in np.argsort(cost[-1], kind="stable")[:wanted].tolist():
        taken[-1][column] = 1
        for row in range(height - 2, -1, -1):
            costs, used = rows[row], taken[row]
            best, best_cost = -1, float("inf")
            for candidate in (column, column - 1, column + 1):
                if 0 <= candidate < width and not used[candidate] and costs[candidate] < best_cost:
                    best, best_cost = candidate, costs[candidate]
            reach = 2
            while best < 0:
                for candidate in (column - reach, column + reach):
                    if 0 <= candidate < width and not used[candidate] and costs[candidate] < best_cost:
                        best, best_cost = candidate, costs[candidate]
                reach += 1
            column = best
            used[column] = 1
    return np.frombuffer(b"".join(taken), dtype=np.uint8).reshape(height, width) == 0


def _narrow(pixels: np.ndarray, target_width: int) -> np.ndarray:
    """Carve columns out of `pixels` (height x width x 3, uint8) until it is
    `target_width` wide. Returns which source column each remaining pixel is."""
    height, width = pixels.shape[:2]
    columns = np.tile(np.arange(width, dtype=np.int32), (height, 1))
    while width > target_width:
        wanted = min(width - target_width, max(1, int(width * SEAMS_PER_PASS)))
        keep = _pick_seams(_path_costs(_energy(pixels)), wanted)
        width = int(keep[0].sum())  # every seam crosses every row once, so each row loses the same number
        pixels = pixels[keep].reshape(height, width, 3)
        columns = columns[keep].reshape(height, width)
    return columns


def _read(pixels: np.ndarray, picks: np.ndarray) -> np.ndarray:
    return np.take_along_axis(pixels, picks[:, :, None], axis=1)


def plan(image: Image.Image, target_width: int, target_height: int) -> CarveMap:
    """Work out how to carve the picture down to the target size."""
    pixels = np.ascontiguousarray(np.asarray(image.convert("RGB")))
    columns = _narrow(pixels, max(1, min(target_width, image.width)))
    turned = np.ascontiguousarray(_read(pixels, columns).transpose(1, 0, 2))  # the height is carved as a width
    rows = _narrow(turned, max(1, min(target_height, image.height)))
    return CarveMap(image.size, columns, rows)


def apply(image: Image.Image, carve: CarveMap) -> Image.Image:
    """Carve a picture the way `plan` worked out. It is resized to the planned
    picture's size first, so every frame of a GIF can go through the same plan."""
    frame = image.convert("RGB")
    if frame.size != carve.source_size:
        frame = frame.resize(carve.source_size, Image.LANCZOS)
    narrowed = _read(np.asarray(frame), carve.columns)
    carved = _read(narrowed.transpose(1, 0, 2), carve.rows).transpose(1, 0, 2)
    return Image.fromarray(np.ascontiguousarray(carved), "RGB")


# Carving is worked out afresh on only some frames of an animation, spread
# evenly through it; the frames between them are carved by a blend of the plans
# on either side, so the melt drifts from one to the next. A short animation
# has every frame carved. Past that, the longer it is the thinner the carved
# frames are spread, up to a ceiling that keeps the longest ones affordable.
KEYFRAMES_ALL = 8  # animations up to this long are carved frame by frame
KEYFRAMES_EVERY = 8  # then one more carved frame for each this many frames
KEYFRAMES_MAX = 12


def keyframe_count(frames: int) -> int:
    """How many frames of an animation this long get a plan of their own."""
    if frames <= KEYFRAMES_ALL:
        return frames
    return min(KEYFRAMES_MAX, KEYFRAMES_ALL + (frames - KEYFRAMES_ALL) // KEYFRAMES_EVERY)


def blend(before: CarveMap, after: CarveMap, share: float) -> CarveMap:
    """A plan part of the way from one to another. Both maps only ever rise
    along a row, so a mix of them does too and is as valid a plan as either."""
    if share <= 0:
        return before
    if share >= 1:
        return after

    def mix(first: np.ndarray, second: np.ndarray) -> np.ndarray:
        return np.rint(first * (1 - share) + second * share).astype(np.int32)

    return CarveMap(before.source_size, mix(before.columns, after.columns), mix(before.rows, after.rows))


def carve_frames(frames: list[Image.Image], target_width: int, target_height: int) -> list[Image.Image]:
    """Carve every frame of an animation to the target size."""
    last = len(frames) - 1
    count = keyframe_count(len(frames))
    keys = sorted({round(index * last / max(1, count - 1)) for index in range(count)})
    plans = {key: plan(frames[key], target_width, target_height) for key in keys}
    carved = []
    for index, frame in enumerate(frames):
        before = max(key for key in keys if key <= index)
        after = min(key for key in keys if key >= index)
        share = (index - before) / (after - before) if after > before else 0.0
        carved.append(apply(frame, blend(plans[before], plans[after], share)))
    return carved


def carve_bytes(image_bytes: bytes, filename: str | None, width_percent: int, height_percent: int, max_edge: int) -> tuple[bytes, str]:
    """Carve an upload down to these percentages of its size, after shrinking
    it to `max_edge`. Each frame of an animation keeps its own timing."""
    loaded = load_frames(image_bytes, filename, max_edge=max_edge)
    width, height = loaded.size
    frames = carve_frames(loaded.frames, max(2, width * width_percent // 100), max(2, height * height_percent // 100))
    return encode_frames(frames, loaded.durations, "contentaware", loop=loaded.loop)
