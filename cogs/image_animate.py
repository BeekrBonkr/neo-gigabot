from __future__ import annotations

import random

import discord
from discord import app_commands
from discord.ext import commands

from utils import audio_fx
from utils import image_animate as an
from utils import image_mosh as mosh
from utils.imaging import encode_frames, load_frames, set_video_sound_filter

SOURCE_HELP = {
    "attachment": "Optional image attachment.",
    "message_link": "Optional Discord message link containing the image.",
}
SPEED_HELP = "Milliseconds per frame. Lower is faster."
USER_HELP = "Use this member's avatar instead of an image."
DIRECTIONS = [
    app_commands.Choice(name="Clockwise", value="clockwise"),
    app_commands.Choice(name="Counterclockwise", value="counterclockwise"),
]
Speed = app_commands.Range[int, 20, 200]
MOSH_BLOCKS = [
    app_commands.Choice(name="Fine", value="fine"),
    app_commands.Choice(name="Medium", value="medium"),
    app_commands.Choice(name="Chunky", value="chunky"),
]
MOSH_MOTION = [
    app_commands.Choice(name="Smear: pixels follow what moves", value="smear"),
    app_commands.Choice(name="Ghost: trails stay in place", value="ghost"),
]
MOSH_CUTS = [
    app_commands.Choice(name="Mosh through scene cuts", value="mosh"),
    app_commands.Choice(name="Keep scene cuts clean", value="clean"),
]
MOSH_LENGTH = [
    app_commands.Choice(name="Keep the clip's length", value="keep"),
    app_commands.Choice(name="Stretch: melting adds frames", value="stretch"),
]
MOSH_SOUND = [
    app_commands.Choice(name="Clean", value="clean"),
    app_commands.Choice(name="Light crush", value="light"),
    app_commands.Choice(name="Heavy crush", value="heavy"),
    app_commands.Choice(name="Destroyed", value="destroyed"),
]
CRUSH_SHAPES = [
    app_commands.Choice(name="Hard: even steps", value="lin"),
    app_commands.Choice(name="Soft: finer steps where it is quiet", value="log"),
]


class ImageAnimate(commands.GroupCog, group_name="animate", group_description="Turn images into GIFs, and edit GIFs."):
    """Animations made from a still image, and tools for existing GIFs.
    Images are found the same way /image finds them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _animate(self, interaction: discord.Interaction, name: str, title: str, build, attachment, message_link,
                       *, user: discord.User | None = None) -> None:
        """`build(Frames)` returns (frames, durations). Stills are shrunk
        before being animated; GIFs and videos keep their own frames."""

        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
            loaded = load_frames(image_bytes, filename, max_edge=an.GIF_MAX_EDGE)
            if not loaded.animated:
                loaded = load_frames(image_bytes, filename, max_edge=an.STILL_MAX_EDGE, first_only=True)
            frames, durations = build(loaded)
            return encode_frames(frames, durations, name, loop=loaded.loop)

        async def avatar() -> tuple[bytes, str]:
            return await user.display_avatar.replace(size=256, static_format="png").read(), "avatar.png"

        await self.bot.image_tools.run_image_command(
            interaction, name, title, work, attachment=attachment, message_link=message_link, source=avatar if user else None
        )

    @app_commands.command(name="spin", description="Spin an image in a loop.")
    @app_commands.describe(speed=SPEED_HELP, direction="Which way it turns.", **SOURCE_HELP)
    @app_commands.choices(direction=DIRECTIONS)
    @app_commands.checks.cooldown(1, 5.0)
    async def spin(
        self, interaction: discord.Interaction, speed: Speed = 50, direction: app_commands.Choice[str] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        clockwise = direction is None or direction.value == "clockwise"
        await self._animate(interaction, "spin", "Spinning", lambda loaded: an.spin(loaded, speed, clockwise), attachment, message_link)

    @app_commands.command(name="rainbow", description="Cycle an image's colors through the whole spectrum.")
    @app_commands.describe(speed=SPEED_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def rainbow(
        self, interaction: discord.Interaction, speed: Speed = 60,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "rainbow", "Rainbow", lambda loaded: an.rainbow(loaded, speed), attachment, message_link)

    @app_commands.command(name="wave", description="Ripple an image like a flag.")
    @app_commands.describe(speed=SPEED_HELP, strength="How far the ripple moves, as a percent of the width.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def wave(
        self, interaction: discord.Interaction, speed: Speed = 50, strength: app_commands.Range[int, 1, 20] = 5,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "wave", "Waving", lambda loaded: an.wave(loaded, speed, strength), attachment, message_link)

    @app_commands.command(name="zoom", description="Pulse an image in and out.")
    @app_commands.describe(speed=SPEED_HELP, amount="Extra zoom at the peak, in percent.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def zoom(
        self, interaction: discord.Interaction, speed: Speed = 50, amount: app_commands.Range[int, 5, 200] = 40,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "zoom", "Zooming", lambda loaded: an.zoom(loaded, speed, amount), attachment, message_link)

    @app_commands.command(name="pet", description="A hand pats an image or someone's avatar.")
    @app_commands.describe(user=USER_HELP, speed=SPEED_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def pet(
        self, interaction: discord.Interaction, user: discord.User | None = None, speed: Speed = 60,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "pet", "Pet", lambda loaded: an.pet(loaded, speed), attachment, message_link, user=user)

    @app_commands.command(name="triggered", description="An image or someone's avatar shakes over a TRIGGERED label.")
    @app_commands.describe(user=USER_HELP, speed=SPEED_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def triggered(
        self, interaction: discord.Interaction, user: discord.User | None = None, speed: Speed = 50,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(
            interaction, "triggered", "Triggered", lambda loaded: an.triggered(loaded, speed), attachment, message_link, user=user,
        )

    @app_commands.command(name="bonk", description="The bonk meme: a bat comes down on an image or someone's avatar.")
    @app_commands.describe(user=USER_HELP, speed=SPEED_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def bonk(
        self, interaction: discord.Interaction, user: discord.User | None = None, speed: Speed = 50,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "bonk", "Bonk", lambda loaded: an.bonk(loaded, speed), attachment, message_link, user=user)

    @app_commands.command(name="boom", description="An image, GIF or avatar swells up, then goes off in a nuclear explosion.")
    @app_commands.describe(user=USER_HELP, speed="Milliseconds per frame while a still image swells. Lower is faster.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def boom(
        self, interaction: discord.Interaction, user: discord.User | None = None, speed: Speed = 40,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "boom", "Boom", lambda loaded: an.boom(loaded, speed), attachment, message_link, user=user)

    # ---------- tools for GIFs people already have ----------

    async def _gif_tool(self, interaction: discord.Interaction, name: str, title: str, build, attachment, message_link) -> None:
        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
            loaded = load_frames(image_bytes, filename)
            frames, durations = build(loaded)
            return encode_frames(frames, durations, name, loop=loaded.loop)

        await self.bot.image_tools.run_image_command(interaction, name, title, work, attachment=attachment, message_link=message_link)

    @app_commands.command(name="speed", description="Make a GIF faster or slower.")
    @app_commands.describe(percent="100 is unchanged, 200 is twice as fast, 50 is half speed.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def speed(
        self, interaction: discord.Interaction, percent: app_commands.Range[int, 10, 1000] = 200,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._gif_tool(interaction, "speed", "Speed Changed", lambda loaded: an.speed(loaded, percent / 100.0), attachment, message_link)

    @app_commands.command(name="reverse", description="Play a GIF backward.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def reverse(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._gif_tool(interaction, "reverse", "Reversed", an.reverse, attachment, message_link)

    @app_commands.command(name="frames", description="Pull one frame out of a GIF as a still image.")
    @app_commands.describe(number="Which frame, counting from 1.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def frames(
        self, interaction: discord.Interaction, number: app_commands.Range[int, 1, 120] = 1,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str, str]:
            frame, summary = an.pick_frame(load_frames(image_bytes, filename), number)
            data, out_name = encode_frames([frame], [100], f"frame-{number}")
            return data, out_name, summary

        await self.bot.image_tools.run_image_command(interaction, "frames", "Frame", work, attachment=attachment, message_link=message_link)

    @app_commands.command(name="datamosh", description="Break a GIF or video the way a damaged file breaks: smears, melts and glitches.")
    @app_commands.describe(
        into="A second GIF or video. Its motion drags the first one's last picture around: the classic mosh.",
        melt="How many extra times the motion at each mosh point is applied. More melts further.",
        every="A mosh point every this many frames.",
        start="How far in the moshing begins, in percent.",
        end="How far in the moshing stops, in percent.",
        skip="Frames thrown away after each mosh point, so later motion lands on a stale picture.",
        shuffle="Percent of motion frames moved out of order.",
        blockiness="How hard the picture is compressed. 1 is clean, 31 is crushed.",
        block_size="How big the blocks that smear around are.",
        motion="Smear follows what moves. Ghost leaves trails in place.",
        cuts="What happens where the scene changes.",
        length="Whether melting stretches the clip. Keeping the length keeps a video's sound.",
        bitcrush="Bit crush a video's sound to go with the picture.",
        reverse="Mosh the clip played backward.",
        seed="The same seed gives the same shuffle again.",
        **SOURCE_HELP,
    )
    @app_commands.choices(block_size=MOSH_BLOCKS, motion=MOSH_MOTION, cuts=MOSH_CUTS, length=MOSH_LENGTH, bitcrush=MOSH_SOUND)
    @app_commands.checks.cooldown(1, 8.0)
    async def datamosh(
        self, interaction: discord.Interaction,
        into: discord.Attachment | None = None,
        melt: app_commands.Range[int, 0, 40] = 6, every: app_commands.Range[int, 1, 120] = 12,
        start: app_commands.Range[int, 0, 95] = 0, end: app_commands.Range[int, 5, 100] = 100,
        skip: app_commands.Range[int, 0, 30] = 0, shuffle: app_commands.Range[int, 0, 100] = 0,
        blockiness: app_commands.Range[int, 1, 31] = 5,
        block_size: app_commands.Choice[str] | None = None, motion: app_commands.Choice[str] | None = None,
        cuts: app_commands.Choice[str] | None = None, length: app_commands.Choice[str] | None = None,
        bitcrush: app_commands.Choice[str] | None = None,
        reverse: bool = False, seed: app_commands.Range[int, 0, 999999] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        if end <= start:
            await self.bot.image_tools.send_error(interaction, "Nothing To Mosh", "`end` has to come after `start`.")
            return
        settings = mosh.Settings(
            melt=melt, every=every, start=start, end=end, skip=skip, shuffle=shuffle, blockiness=blockiness,
            block_size=block_size.value if block_size else "fine", motion=motion.value if motion else "smear",
            clean_cuts=bool(cuts and cuts.value == "clean"), keep_length=not (length and length.value == "stretch"),
            reverse=reverse, seed=seed if seed is not None else random.randrange(1000000),
        )

        crush = bitcrush.value if bitcrush and bitcrush.value != "clean" else None

        def work(image_bytes: bytes, filename: str, *more: bytes) -> tuple[bytes, str, str]:
            clip = load_frames(image_bytes, filename, max_edge=mosh.MOSH_MAX_EDGE)
            second = load_frames(more[0], max_edge=mosh.MOSH_MAX_EDGE) if more else None
            frames, durations = mosh.datamosh(clip, settings, second)
            summary = f"Melt {melt} every {every} frames · blockiness {blockiness} · seed {settings.seed}"
            if crush and set_video_sound_filter(audio_fx.crush_filter(*audio_fx.CRUSH_PRESETS[crush])):
                summary += f" · sound: {bitcrush.name.lower()}"  # only said when there is a video, and so a sound, to crush
            data, out_name = encode_frames(frames, durations, "datamosh", loop=clip.loop)
            return data, out_name, summary

        await self.bot.image_tools.run_image_command(
            interaction, "datamosh", "Datamoshed", work, attachment=attachment, message_link=message_link,
            extra_sources=[(into, None)] if into is not None else None,
        )

    @app_commands.command(name="bitcrush", description="Bit crush the sound of a video or an audio file. The picture is left as it is.")
    @app_commands.describe(
        bits="Bits left for each sample. Fewer is fizzier: 8 is an old console, 3 is rubble.",
        downsample="How many samples become one. More is more metallic and muffled.",
        mix="Percent of crushed sound against the original.",
        shape="How the steps are spaced. Soft keeps quiet sounds that hard steps would round down to silence.",
        drive="Decibels of boost going in, for more distortion. The result is kept from getting louder.",
        attachment="A video or an audio file.",
        message_link="Or a link to a message that has one.",
    )
    @app_commands.choices(shape=CRUSH_SHAPES)
    @app_commands.checks.cooldown(1, 8.0)
    async def bitcrush(
        self, interaction: discord.Interaction,
        bits: app_commands.Range[int, 2, 16] = 6, downsample: app_commands.Range[int, 1, 100] = 8,
        mix: app_commands.Range[int, 1, 100] = 100, shape: app_commands.Choice[str] | None = None,
        drive: app_commands.Range[int, 0, 30] = 0,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        crush = audio_fx.crush_filter(bits, downsample, shape.value if shape else "lin", mix=mix, drive=drive)

        def work(media: bytes, filename: str) -> tuple[bytes, str, str]:
            data, extension = audio_fx.apply_audio_filter(media, crush)
            return data, f"bitcrushed.{extension}", f"{bits} bits · every {downsample} samples become one · {mix}% crushed"

        async def media() -> tuple[bytes, str]:
            return await self.bot.image_tools.get_media_source(interaction, attachment, message_link)

        await self.bot.image_tools.run_image_command(interaction, "bitcrush", "Bit Crushed", work, source=media)

    @app_commands.command(name="togif", description="Join two to six images, GIFs or videos into one looping GIF.")
    @app_commands.describe(
        image1="First frame. It sets the size of the GIF.", image2="Second frame.", image3="Third frame.", image4="Fourth frame.",
        image5="Fifth frame.", image6="Sixth frame.", delay="Milliseconds each still image is shown. A GIF or a video plays at its own pace.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def togif(
        self, interaction: discord.Interaction, image1: discord.Attachment, image2: discord.Attachment,
        image3: discord.Attachment | None = None, image4: discord.Attachment | None = None,
        image5: discord.Attachment | None = None, image6: discord.Attachment | None = None,
        delay: app_commands.Range[int, 20, 5000] = 500,
    ) -> None:
        others = [image for image in (image2, image3, image4, image5, image6) if image is not None]

        def work(image_bytes: bytes, filename: str, *more: bytes) -> tuple[bytes, str]:
            pictures = [load_frames(data, max_edge=an.GIF_MAX_EDGE) for data in (image_bytes, *more)]
            frames, durations = an.join(pictures, delay)
            return encode_frames(frames, durations, "joined", keep_gif=True)  # the command is named for it

        await self.bot.image_tools.run_image_command(
            interaction, "togif", "Joined", work, attachment=image1, extra_sources=[(image, None) for image in others]
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageAnimate(bot))
