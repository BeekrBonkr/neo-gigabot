"""Example output for image commands, for the What's New lists.

A sample is made by running the command itself on a stock picture, so it
always shows what the command really does. The command is handed a stand-in
for the Discord interaction: the picture arrives as its attachment, and the
file it would have posted is caught instead of sent.

The stock picture is the bot's own avatar. A command that has nothing to show
for it is tried on a portrait (the ones that look for a face) and then on a
GIF (the ones that only work on animations). Commands that take no picture,
or that fail on all three, simply have no sample."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import discord
from discord import app_commands

log = logging.getLogger(__name__)

SAMPLE_TEXT = "Hello there"
# The Mona Lisa (public domain, from Wikimedia Commons), cropped to a square around the face.
FACE_PICTURE = Path(__file__).resolve().parents[1] / "assets" / "images" / "sample_face.jpg"
# Options a command needs before it does anything worth showing.
SAMPLE_OPTIONS: dict[str, dict[str, Any]] = {
    "filter adjust": {"contrast": 130, "saturation": 220},
    "edit resize": {"percent": 50},
    "image transform crop": {"left": 25, "right": 25},
}
# Commands whose result says nothing as a picture (a change of file format), or
# that no stock picture suits (swapping needs two faces).
NO_SAMPLE = {"image effects convert", "image effects faceswap"}
SAMPLE_TIMEOUT_SECONDS = 45.0
MAX_SAMPLE_BYTES = 6 * 1024 * 1024


class _Picture:
    """The stock picture, dressed as an uploaded attachment."""

    def __init__(self, data: bytes, kind: str = "png") -> None:
        self._data = data
        self.size = len(data)
        self.content_type = f"image/{kind}"
        self.filename = f"sample.{kind}"

    async def read(self) -> bytes:
        return self._data


class _Response:
    def __init__(self, caught: "_Caught") -> None:
        self._caught = caught
        self._done = False

    def is_done(self) -> bool:
        return self._done

    async def defer(self, **_: Any) -> None:
        self._done = True

    async def send_message(self, *_: Any, **kwargs: Any) -> None:
        self._done = True
        self._caught.take(kwargs)

    async def edit_message(self, **kwargs: Any) -> None:
        self._done = True
        self._caught.take(kwargs)


class _Followup:
    def __init__(self, caught: "_Caught") -> None:
        self._caught = caught

    async def send(self, *_: Any, **kwargs: Any) -> None:
        self._caught.take(kwargs)


class _Caught:
    """Whatever the command tried to post."""

    def __init__(self) -> None:
        self.file: tuple[bytes, str] | None = None

    def take(self, kwargs: dict[str, Any]) -> None:
        files = [kwargs["file"]] if kwargs.get("file") is not None else list(kwargs.get("files") or [])
        if files and self.file is None:
            files[0].fp.seek(0)
            self.file = (files[0].fp.read(), files[0].filename)


class _Interaction:
    """As much of an interaction as an image command touches. There is no
    server and no channel, so per-server rules and channel searches don't apply."""

    guild = None
    guild_id = None
    channel = None
    channel_id = None
    message = None
    locale = discord.Locale.american_english

    def __init__(self, bot: Any, command: app_commands.Command, caught: _Caught) -> None:
        self.client = bot
        self.command = command
        self.user = bot.user
        self.response = _Response(caught)
        self.followup = _Followup(caught)
        self.extras: dict[str, Any] = {}

    async def original_response(self) -> None:
        return None

    async def edit_original_response(self, **kwargs: Any) -> None:
        return None


def _arguments(command: app_commands.Command, picture: _Picture) -> dict[str, Any] | None:
    """Values for the command's options: the picture wherever it takes an
    attachment, and something plain for anything else it insists on. None if
    the command takes no picture at all."""
    if command.qualified_name in NO_SAMPLE:
        return None
    values: dict[str, Any] = {}
    takes_picture = False
    for parameter in command.parameters:
        if parameter.type is discord.AppCommandOptionType.attachment:
            values[parameter.name] = picture  # every slot: a command that compares two pictures gets the same one twice
            takes_picture = True
            continue
        if not parameter.required:
            continue
        if parameter.choices:
            values[parameter.name] = parameter.choices[0]
        elif parameter.type is discord.AppCommandOptionType.string:
            values[parameter.name] = SAMPLE_TEXT
        elif parameter.type in (discord.AppCommandOptionType.integer, discord.AppCommandOptionType.number):
            low = parameter.min_value if parameter.min_value is not None else 1
            values[parameter.name] = type(low)(max(low, min(parameter.max_value if parameter.max_value is not None else 2, 2)))
        elif parameter.type is discord.AppCommandOptionType.boolean:
            values[parameter.name] = True
        else:
            return None  # a member, a channel, a role: nothing to offer without a server
    if not takes_picture:
        return None
    return {**values, **SAMPLE_OPTIONS.get(command.qualified_name, {})}


def can_sample(command: app_commands.Command) -> bool:
    return _arguments(command, _Picture(b"")) is not None


async def render(bot: Any, command: app_commands.Command, picture: bytes, kind: str = "png") -> tuple[bytes, str] | None:
    """Run the command on the picture (`kind` is its format: png, jpeg, gif).
    Returns (data, filename) of what it would have posted, or None if it has
    nothing to show."""
    arguments = _arguments(command, _Picture(picture, kind))
    if arguments is None:
        return None
    caught = _Caught()
    interaction = _Interaction(bot, command, caught)
    try:
        call = command.callback(command.binding, interaction, **arguments) if command.binding is not None else command.callback(interaction, **arguments)
        await asyncio.wait_for(call, timeout=SAMPLE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 - a command that cannot be sampled is not an error worth more than a note
        log.info("No sample for /%s: %r", command.qualified_name, exc)
        return None
    if caught.file is None or len(caught.file[0]) > MAX_SAMPLE_BYTES:
        return None
    return caught.file


def _moving(avatar: bytes) -> bytes:
    """The avatar as a short GIF (it gets patted), for commands that need one."""
    from utils import image_animate
    from utils.imaging import encode_frames, load_frames

    frames, durations = image_animate.pet(load_frames(avatar, max_edge=image_animate.STILL_MAX_EDGE, first_only=True))
    return encode_frames(frames, durations, "sample")[0]


class SampleStore:
    """Samples by command name, each made once and kept until the bot restarts
    (a restart is also when a command's behavior can change)."""

    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._made: dict[str, tuple[bytes, str] | None] = {}
        self._pictures: list[tuple[bytes, str]] | None = None
        self._lock = asyncio.Lock()  # one at a time: a page asking for forty must not start forty renders

    def command(self, name: str) -> app_commands.Command | None:
        for command in self.bot.tree.walk_commands():
            if isinstance(command, app_commands.Command) and command.qualified_name == name:
                return command
        return None

    async def _stock_pictures(self) -> list[tuple[bytes, str]]:
        """What a command is tried on, in order: the avatar, a face, a GIF."""
        if self._pictures is None:
            avatar = await self.bot.user.display_avatar.replace(size=512, static_format="png").read()
            pictures = [(avatar, "png")]
            if FACE_PICTURE.is_file():
                pictures.append((FACE_PICTURE.read_bytes(), "jpeg"))
            try:
                pictures.append((_moving(avatar), "gif"))
            except Exception:  # noqa: BLE001 - an avatar that can't be animated just leaves the GIF-only commands without a sample
                log.info("No animated stock picture", exc_info=True)
            self._pictures = pictures
        return self._pictures

    async def get(self, name: str) -> tuple[bytes, str] | None:
        """The sample for the command with this full name, or None."""
        if name in self._made:
            return self._made[name]
        async with self._lock:
            if name not in self._made:
                command = self.command(name)
                made = None
                if command is not None and can_sample(command) and self.bot.user is not None:
                    for picture, kind in await self._stock_pictures():
                        made = await render(self.bot, command, picture, kind)
                        if made is not None:
                            break
                self._made[name] = made
        return self._made[name]
