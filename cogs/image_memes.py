from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from utils import image_memes as mm
from utils.imaging import apply_to_frames, encode_frames, load_frames

SOURCE_HELP = {
    "attachment": "Optional image attachment.",
    "message_link": "Optional Discord message link containing the image.",
}
TAILS = [
    app_commands.Choice(name="Left", value="left"),
    app_commands.Choice(name="Center", value="center"),
    app_commands.Choice(name="Right", value="right"),
]
POSTER_MAX_EDGE = 1024


async def _no_image() -> tuple[bytes, str]:
    """For formats that are made from text alone."""
    return b"", ""


class ImageMemes(commands.GroupCog, group_name="memegen", group_description="Make memes from images and text."):
    """Meme formats. The ones that take an image find it the same way /image does."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _per_frame(self, interaction: discord.Interaction, name: str, title: str, processor, attachment, message_link) -> None:
        await self.bot.image_tools.run_image_command(
            interaction, name, title,
            lambda image_bytes, filename: apply_to_frames(image_bytes, filename, processor, name),
            attachment=attachment, message_link=message_link,
        )

    @app_commands.command(name="speechbubble", description="Cut a speech bubble into the top of an image.")
    @app_commands.describe(tail="Where the bubble's point aims.", **SOURCE_HELP)
    @app_commands.choices(tail=TAILS)
    @app_commands.checks.cooldown(1, 3.0)
    async def speechbubble(
        self, interaction: discord.Interaction, tail: app_commands.Choice[str] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        chosen = tail.value if tail else "left"
        await self._per_frame(interaction, "speechbubble", "Speech Bubble", lambda frame: mm.speech_bubble(frame, chosen), attachment, message_link)

    @app_commands.command(name="jail", description="Put an image behind bars.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def jail(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._per_frame(interaction, "jail", "Jailed", mm.jail, attachment, message_link)

    @app_commands.command(name="wasted", description="Drain the color and stamp WASTED across an image.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def wasted(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        await self._per_frame(interaction, "wasted", "Wasted", mm.wasted, attachment, message_link)

    @app_commands.command(name="wanted", description="Put an image on an old-west wanted poster.")
    @app_commands.describe(
        name="The name under the picture.", reward="The reward, such as $5,000.",
        user="Use this member's avatar and name instead of an image.", **SOURCE_HELP,
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def wanted(
        self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 40] | None = None,
        reward: app_commands.Range[str, 1, 20] = "$5,000", user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        texts: dict[str, object] = {}

        async def prepare() -> None:
            texts["name"] = await self.bot.image_tools.prepare_text(name or (user.display_name if user else ""), interaction.guild)
            texts["reward"] = await self.bot.image_tools.prepare_text(reward, interaction.guild)

        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
            picture = load_frames(image_bytes, filename, max_edge=POSTER_MAX_EDGE, first_only=True).frames[0]
            return encode_frames([mm.wanted(picture, texts["name"], texts["reward"])], [100], "wanted")

        async def avatar() -> tuple[bytes, str]:
            return await user.display_avatar.replace(size=512, static_format="png").read(), "avatar.png"

        await self.bot.image_tools.run_image_command(
            interaction, "wanted", "Wanted", work, attachment=attachment, message_link=message_link,
            source=avatar if user else None, before=prepare,
        )

    @app_commands.command(name="drake", description="The two-panel format: one thing turned down, another preferred.")
    @app_commands.describe(
        reject="Text for the top panel, the thing turned down.", approve="Text for the bottom panel, the thing preferred.",
        reject_image="An image for the top panel, instead of text.", approve_image="An image for the bottom panel, instead of text.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def drake(
        self, interaction: discord.Interaction,
        reject: app_commands.Range[str, 1, 140] | None = None, approve: app_commands.Range[str, 1, 140] | None = None,
        reject_image: discord.Attachment | None = None, approve_image: discord.Attachment | None = None,
    ) -> None:
        if not (reject or reject_image) or not (approve or approve_image):
            await self.bot.image_tools.send_error(interaction, "Missing A Panel", "Give text or an image for both the top and the bottom panel.")
            return
        given = [image for image in (reject_image, approve_image) if image is not None]
        texts: dict[str, object] = {}

        async def prepare() -> None:
            texts["reject"] = await self.bot.image_tools.prepare_text(reject or "", interaction.guild)
            texts["approve"] = await self.bot.image_tools.prepare_text(approve or "", interaction.guild)

        def work(_bytes: bytes, _name: str, *pictures: bytes) -> tuple[bytes, str]:
            loaded = [load_frames(data, max_edge=POSTER_MAX_EDGE, first_only=True).frames[0] for data in pictures]
            top = loaded.pop(0) if reject_image is not None else None
            bottom = loaded.pop(0) if approve_image is not None else None
            return encode_frames([mm.drake(texts["reject"], texts["approve"], top, bottom)], [100], "drake")

        await self.bot.image_tools.run_image_command(
            interaction, "drake", "Drake", work, source=_no_image, extra_sources=[(image, None) for image in given], before=prepare,
        )

    @app_commands.command(name="changemymind", description="Your statement on the sign of the change-my-mind table.")
    @app_commands.describe(text="The statement on the sign.")
    @app_commands.checks.cooldown(1, 3.0)
    async def changemymind(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 140]) -> None:
        texts: dict[str, object] = {}

        async def prepare() -> None:
            texts["statement"] = await self.bot.image_tools.prepare_text(text, interaction.guild)

        await self.bot.image_tools.run_image_command(
            interaction, "changemymind", "Change My Mind",
            lambda _bytes, _name: encode_frames([mm.change_my_mind(texts["statement"])], [100], "changemymind"),
            source=_no_image, before=prepare,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageMemes(bot))
