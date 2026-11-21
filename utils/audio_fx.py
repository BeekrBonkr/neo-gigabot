"""Sound effects for videos and audio files, done by ffmpeg.

Bit crushing is what old samplers and game consoles did to sound for want of
memory: fewer bits for each sample (so quiet detail turns to fizz and steps)
and fewer samples a second (so the highs fold back down as a metallic ring).
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import tempfile
from pathlib import Path

from utils.imaging import MAX_CLIP_SECONDS, VIDEO_CONTAINERS, note_cut, video_container

TIMEOUT_SECONDS = 300.0
# For /animate datamosh's one option: (bits, downsample, shape). With so few bits, even
# steps would round anything quiet down to silence, so the harshest one spaces them by loudness.
CRUSH_PRESETS = {"light": (8, 4, "lin"), "heavy": (5, 12, "lin"), "destroyed": (3, 30, "log")}


def crush_filter(bits: int = 6, downsample: int = 8, shape: str = "lin", *, mix: int = 100, drive: int = 0) -> str:
    """The ffmpeg audio filter for a bit crush. `bits` is how many are left
    for each sample, `downsample` how many samples become one, `mix` the
    percent of crushed sound against the original, `shape` "lin" for even
    steps or "log" for finer steps where it is quiet, and `drive` the
    decibels of boost going in. A limiter after it keeps a boosted sound from
    coming out louder than the original could be."""
    level = 10 ** (drive / 20.0)
    # ffmpeg's crusher does one thing or the other, by its own mix setting: at 0 it cuts the bits
    # and leaves the sample rate alone, at 1 the reverse. So it is used twice, once for each.
    crushed = (f"acrusher=level_in={level:.4f}:level_out={1 / level:.4f}:bits={bits}:samples=1:mix=0:mode={shape}:aa=0,"
               f"acrusher=bits=16:samples={downsample}:mix=1:aa=0")
    if mix < 100:
        wet = mix / 100.0
        crushed = f"asplit[dry][wet];[wet]{crushed}[crushed];[dry][crushed]amix=inputs=2:weights='{1 - wet:.2f} {wet:.2f}':normalize=0"
    return f"{crushed},alimiter=limit=0.95:level=disabled"


def audio_format(data: bytes) -> str | None:
    """Which kind of sound file these bytes are, going by how they start, as
    the name of the ffmpeg reader for it. Names and content types are not
    trusted, and anything unrecognized is not opened."""
    if data[:4] == b"RIFF" and data[8:12] == b"WAVE":
        return "wav"
    if data[:4] == b"OggS":
        return "ogg"
    if data[:4] == b"fLaC":
        return "flac"
    if data[:3] == b"ID3" or (len(data) > 1 and data[0] == 0xFF and data[1] & 0xE0 == 0xE0 and data[1] & 0x06 != 0):
        return "mp3"
    if len(data) > 1 and data[0] == 0xFF and data[1] & 0xF6 == 0xF0:
        return "aac"
    return None


def _run(arguments: list[str], source: Path, target: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise ValueError("This needs ffmpeg, which is not installed here.")
    # The reader is named outright and only local files may be opened, so a
    # crafted upload cannot point ffmpeg at a playlist, a URL or another file.
    command = [ffmpeg, "-nostdin", "-v", "error", "-protocol_whitelist", "file", *arguments[:2], "-i", str(source), *arguments[2:], str(target)]
    try:
        done = subprocess.run(command, capture_output=True, timeout=TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise ValueError("That took too long. Try a shorter clip.") from exc
    if done.returncode != 0:
        logging.getLogger(__name__).info("ffmpeg could not process audio: %s", done.stderr.decode(errors="replace")[-300:])
    return done.returncode == 0 and target.is_file() and target.stat().st_size > 0


def apply_audio_filter(data: bytes, audio_filter: str) -> tuple[bytes, str]:
    """Run a video's or an audio file's sound through an ffmpeg filter.
    Returns (the new file, its extension). A video keeps its picture exactly
    as it was, whole and at full size: only the sound is made again."""
    container = video_container(data)
    reader = VIDEO_CONTAINERS[container] if container else audio_format(data)
    if reader is None:
        raise ValueError("That is not a video or an audio file I can read.")
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder) / "source"
        source.write_bytes(data)
        # the same cap as the picture commands: the first MAX_CLIP_SECONDS, and the reply says when that left some off
        seconds = _seconds(source, reader)
        if seconds and seconds > MAX_CLIP_SECONDS + 0.5:
            note_cut(float(MAX_CLIP_SECONDS))
        sound = ["-map", "0:a:0", "-af", audio_filter, "-t", str(MAX_CLIP_SECONDS)]
        if container == "matroska":  # a WebM stays a WebM
            attempts = [("webm", ["-map", "0:v:0?", "-c:v", "copy", *sound, "-c:a", "libopus", "-b:a", "128k", "-f", "webm"])]
        elif container == "mov":  # MP4, MOV, and M4A, which is the same file with no picture
            attempts = [("mp4", ["-map", "0:v:0?", "-c:v", "copy", *sound, "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-f", "mp4"])]
        else:
            attempts = [("mp3", [*sound, "-c:a", "libmp3lame", "-q:a", "3", "-f", "mp3"])]
        if container:  # a picture that cannot be carried over as it is gets made again
            attempts.append(("mp4", ["-map", "0:v:0?", "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
                                     *sound, "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", "-f", "mp4"]))
        for extension, arguments in attempts:
            target = Path(folder) / f"out.{extension}"
            if _run(["-f", reader, *arguments], source, target):
                made = target.read_bytes()
                if extension == "mp4" and container == "mov" and not has_picture(data):
                    extension = "m4a"
                return made, extension
    raise ValueError("I couldn't find any sound in that to work on.")


def _seconds(source: Path, reader: str) -> float | None:
    """How long a media file runs, or None if that cannot be told."""
    ffprobe = shutil.which("ffprobe")
    if ffprobe is None:
        return None
    command = [ffprobe, "-v", "error", "-protocol_whitelist", "file", "-f", reader, "-show_entries", "format=duration", "-of", "csv=p=0", str(source)]
    try:
        return float(subprocess.run(command, capture_output=True, timeout=10, text=True).stdout.strip())
    except (ValueError, subprocess.TimeoutExpired):
        return None


def has_picture(data: bytes) -> bool:
    """Whether an MP4-family file has a video track, going by its track types."""
    return b"vide" in data
