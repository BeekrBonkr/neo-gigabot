from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands
from PIL import Image

from utils import image_edit as ed
from utils.imaging import apply_to_frames, encode_frames, load_frames

SOURCE_HELP = {
    "attachment": "Optional image attachment.",
    "message_link": "Optional Discord message link containing the image.",
}
SIDES = [app_commands.Choice(name=f"Keep the {side} half", value=side) for side in ed.MIRROR_SIDES]
LAYOUTS = [
    app_commands.Choice(name="Side by side", value="row"),
    app_commands.Choice(name="Grid", value="grid"),
]
# Pictures going into a collage or the background remover are shrunk to this first.
WORK_MAX_EDGE = 1600
GIF_RESIZE_MAX_EDGE = 1024


class ImageEdit(commands.GroupCog, group_name="edit", group_description="Resize, frame, combine and cut out images."):
    """Layout and utility edits. Images are found the same way /image finds them."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        super().__init__()

    async def _per_frame(self, interaction: discord.Interaction, name: str, title: str, processor, attachment, message_link) -> None:
        await self.bot.image_tools.run_image_command(
            interaction, name, title,
            lambda image_bytes, filename: apply_to_frames(image_bytes, filename, processor, name),
            attachment=attachment, message_link=message_link,
        )

    @app_commands.command(name="mirror", description="Reflect one half of an image onto the other.")
    @app_commands.describe(side="The half that is kept and reflected.", **SOURCE_HELP)
    @app_commands.choices(side=SIDES)
    @app_commands.checks.cooldown(1, 3.0)
    async def mirror(
        self, interaction: discord.Interaction, side: app_commands.Choice[str] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        chosen = side.value if side else "left"
        await self._per_frame(interaction, "mirror", "Mirrored", lambda frame: ed.mirror(frame, chosen), attachment, message_link)

    @app_commands.command(name="tile", description="Repeat an image in a grid.")
    @app_commands.describe(columns="How many across.", rows="How many down. Defaults to the same as columns.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def tile(
        self, interaction: discord.Interaction, columns: app_commands.Range[int, 1, 10] = 3, rows: app_commands.Range[int, 1, 10] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        down = rows or columns
        if columns == down == 1:
            await self.bot.image_tools.send_error(interaction, "Nothing To Tile", "A 1 by 1 grid is just the image. Pick more columns or rows.")
            return
        await self._per_frame(interaction, "tile", "Tiled", lambda frame: ed.tile(frame, columns, down), attachment, message_link)

    @app_commands.command(name="circle", description="Crop an image to a circle with a transparent background.")
    @app_commands.describe(user="Use this member's avatar instead of an image.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def circle(
        self, interaction: discord.Interaction, user: discord.User | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        async def avatar() -> tuple[bytes, str]:
            asset = user.display_avatar.replace(size=512)
            return await asset.read(), "avatar.gif" if asset.is_animated() else "avatar.png"

        await self.bot.image_tools.run_image_command(
            interaction, "circle", "Circle",
            lambda image_bytes, filename: apply_to_frames(image_bytes, filename, ed.circle, "circle"),
            attachment=attachment, message_link=message_link, source=avatar if user else None,
        )

    @app_commands.command(name="border", description="Add a colored frame or rounded corners to an image.")
    @app_commands.describe(
        color="A color name or hex code, such as white or #5865F2.", thickness="Frame width as a percent of the shorter side. 0 for none.",
        rounding="Corner rounding as a percent of the shorter side.", **SOURCE_HELP,
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def border(
        self, interaction: discord.Interaction, color: app_commands.Range[str, 1, 30] = "white",
        thickness: app_commands.Range[int, 0, 25] = 5, rounding: app_commands.Range[int, 0, 50] = 0,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        try:
            rgb = ed.parse_color(color)
        except ValueError as exc:
            await self.bot.image_tools.send_error(interaction, "Unknown Color", str(exc))
            return
        if thickness == 0 and rounding == 0:
            await self.bot.image_tools.send_error(interaction, "Nothing To Add", "Set a thickness, a rounding, or both.")
            return
        await self._per_frame(interaction, "border", "Framed", lambda frame: ed.border(frame, rgb, thickness, rounding), attachment, message_link)

    @app_commands.command(name="resize", description="Change an image's size.")
    @app_commands.describe(
        width="New width in pixels. Alone, the height follows to keep the shape.",
        height="New height in pixels. Alone, the width follows to keep the shape.",
        percent="Scale by a percent instead: 50 halves it, 200 doubles it.", **SOURCE_HELP,
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def resize(
        self, interaction: discord.Interaction, width: app_commands.Range[int, 1, 4096] | None = None,
        height: app_commands.Range[int, 1, 4096] | None = None, percent: app_commands.Range[int, 1, 1000] | None = None,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        if width is None and height is None and percent is None:
            await self.bot.image_tools.send_error(interaction, "No Size Given", "Give a width, a height, or a percent.")
            return

        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str, str]:
            loaded = load_frames(image_bytes, filename)
            limit = GIF_RESIZE_MAX_EDGE if loaded.animated else ed.MAX_OUTPUT_EDGE
            new_size = ed.target_size(loaded.size, width, height, percent, max_edge=limit)
            frames = [frame.resize(new_size, Image.LANCZOS) for frame in loaded.frames]
            data, out_name = encode_frames(frames, loaded.durations, "resized", loop=loaded.loop)
            return data, out_name, f"{loaded.size[0]}x{loaded.size[1]} to {new_size[0]}x{new_size[1]}"

        await self.bot.image_tools.run_image_command(interaction, "resize", "Resized", work, attachment=attachment, message_link=message_link)

    @app_commands.command(name="collage", description="Combine two to four images into one.")
    @app_commands.describe(
        image1="First image.", image2="Second image.", image3="Third image.", image4="Fourth image.",
        layout="Side by side, or a two-column grid. Picked for you if left out.",
    )
    @app_commands.choices(layout=LAYOUTS)
    @app_commands.checks.cooldown(1, 5.0)
    async def collage(
        self, interaction: discord.Interaction, image1: discord.Attachment, image2: discord.Attachment,
        image3: discord.Attachment | None = None, image4: discord.Attachment | None = None,
        layout: app_commands.Choice[str] | None = None,
    ) -> None:
        others = [image for image in (image2, image3, image4) if image is not None]
        chosen = layout.value if layout else "auto"

        def work(image_bytes: bytes, filename: str, *more: bytes) -> tuple[bytes, str]:
            pictures = [load_frames(data, max_edge=WORK_MAX_EDGE, first_only=True).frames[0] for data in (image_bytes, *more)]
            return encode_frames([ed.collage(pictures, chosen)], [100], "collage")

        await self.bot.image_tools.run_image_command(
            interaction, "collage", "Collage", work, attachment=image1, extra_sources=[(image, None) for image in others]
        )

    @app_commands.command(name="palette", description="Show an image's main colors with their hex codes.")
    @app_commands.describe(colors="How many colors to pick out.", **SOURCE_HELP)
    @app_commands.checks.cooldown(1, 3.0)
    async def palette(
        self, interaction: discord.Interaction, colors: app_commands.Range[int, 2, 8] = 5,
        attachment: discord.Attachment | None = None, message_link: str | None = None,
    ) -> None:
        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str, str]:
            swatches, codes = ed.palette(load_frames(image_bytes, filename, first_only=True).frames[0], colors)
            data, out_name = encode_frames([swatches], [100], "palette")
            return data, out_name, "Most common first: " + " ".join(f"`{code}`" for code in codes)

        await self.bot.image_tools.run_image_command(interaction, "palette", "Palette", work, attachment=attachment, message_link=message_link)

    @app_commands.command(name="removebg", description="Cut out the main subject and make the background transparent.")
    @app_commands.describe(**SOURCE_HELP)
    @app_commands.checks.cooldown(1, 8.0)
    async def removebg(self, interaction: discord.Interaction, attachment: discord.Attachment | None = None, message_link: str | None = None) -> None:
        def work(image_bytes: bytes, filename: str) -> tuple[bytes, str, str]:
            loaded = load_frames(image_bytes, filename, max_edge=WORK_MAX_EDGE, first_only=True)
            data, out_name = encode_frames([ed.remove_background(loaded.frames[0])], [100], "cutout")
            return data, out_name, "Works best on photos with one clear subject. GIFs are cut from their first frame."

        await self.bot.image_tools.run_image_command(interaction, "removebg", "Background Removed", work, attachment=attachment, message_link=message_link)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageEdit(bot))
