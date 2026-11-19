from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import image_animate as an
from utils.imaging import encode_frames, load_frames

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


class ImageAnimate(commands.GroupCog, group_name="animate", group_description="Turn images into GIFs, and edit GIFs."):
    """Animations made from a still image, and tools for existing GIFs.
    Images are found the same way /image finds them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _animate(self, interaction: discord.Interaction, name: str, title: str, build, attachment, message_link,
                       *, user: discord.User | None = None, still_only: bool = False) -> None:
        """`build(Frames)` returns (frames, durations). Stills are shrunk
        before being animated; GIFs keep their own frames unless `still_only`."""

        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
            loaded = load_frames(image_bytes, filename, max_edge=an.GIF_MAX_EDGE, first_only=still_only)
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
        await self._animate(interaction, "pet", "Pet", lambda loaded: an.pet(loaded, speed), attachment, message_link, user=user, still_only=True)

    @app_commands.command(name="triggered", description="An image or someone's avatar shakes over a TRIGGERED label.")
    @app_commands.describe(user=USER_HELP, speed=SPEED_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def triggered(
        self, interaction: discord.Interaction, user: discord.User | None = None, speed: Speed = 50,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(
            interaction, "triggered", "Triggered", lambda loaded: an.triggered(loaded, speed), attachment, message_link, user=user, still_only=True,
        )

    @app_commands.command(name="bonk", description="The bonk meme: a bat comes down on an image or someone's avatar.")
    @app_commands.describe(user=USER_HELP, speed=SPEED_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 5.0)
    async def bonk(
        self, interaction: discord.Interaction, user: discord.User | None = None, speed: Speed = 50,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._animate(interaction, "bonk", "Bonk", lambda loaded: an.bonk(loaded, speed), attachment, message_link, user=user, still_only=True)

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

    @app_commands.command(name="togif", description="Join two to six images into a looping GIF.")
    @app_commands.describe(
        image1="First frame. It sets the size of the GIF.", image2="Second frame.", image3="Third frame.", image4="Fourth frame.",
        image5="Fifth frame.", image6="Sixth frame.", delay="Milliseconds each image is shown.",
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
            stills = [load_frames(data, max_edge=an.GIF_MAX_EDGE, first_only=True).frames[0] for data in (image_bytes, *more)]
            frames, durations = an.join(stills, delay)
            return encode_frames(frames, durations, "joined")

        await self.bot.image_tools.run_image_command(
            interaction, "togif", "Joined", work, attachment=image1, extra_sources=[(image, None) for image in others]
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageAnimate(bot))
