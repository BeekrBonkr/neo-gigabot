"""Datamoshing for /animate datamosh: breaking a clip the way a damaged video
file breaks.

A compressed video is mostly made of delta frames: not pictures, but
instructions for moving the pixels of the picture before them. Now and then
there is a keyframe, a whole picture, to start again from. The glitch comes
from tampering with that stream and then playing it anyway:

- take a keyframe out, and the motion that follows is applied to whatever
  picture was on screen before it: one scene smears into the shape of another;
- repeat a delta frame, and its motion is applied again and again: the picture
  melts in the direction things were moving;
- throw delta frames away or shuffle them, and later motion lands on a picture
  it was never meant for.

So the frames are encoded to MPEG-4 with ffmpeg, the stream is edited here
frame by frame, and ffmpeg decodes what is left. Nothing is drawn: every
artifact is the decoder doing what the broken stream tells it to.
"""

from __future__ import annotations

import random
import shutil
import subprocess
from dataclasses import dataclass

from PIL import Image, ImageOps

from utils.imaging import MAX_GIF_FRAMES, Frames, frame_budget, thin_frames

FRAME_START = b"\x00\x00\x01\xb6"  # opens every coded frame in an MPEG-4 stream
ANY_START = b"\x00\x00\x01"
FFMPEG_TIMEOUT_SECONDS = 40.0
MOSH_MAX_EDGE = 480
BLOCK = 16  # the codec works in squares this size; pictures are sized to a whole number of them
HOLD = -1  # in a plan: show the last picture again without touching the stream

BLOCK_SIZES = {"fine": 1.0, "medium": 0.5, "chunky": 0.25}
MOTION = {"smear": "epzs", "ghost": "zero"}
SHUFFLE_REACH = 8  # how far a shuffled frame can land from where it was


@dataclass(slots=True)
class Settings:
    melt: int = 6  # extra times the motion at a mosh point is applied
    every: int = 12  # a mosh point every this many frames
    start: int = 0  # percent of the way in where moshing begins
    end: int = 100  # and where it stops
    skip: int = 0  # frames thrown away after each mosh point
    shuffle: int = 0  # percent of delta frames moved out of order
    blockiness: int = 5  # the codec's quantizer, 1 (clean) to 31 (crushed)
    block_size: str = "fine"
    motion: str = "smear"
    clean_cuts: bool = False  # keep keyframes at scene cuts instead of moshing through them
    keep_length: bool = True  # repeats replace the frames after them, so the clip is not stretched
    reverse: bool = False  # mosh the clip played backward
    seed: int = 0


def _ffmpeg(arguments: list[str], data: bytes) -> bytes:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("Datamoshing needs ffmpeg, which is not installed here.")
    try:
        done = subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "pipe", *arguments],
                              input=data, capture_output=True, timeout=FFMPEG_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("That took too long to mosh. Try a shorter clip.") from exc
    if done.returncode != 0 or not done.stdout:
        raise ValueError("That clip could not be moshed.")
    return done.stdout


def encode(frames: list[Image.Image], size: tuple[int, int], settings: Settings) -> tuple[bytes, list[bytes]]:
    """Frames of one size as an MPEG-4 stream: (its headers, one piece per
    frame). The first frame is a keyframe; the encoder adds another only
    where the scene changes."""
    raw = b"".join(frame.convert("RGB").tobytes() for frame in frames)
    stream = _ffmpeg(
        ["-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}", "-framerate", "25", "-i", "pipe:0",
         "-c:v", "mpeg4", "-bf", "0", "-g", "99999", "-q:v", str(settings.blockiness),
         "-motion_est", MOTION[settings.motion], "-threads", "2", "-f", "m4v", "pipe:1"],
        raw,
    )
    header, *pieces = stream.split(FRAME_START)
    # a keyframe is announced by headers of its own, which trail the frame before it: cut them off
    return header, [piece.split(ANY_START, 1)[0] for piece in pieces]


def is_keyframe(piece: bytes) -> bool:
    return bool(piece) and piece[0] >> 6 == 0


def decode(header: bytes, pieces: list[bytes], size: tuple[int, int]) -> list[Image.Image]:
    """Play a stream, however broken, and collect the pictures."""
    raw = _ffmpeg(
        ["-f", "m4v", "-i", "pipe:0", "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"],
        header + b"".join(FRAME_START + piece for piece in pieces),
    )
    step = size[0] * size[1] * 3
    return [Image.frombytes("RGB", size, raw[at:at + step]) for at in range(0, len(raw) - step + 1, step)]


def plan(keyframes: list[bool], settings: Settings, *, limit: int = MAX_GIF_FRAMES) -> list[int]:
    """Which coded frame to feed the decoder at each step of the result: an
    index into the stream, or HOLD to leave the picture as it is. `keyframes`
    says which frames of the stream are whole pictures. The first frame is
    always kept, since there has to be a picture to break."""
    count = len(keyframes)
    first = max(1, round(count * settings.start / 100))
    last = round(count * settings.end / 100)
    order = [0]
    at = 1
    while at < count:
        if keyframes[at]:
            # Kept, the cut stays clean. Taken out, the next motion lands on the old picture,
            # which is held for the missing frame's time when the clip is to keep its length.
            if settings.clean_cuts:
                order.append(at)
            elif settings.keep_length:
                order.append(HOLD)
            at += 1
            continue
        if first <= at < last and (at - first) % settings.every == 0 and (settings.melt or settings.skip):
            if settings.keep_length:
                # the repeats stand in for the frames after this one, and thrown-away frames hold the picture
                melt = min(settings.melt, count - at - 1)
                gone = min(settings.skip, count - at - 1 - melt)
                order.extend([at] * (1 + melt) + [HOLD] * gone)
                at += 1 + melt + gone
            else:
                order.extend([at] * (1 + settings.melt))
                at += 1 + settings.skip
            continue
        order.append(at)
        at += 1

    if settings.shuffle:
        rng = random.Random(settings.seed)
        movable = [spot for spot, frame in enumerate(order) if spot and frame != HOLD and not keyframes[frame] and first <= frame < last]
        for spot in movable:
            if rng.random() * 100 < settings.shuffle:
                near = [other for other in movable if other != spot and abs(other - spot) <= SHUFFLE_REACH]
                if near:
                    other = rng.choice(near)
                    order[spot], order[other] = order[other], order[spot]
    return order[:limit]


def _work_size(size: tuple[int, int], scale: float) -> tuple[int, int]:
    ratio = min(1.0, MOSH_MAX_EDGE / max(size)) * scale
    return tuple(max(BLOCK * 2, round(edge * ratio / BLOCK) * BLOCK) for edge in size)


def datamosh(clip: Frames, settings: Settings, into: Frames | None = None) -> tuple[list[Image.Image], list[int]]:
    """Mosh `clip`. With `into`, that clip follows it with its opening
    keyframe removed, so its motion drags the first clip's last picture
    around: the classic transition. Returns (frames, durations)."""
    if into is not None and not into.animated:
        raise ValueError("`into` has to be a GIF or a video: it is its motion that gets used.")
    if not clip.animated and into is None:
        raise ValueError("Datamoshing needs something that moves: a GIF or a video, here or in `into`.")
    out_size = clip.size
    size = _work_size(out_size, BLOCK_SIZES[settings.block_size])
    parts = [clip] + ([into] if into is not None else [])
    budget = frame_budget()
    share = budget // len(parts)

    header, stream, keyframes, durations = b"", [], [], []
    for number, part in enumerate(parts):
        frames, times = thin_frames(part.frames, part.durations, share) if part.animated else (part.frames[:1], [100])
        if settings.reverse:
            frames, times = frames[::-1], times[::-1]
        fitted = [ImageOps.fit(frame.convert("RGB"), size, Image.BILINEAR) for frame in frames]
        part_header, pieces = encode(fitted, size, settings)
        header = header or part_header
        if len(pieces) != len(frames):
            raise ValueError("That clip could not be moshed.")
        if number == 1:
            # the picture that opens the second clip is taken out whatever the settings say:
            # its motion landing on the first clip's last picture is the whole effect
            pieces, times = pieces[1:], times[1:]
        stream.extend(pieces)
        keyframes.extend(is_keyframe(piece) for piece in pieces)
        durations.extend(times)

    if all(keyframes[1:]):
        raise ValueError("Every frame of that clip is a new scene, so there is no motion in it to mosh.")
    order = plan(keyframes, settings, limit=budget)
    pictures = decode(header, [stream[frame] for frame in order if frame != HOLD], size)
    result, times, shown = [], [], iter(pictures)
    current = None
    following = 0  # the stream frame a hold stands in for, for its timing
    for frame in order:
        if frame == HOLD:
            following = min(following + 1, len(durations) - 1)
        else:
            current = next(shown, current)
            following = frame
        if current is None:
            continue
        result.append(current if size == out_size else current.resize(out_size, Image.BILINEAR))
        times.append(durations[following])
    if len(result) < 2:
        raise ValueError("That left nothing to play. Try gentler settings.")
    return result, times
