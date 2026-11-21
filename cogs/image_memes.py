from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from utils import image_memes as mm
from utils.imaging import VIDEO_MAX_FRAMES, Frames, apply_to_frames, compose_frames, encode_frames, fit_within, frame_budget, load_frames

SOURCE_HELP = {
    "attachment": "Optional image attachment.",
    "message_link": "Optional Discord message link containing the image.",
}
TAILS = [
    app_commands.Choice(name="Left", value="left"),
    app_commands.Choice(name="Center", value="center"),
    app_commands.Choice(name="Right", value="right"),
]
WATERMARKS = [
    app_commands.Choice(name="Unregistered HyperCam 2", value="hypercam"),
    app_commands.Choice(name="Bandicam", value="bandicam"),
    app_commands.Choice(name="iFunny", value="ifunny"),
]
SCENES = [
    app_commands.Choice(name="Museum", value="museum"),
    app_commands.Choice(name="Billboard", value="billboard"),
    app_commands.Choice(name="Old TV", value="oldtv"),
    app_commands.Choice(name="$100 Bill", value="bill"),
    app_commands.Choice(name="Bob Ross", value="bobross"),
]
POSTER_MAX_EDGE = 1024
# A GIF or video put into one of these formats gives a GIF, and a GIF stores
# every frame whole: its picture goes in smaller, the result comes out smaller,
# and it has fewer frames, so it is quick to make and fits Discord's upload cap.
MOVING_PICTURE_MAX_EDGE = 360
MOVING_RESULT_MAX_EDGE = 480
MOVING_MAX_FRAMES = 60
MOVING_VIDEO_MAX_FRAMES = VIDEO_MAX_FRAMES  # a video holds frames cheaply, so it gets all of them, however long they take to draw


def _picture(data: bytes, filename: str | None = None) -> Frames:
    """An input for a poster format: a still at poster size, or the frames of a GIF or video, smaller."""
    loaded = load_frames(data, filename, max_edge=POSTER_MAX_EDGE)
    if loaded.animated:
        loaded.frames = [fit_within(frame, MOVING_PICTURE_MAX_EDGE) for frame in loaded.frames]
    return loaded


def _compose(pictures: list[Frames], build, stem: str) -> tuple[bytes, str]:
    return compose_frames(pictures, build, stem, max_edge=MOVING_RESULT_MAX_EDGE, limit=frame_budget(MOVING_MAX_FRAMES, MOVING_VIDEO_MAX_FRAMES))
USER_HELP = "Use this member's avatar instead of an image."


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
            return _compose([_picture(image_bytes, filename)], lambda picture: mm.wanted(picture, texts["name"], texts["reward"]), "wanted")

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
            def build(*shown: Image.Image) -> Image.Image:
                left = list(shown)
                top = left.pop(0) if reject_image is not None else None
                bottom = left.pop(0) if approve_image is not None else None
                return mm.drake(texts["reject"], texts["approve"], top, bottom)

            if not pictures:
                return encode_frames([build()], [100], "drake")
            return _compose([_picture(data) for data in pictures], build, "drake")

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

    # ---------- formats built on a template picture ----------

    async def _poster(self, interaction: discord.Interaction, name: str, title: str, build, attachment=None, message_link=None,
                      *, user: discord.User | None = None, texts: tuple[str, ...] = ()) -> None:
        """A format made from one picture: `build(picture, *texts)` returns it.
        A GIF or a video gives a GIF, built frame by frame. The picture is the
        member's avatar if `user` is given; `texts` are words typed in
        Discord, made ready to draw first."""
        ready: list = []

        async def prepare() -> None:
            for text in texts:
                ready.append(await self.bot.image_tools.prepare_text(text, interaction.guild))

        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str]:
            return _compose([_picture(image_bytes, filename)], lambda picture: build(picture, *ready), name)

        async def avatar() -> tuple[bytes, str]:
            return await user.display_avatar.replace(size=512, static_format="png").read(), "avatar.png"

        await self.bot.image_tools.run_image_command(
            interaction, name, title, work, attachment=attachment, message_link=message_link,
            source=avatar if user else None, before=prepare,
        )

    @app_commands.command(name="respects", description="Put an image in the funeral portrait. Press F to pay respects.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def respects(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "respects", "Press F", mm.respects, attachment, message_link, user=user)

    @app_commands.command(name="whodidthis", description="Frame an image with WHO DID THIS and crying-laughing faces.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def whodidthis(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "whodidthis", "Who Did This", mm.who_did_this, attachment, message_link, user=user)

    @app_commands.command(name="beautiful", description="Grunkle Stan admires an image: \"Oh, this- this is beautiful\".")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def beautiful(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "beautiful", "Beautiful", mm.beautiful, attachment, message_link, user=user)

    @app_commands.command(name="wheeze", description="Put an image in the speech bubble of the wheeze comic.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def wheeze(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "wheeze", "Wheeze", mm.wheeze, attachment, message_link, user=user)

    @app_commands.command(name="keepitem", description="List an image for sale: customers usually keep this item.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def keepitem(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "keepitem", "Keep This Item", mm.keep_item, attachment, message_link, user=user)

    async def _from_text(self, interaction: discord.Interaction, name: str, title: str, build, *texts: str) -> None:
        """A still made from words alone: `build(*texts)` returns it."""
        ready: list = []

        async def prepare() -> None:
            for text in texts:
                ready.append(await self.bot.image_tools.prepare_text(text, interaction.guild))

        await self.bot.image_tools.run_image_command(
            interaction, name, title, lambda _bytes, _name: encode_frames([build(*ready)], [100], name), source=_no_image, before=prepare,
        )

    @app_commands.command(name="achievement", description="An Xbox achievement popup with your text.")
    @app_commands.describe(text="What the achievement is for.")
    @app_commands.checks.cooldown(1, 3.0)
    async def achievement(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 60]) -> None:
        await self._from_text(interaction, "achievement", "Achievement Unlocked", mm.achievement, text)

    @app_commands.command(name="rip", description="A headstone for some text, or for a member.")
    @app_commands.describe(text="What the headstone says. Defaults to the member's name.", user="Carve this member's avatar into the stone.")
    @app_commands.checks.cooldown(1, 3.0)
    async def rip(
        self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 60] | None = None, user: discord.User | None = None,
    ) -> None:
        if user is not None:
            await self._poster(interaction, "rip", "R.I.P.", lambda picture, words: mm.rip(words, picture), user=user, texts=(text or user.display_name,))
        elif text:
            await self._from_text(interaction, "rip", "R.I.P.", mm.rip, text)
        else:
            await self.bot.image_tools.send_error(interaction, "Nothing To Bury", "Give some text, a member, or both.")

    @app_commands.command(name="rtx", description="Two images side by side: RTX OFF and RTX ON.")
    @app_commands.describe(
        off_image="The RTX OFF image.", on_image="The RTX ON image.",
        off_user="Use this member's avatar for RTX OFF.", on_user="Use this member's avatar for RTX ON.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def rtx(
        self, interaction: discord.Interaction,
        off_image: discord.Attachment | None = None, on_image: discord.Attachment | None = None,
        off_user: discord.User | None = None, on_user: discord.User | None = None,
    ) -> None:
        sides = ((off_image, off_user), (on_image, on_user))
        if any(image is None and member is None for image, member in sides):
            await self.bot.image_tools.send_error(interaction, "Missing A Side", "Give an image or a member for both RTX OFF and RTX ON.")
            return
        pictures: list[bytes] = []

        async def prepare() -> None:
            for image, member in sides:
                if image is not None:
                    pictures.append((await self.bot.image_tools.get_source_image(interaction, image, None))[0])
                else:
                    pictures.append(await member.display_avatar.replace(size=512, static_format="png").read())

        def work(_bytes: bytes, _name: str) -> tuple[bytes, str]:
            return _compose([_picture(data) for data in pictures], mm.rtx, "rtx")

        await self.bot.image_tools.run_image_command(interaction, "rtx", "RTX", work, source=_no_image, before=prepare)

    @app_commands.command(name="news", description="Turn an image into a breaking news broadcast.")
    @app_commands.describe(headline="The headline under BREAKING NEWS.", user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def news(
        self, interaction: discord.Interaction, headline: app_commands.Range[str, 1, 80], user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "news", "Breaking News", mm.news, attachment, message_link, user=user, texts=(headline,))

    @app_commands.command(name="presidential", description="A phone's Presidential Alert with your text.")
    @app_commands.describe(text="The alert's message.")
    @app_commands.checks.cooldown(1, 3.0)
    async def presidential(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 160]) -> None:
        await self._from_text(interaction, "presidential", "Presidential Alert", mm.presidential, text)

    @app_commands.command(name="notification", description="Drop a phone message banner over the top of an image.")
    @app_commands.describe(text="The message.", sender="Who it is from. Defaults to you.", user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def notification(
        self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 120],
        sender: app_commands.Range[str, 1, 40] | None = None, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(
            interaction, "notification", "Notification", mm.notification, attachment, message_link,
            user=user, texts=(text, sender or interaction.user.display_name),
        )

    @app_commands.command(name="einstein", description="Einstein writes your text on his blackboard.")
    @app_commands.describe(text="What he writes. Short works best.")
    @app_commands.checks.cooldown(1, 3.0)
    async def einstein(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 60]) -> None:
        await self._from_text(interaction, "einstein", "Einstein", mm.einstein, text)

    @app_commands.command(name="sonic", description="Sonic says your text.")
    @app_commands.describe(text="What Sonic says.")
    @app_commands.checks.cooldown(1, 3.0)
    async def sonic(self, interaction: discord.Interaction, text: app_commands.Range[str, 1, 120]) -> None:
        await self._from_text(interaction, "sonic", "Sonic Says", mm.sonic, text)

    @app_commands.command(name="watchmojo", description="Turn an image into a WatchMojo video with your title.")
    @app_commands.describe(title="The video's title, such as Top 10 Images You Need To See.", user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def watchmojo(
        self, interaction: discord.Interaction, title: app_commands.Range[str, 1, 80], user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "watchmojo", "WatchMojo", mm.watchmojo, attachment, message_link, user=user, texts=(title,))

    @app_commands.command(name="watermark", description="Stamp an image like a screen recorder or a meme site would.")
    @app_commands.describe(style="Whose watermark to add.", **SOURCE_HELP)
    @app_commands.choices(style=WATERMARKS)
    @app_commands.checks.cooldown(1, 3.0)
    async def watermark(
        self, interaction: discord.Interaction, style: app_commands.Choice[str],
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._per_frame(interaction, "watermark", "Watermarked", lambda frame: mm.watermark(frame, style.value), attachment, message_link)

    @app_commands.command(name="kirby", description="Kirby calls the police about an image.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def kirby(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "kirby", "Kirby's Calling The Police", mm.kirby, attachment, message_link, user=user)

    @app_commands.command(name="excuse", description="React to an image with the \"excuse me what\" panel.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def excuse(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "excuse", "Excuse Me", mm.excuse, attachment, message_link, user=user)

    @app_commands.command(name="perfection", description="Make an image the dictionary's example of perfection.")
    @app_commands.describe(user=USER_HELP, **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def perfection(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(interaction, "perfection", "Perfection", mm.perfection, attachment, message_link, user=user)

    @app_commands.command(name="scene", description="Put an image in a museum, on a billboard, an old TV, a $100 bill or Bob Ross's easel.")
    @app_commands.describe(scene="Where the image goes.", user=USER_HELP, **SOURCE_HELP)
    @app_commands.choices(scene=SCENES)
    @app_commands.checks.cooldown(1, 3.0)
    async def scene(
        self, interaction: discord.Interaction, scene: app_commands.Choice[str], user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        await self._poster(
            interaction, "scene", scene.name, lambda picture: mm.scene(picture, scene.value), attachment, message_link, user=user,
        )


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageMemes(bot))
