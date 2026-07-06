from __future__ import annotations

import asyncio
import functools
import io
import math
import random
from pathlib import Path
from typing import Callable

import cv2
import discord
import numpy as np
from discord import app_commands
from discord.ext import commands
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageSequence

LINE_SPACING = 6

# ---------- face detection ----------
#
# OpenCV's bundled "res10_300x300_ssd_iter_140000" Caffe model - the standard,
# widely-used DNN face detector (much better than a Haar cascade at angled/
# partially-occluded faces). Files live in assets/models/, fetched once from
# opencv's own GitHub repos (see git history for provenance).
#
# Pinned in requirements.txt: OpenCV 5.0 removed the Caffe importer entirely
# (cv2.dnn.readNet/readNetFromCaffe both refuse .caffemodel files), so this
# needs opencv-python-headless < 5.0.
FACE_PROTOTXT_NAME = "deploy.prototxt"
FACE_WEIGHTS_NAME = "res10_300x300_ssd_iter_140000_fp16.caffemodel"
FACE_CONFIDENCE_THRESHOLD = 0.5
FACE_BLOB_SIZE = (300, 300)
FACE_BLOB_MEAN = (104.0, 177.0, 123.0)

# Face detection, content-aware scale, swirl, shake, and deepfry are all
# meaningfully heavier than the rest of this cog's transforms (model inference,
# a seam-carving DP loop, or a per-pixel/per-frame Python loop), so they're run
# through an executor with a timeout and a bot-wide concurrency cap, mirroring
# cogs/music.py's _run_extraction helper for yt-dlp. GIFs multiply the cost of
# these by frame count, so the timeout has headroom well past what a single
# frame needs.
MAX_CONCURRENT_CPU_BOUND = 2
CPU_BOUND_TIMEOUT_SECONDS = 60.0

# /contentaware: seam carving is O(seams_removed x width x height) even with a
# vectorized per-row DP loop, and this command now carves both axes (width, then
# height via transpose) to get a deliberately stupid-looking squish. Benchmarked
# locally: a 450x450 image losing 90% of its width and then 90% of its height
# (the most extreme case this command allows) takes ~11s. Capping the working
# resolution and the minimum target percent keeps the worst case well inside
# CPU_BOUND_TIMEOUT_SECONDS with room to spare on a slower host.
#
# GIF frames replay the exact seams removed from the first frame instead of
# re-running the DP per frame - the DP is the expensive part, and every frame of
# a short meme GIF looks close enough to the first that reusing its seams is
# indistinguishable in practice while keeping cost roughly constant regardless
# of frame count.
CONTENTAWARE_MAX_DIMENSION = 450
CONTENTAWARE_MIN_PERCENT = 10
CONTENTAWARE_MAX_PERCENT = 95

OVERLAY_PLACEMENTS = (
    "center", "top", "bottom", "left", "right",
    "topleft", "topright", "bottomleft", "bottomright",
)

# toptext/middletext/bottomtext: classic Impact meme text should fill as much of the
# image as it can without touching the edges, growing OR shrinking to fit - not just
# shrinking down from a fixed default. Margins are a percentage of image size so this
# scales sensibly from tiny stickers to huge screenshots; the height cap keeps a giant
# wall of text from swallowing the whole image.
MEME_TEXT_MIN_SIZE = 12
MEME_TEXT_MAX_SIZE = 300
MEME_TEXT_MARGIN_RATIO = 0.05
MEME_TEXT_MIN_MARGIN_PX = 8
MEME_TEXT_MAX_BLOCK_HEIGHT_RATIO = 0.4

# /caption: classic "white box caption" meme format (see e.g. ezgif's "White Box
# Caption" tool) - a solid white bar prepended above (or appended below) the image
# with black text, sized to the bar rather than the other way around.
CAPTION_SIDE_MARGIN_RATIO = 0.06
CAPTION_VERTICAL_PADDING_PX = 16
CAPTION_MIN_BOX_HEIGHT_RATIO = 0.14
CAPTION_MAX_BOX_HEIGHT_RATIO = 0.6
CAPTION_MIN_FONT_SIZE = 16
CAPTION_MAX_FONT_SIZE = 250

# /motivate: the classic despair.com-style "motivational poster" - black background,
# thin light frame around the photo, bold caption (+ optional smaller subcaption)
# below. Entirely procedural, no extra template images needed.
MOTIVATE_PHOTO_WIDTH = 700
MOTIVATE_BORDER_PX = 14
MOTIVATE_MARGIN_PX = 48
MOTIVATE_TEXT_GAP_PX = 28
MOTIVATE_TITLE_MAX_HEIGHT = 150
MOTIVATE_TITLE_MAX_SIZE = 100
MOTIVATE_TITLE_MIN_SIZE = 20
MOTIVATE_SUBTITLE_MAX_HEIGHT = 70
MOTIVATE_SUBTITLE_MAX_SIZE = 40
MOTIVATE_SUBTITLE_MIN_SIZE = 14

# /quote text bounds. The canvas is a fixed 400x400, so nothing close to Discord's
# 2000/4000-char message limit will ever usefully fit - capping the input up front
# keeps the fit-to-11-lines loop below fast and bounded no matter what gets quoted.
QUOTE_MAX_INPUT_LENGTH = 400
QUOTE_MAX_SHRINK_ATTEMPTS = 30


class Images(commands.GroupCog, group_name="image", group_description="Image manipulation, memes, and effects."):
    """Slash-command image manipulation and quote commands."""

    # Subcommand groups, kept under 25 children each per Discord's per-group
    # subcommand cap. Must be defined before any @<group>.command(...) usage
    # below, but otherwise their order here doesn't matter.
    transform = app_commands.Group(name="transform", description="Resize, rotate, flip, crop, and other geometric transforms.")
    text = app_commands.Group(name="text", description="Meme text, captions, quote images, and motivational posters.")
    template = app_commands.Group(name="template", description="Overlay a template image.")
    effects = app_commands.Group(name="effects", description="Filters, format conversion, face detection, and compositing.")
    misc = app_commands.Group(name="misc", description="Other image utilities.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.assets_dir = self.bot.project_root / "assets" / "images"
        self._cpu_semaphore = asyncio.Semaphore(MAX_CONCURRENT_CPU_BOUND)
        self._face_net: cv2.dnn.Net | None = None

        self.set_image_source_menu = app_commands.ContextMenu(
            name="Set Image Source",
            callback=self.set_image_source_context,
        )
        self.clear_image_source_menu = app_commands.ContextMenu(
            name="Clear Image Source",
            callback=self.clear_image_source_context,
        )
        super().__init__()

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.set_image_source_menu)
        self.bot.tree.add_command(self.clear_image_source_menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.set_image_source_menu.name, type=self.set_image_source_menu.type)
        self.bot.tree.remove_command(self.clear_image_source_menu.name, type=self.clear_image_source_menu.type)

    # ---------- executor helper (shared with the effects commands below) ----------

    async def _run_cpu_bound(self, func: Callable[[], object], *, timeout: float = CPU_BOUND_TIMEOUT_SECONDS) -> object:
        loop = asyncio.get_running_loop()
        async with self._cpu_semaphore:
            try:
                return await asyncio.wait_for(loop.run_in_executor(None, func), timeout=timeout)
            except asyncio.TimeoutError as exc:
                raise RuntimeError("That took too long to process. Try a smaller image or less aggressive settings.") from exc

    # ---------- face detection helpers ----------

    def _get_face_net(self) -> cv2.dnn.Net:
        if self._face_net is None:
            models_dir = self.bot.project_root / "assets" / "models"
            prototxt = models_dir / FACE_PROTOTXT_NAME
            weights = models_dir / FACE_WEIGHTS_NAME
            if not prototxt.exists() or not weights.exists():
                raise RuntimeError("Face detection model files are missing from assets/models/.")
            self._face_net = cv2.dnn.readNetFromCaffe(str(prototxt), str(weights))
        return self._face_net

    def _detect_faces_in_frame(self, frame: Image.Image) -> list[tuple[int, int, int, int]]:
        net = self._get_face_net()
        rgb = np.array(frame.convert("RGB"))
        bgr = rgb[:, :, ::-1]
        height, width = bgr.shape[:2]
        blob = cv2.dnn.blobFromImage(cv2.resize(bgr, FACE_BLOB_SIZE), 1.0, FACE_BLOB_SIZE, FACE_BLOB_MEAN)
        net.setInput(blob)
        detections = net.forward()

        boxes: list[tuple[int, int, int, int]] = []
        for i in range(detections.shape[2]):
            confidence = float(detections[0, 0, i, 2])
            if confidence < FACE_CONFIDENCE_THRESHOLD:
                continue
            box = detections[0, 0, i, 3:7] * np.array([width, height, width, height])
            x1, y1, x2, y2 = box.astype(int)
            x1, y1 = max(0, x1), max(0, y1)
            x2, y2 = min(width, x2), min(height, y2)
            if x2 > x1 and y2 > y1:
                boxes.append((int(x1), int(y1), int(x2), int(y2)))
        return boxes

    async def _detect_faces(self, image_bytes: bytes, filename: str) -> list[tuple[int, int, int, int]]:
        # Faces are only detected once, on the first frame - re-running DNN
        # inference per GIF frame would multiply the cost by the frame count
        # and blow the timeout. Every frame of a short meme GIF is assumed to
        # keep faces in roughly the same place, which holds well enough for
        # /image effects blurfaces and /image effects facecrop's purposes.
        def work() -> list[tuple[int, int, int, int]]:
            with Image.open(io.BytesIO(image_bytes)) as image:
                first_frame = (
                    next(ImageSequence.Iterator(image)).convert("RGBA")
                    if self.bot.image_tools.is_gif(image, filename)
                    else image.convert("RGBA")
                )
                return self._detect_faces_in_frame(first_frame)

        return await self._run_cpu_bound(work)

    # ---------- content-aware scale (seam carving) ----------

    def _remove_seams(self, arr: np.ndarray, seams_to_remove: int) -> tuple[np.ndarray, list[np.ndarray]]:
        """Vertical seam removal (shrinks the array's width by one column per seam).
        Returns the reduced array plus the list of seams removed, in removal order,
        so the identical cuts can be replayed on other frames of a GIF without
        re-running the DP per frame (see CONTENTAWARE_MAX_DIMENSION comment)."""
        seams: list[np.ndarray] = []
        for _ in range(seams_to_remove):
            gray = cv2.cvtColor(arr.astype(np.uint8), cv2.COLOR_RGB2GRAY).astype(np.float64)
            energy = (
                np.abs(cv2.Sobel(gray, cv2.CV_64F, 1, 0, ksize=3))
                + np.abs(cv2.Sobel(gray, cv2.CV_64F, 0, 1, ksize=3))
            )
            height, width = energy.shape
            cost = energy.copy()
            backtrack = np.zeros((height, width), dtype=np.int64)

            for row in range(1, height):
                prev = cost[row - 1]
                left = np.roll(prev, 1)
                left[0] = np.inf
                right = np.roll(prev, -1)
                right[-1] = np.inf
                choices = np.vstack([left, prev, right])
                best = np.argmin(choices, axis=0)
                backtrack[row] = best - 1
                cost[row] = energy[row] + choices[best, np.arange(width)]

            seam = np.zeros(height, dtype=np.int64)
            seam[-1] = int(np.argmin(cost[-1]))
            for row in range(height - 2, -1, -1):
                next_col = seam[row + 1]
                seam[row] = int(np.clip(next_col + backtrack[row + 1, next_col], 0, width - 1))

            seams.append(seam)
            mask = np.ones((height, width), dtype=bool)
            mask[np.arange(height), seam] = False
            arr = arr[mask].reshape(height, width - 1, 3)

        return arr, seams

    def _apply_seams(self, arr: np.ndarray, seams: list[np.ndarray]) -> np.ndarray:
        """Replay a previously-recorded list of seam removals on a same-sized array."""
        for seam in seams:
            height, width = arr.shape[:2]
            mask = np.ones((height, width), dtype=bool)
            mask[np.arange(height), seam] = False
            arr = arr[mask].reshape(height, width - 1, 3)
        return arr

    def _seam_carve_both_axes(
        self, image: Image.Image, target_width: int, target_height: int
    ) -> tuple[Image.Image, list[np.ndarray], list[np.ndarray]]:
        """Squish both width and height via seam carving (height by transposing,
        carving, then transposing back). Returns the carved image plus the seams
        removed on each axis, so GIF frames can replay the same cuts."""
        arr = np.array(image.convert("RGB")).astype(np.float64)
        arr, width_seams = self._remove_seams(arr, max(0, arr.shape[1] - target_width))

        arr = arr.transpose(1, 0, 2)
        arr, height_seams = self._remove_seams(arr, max(0, arr.shape[1] - target_height))
        arr = arr.transpose(1, 0, 2)

        return Image.fromarray(arr.astype(np.uint8), mode="RGB"), width_seams, height_seams

    def _apply_seams_both_axes(
        self,
        image: Image.Image,
        working_size: tuple[int, int],
        width_seams: list[np.ndarray],
        height_seams: list[np.ndarray],
    ) -> Image.Image:
        frame = image.convert("RGB").resize(working_size, Image.LANCZOS)
        arr = np.array(frame).astype(np.float64)
        arr = self._apply_seams(arr, width_seams)

        arr = arr.transpose(1, 0, 2)
        arr = self._apply_seams(arr, height_seams)
        arr = arr.transpose(1, 0, 2)

        return Image.fromarray(arr.astype(np.uint8), mode="RGB")

    # ---------- image context menu commands ----------

    async def set_image_source_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "imageinfo"):
            return

        if not self.bot.image_tools.message_has_image(message):
            await self.bot.embeds.error_interaction(
                interaction,
                "No Image Found",
                "That message does not contain an image attachment or embedded image.",
                ephemeral=True,
            )
            return

        self.bot.image_tools.sources.remember(interaction.user.id, message.channel.id, message.id)
        await self.bot.image_tools.send_success(
            interaction,
            "Image Source Set",
            "That message is now your temporary image source for image commands.\n\n"
            "Priority order is: attachment, message link, selected message, then most recent image in the channel.",
        )

    async def clear_image_source_context(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "imageinfo"):
            return

        cleared = self.bot.image_tools.sources.clear(interaction.user.id)
        if cleared:
            await self.bot.image_tools.send_success(
                interaction,
                "Image Source Cleared",
                "Your temporary image source has been cleared.",
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nothing To Clear",
                "You do not currently have a temporary image source selected.",
                ephemeral=True,
            )

    @misc.command(name="clearimagesource", description="Clear your selected image source.")
    async def clear_image_source(self, interaction: discord.Interaction) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "imageinfo"):
            return

        cleared = self.bot.image_tools.sources.clear(interaction.user.id)
        if cleared:
            await self.bot.image_tools.send_success(
                interaction,
                "Image Source Cleared",
                "Your temporary image source has been cleared.",
            )
        else:
            await self.bot.embeds.warning_interaction(
                interaction,
                "Nothing To Clear",
                "You do not currently have a temporary image source selected.",
                ephemeral=True,
            )

    # ---------- font / text helpers ----------
    #
    # Two distinct font families are used, matching the legacy bot: Impact for the
    # bold-stroke meme-text commands (toptext/middletext/bottomtext), and Arial for
    # the softer /quote overlay. Both live in assets/fonts/, bundled with the repo;
    # the system-font paths below are only a last-resort fallback if those ever go
    # missing, and cover both common Linux font layouts (Debian/Ubuntu vs Fedora/RHEL).

    MEME_FONT_FILENAMES: tuple[str, ...] = ("impact.ttf", "Impact.ttf")
    MEME_FONT_SYSTEM_FALLBACKS: tuple[str, ...] = (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    )
    QUOTE_FONT_FILENAMES: tuple[str, ...] = ("arial.ttf", "Arial.ttf")
    QUOTE_FONT_SYSTEM_FALLBACKS: tuple[str, ...] = (
        "/usr/share/fonts/dejavu-sans-fonts/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )

    @staticmethod
    @functools.lru_cache(maxsize=64)
    def _load_truetype_font(path: str, size: int) -> ImageFont.FreeTypeFont:
        # Arial.ttf alone is ~4MB; without this cache, the meme-text auto-shrink loop
        # (which re-requests a font at several sizes per call) would reparse it from
        # disk every single time.
        return ImageFont.truetype(path, size=size)

    def _resolve_font_path(self, filenames: tuple[str, ...], system_fallbacks: tuple[str, ...]) -> str | None:
        fonts_dir = self.bot.project_root / "assets" / "fonts"
        for name in filenames:
            candidate = fonts_dir / name
            if candidate.exists():
                return str(candidate)
        for path in system_fallbacks:
            if Path(path).exists():
                return path
        return None

    def _load_font_or_default(self, path: str | None, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        if path is not None:
            try:
                return self._load_truetype_font(path, size)
            except OSError:
                pass
        return ImageFont.load_default()

    def _get_meme_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        path = self._resolve_font_path(self.MEME_FONT_FILENAMES, self.MEME_FONT_SYSTEM_FALLBACKS)
        return self._load_font_or_default(path, size)

    def _get_quote_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        path = self._resolve_font_path(self.QUOTE_FONT_FILENAMES, self.QUOTE_FONT_SYSTEM_FALLBACKS)
        return self._load_font_or_default(path, size)

    def _text_width(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> int:
        bbox = draw.textbbox((0, 0), text, font=font, stroke_width=3)
        return bbox[2] - bbox[0]

    def _split_long_token(
        self,
        draw: ImageDraw.ImageDraw,
        token: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        """Hard-break a single token too wide to fit on its own line (e.g. a long
        URL or a no-spaces wall of text), character by character, so it can't
        render off the edge of the image."""
        pieces: list[str] = []
        current = ""
        for char in token:
            candidate = current + char
            if current and self._text_width(draw, candidate, font) > max_width:
                pieces.append(current)
                current = char
            else:
                current = candidate
        if current:
            pieces.append(current)
        return pieces

    def _wrap_text(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        max_width: int,
    ) -> list[str]:
        lines: list[str] = []
        for paragraph in text.splitlines() or [text]:
            words = paragraph.split()
            if not words:
                lines.append("")
                continue

            current = ""
            for word in words:
                if self._text_width(draw, word, font) > max_width:
                    if current:
                        lines.append(current)
                        current = ""
                    lines.extend(self._split_long_token(draw, word, font, max_width))
                    continue

                candidate = f"{current} {word}".strip()
                if not current or self._text_width(draw, candidate, font) <= max_width:
                    current = candidate
                else:
                    lines.append(current)
                    current = word

            if current:
                lines.append(current)
        return lines

    def _measure_lines(
        self,
        draw: ImageDraw.ImageDraw,
        lines: list[str],
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
    ) -> tuple[int, int]:
        if not lines:
            return 0, 0
        widths = []
        total_height = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line or " ", font=font, stroke_width=3)
            widths.append(bbox[2] - bbox[0])
            total_height += (bbox[3] - bbox[1]) + LINE_SPACING
        return max(widths), max(0, total_height - LINE_SPACING)

    def _fit_text_to_box(
        self,
        draw: ImageDraw.ImageDraw,
        text: str,
        font_loader: Callable[[int], ImageFont.FreeTypeFont | ImageFont.ImageFont],
        *,
        max_width: int,
        max_height: int,
        min_size: int,
        max_size: int,
    ) -> tuple[ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
        """Binary-search the largest font size in [min_size, max_size] at which the
        word-wrapped text fits within max_width x max_height, so text is always as
        large as it can be without touching the edges - instead of shrinking down
        from (or being stuck at) a fixed default size.

        Always returns *some* font+lines, even if the text can't fit at min_size
        (e.g. absurdly long input in a tiny box) - callers never need to handle a
        "nothing fit" case.
        """
        max_width = max(1, max_width)
        max_height = max(1, max_height)
        min_size = max(1, min_size)
        max_size = max(min_size, max_size)

        def try_size(size: int) -> tuple[bool, ImageFont.FreeTypeFont | ImageFont.ImageFont, list[str]]:
            font = font_loader(size)
            wrapped = self._wrap_text(draw, text, font, max_width)
            width, height = self._measure_lines(draw, wrapped, font)
            return (width <= max_width and height <= max_height), font, wrapped

        _, best_font, best_wrapped = try_size(min_size)

        low, high = min_size, max_size
        while low <= high:
            mid = (low + high) // 2
            fits, font, wrapped = try_size(mid)
            if fits:
                best_font, best_wrapped = font, wrapped
                low = mid + 1
            else:
                high = mid - 1

        return best_font, best_wrapped

    def _draw_meme_text(
        self,
        image: Image.Image,
        text: str,
        *,
        position: str,
        max_size: int | None = None,
    ) -> Image.Image:
        # Classic Impact meme text is conventionally all-caps regardless of how the
        # user typed it - every major meme generator (imgflip, Kapwing, etc.) does
        # this automatically.
        text = text.upper()
        working = image.convert("RGBA")
        draw = ImageDraw.Draw(working)

        margin_x = max(MEME_TEXT_MIN_MARGIN_PX, int(working.width * MEME_TEXT_MARGIN_RATIO))
        margin_y = max(MEME_TEXT_MIN_MARGIN_PX, int(working.height * MEME_TEXT_MARGIN_RATIO))
        max_width = max(1, working.width - 2 * margin_x)
        max_block_height = max(1, int(working.height * MEME_TEXT_MAX_BLOCK_HEIGHT_RATIO))
        size_ceiling = min(max_size, MEME_TEXT_MAX_SIZE) if max_size else MEME_TEXT_MAX_SIZE

        font, wrapped = self._fit_text_to_box(
            draw,
            text,
            self._get_meme_font,
            max_width=max_width,
            max_height=max_block_height,
            min_size=MEME_TEXT_MIN_SIZE,
            max_size=size_ceiling,
        )
        _, text_height = self._measure_lines(draw, wrapped, font)

        if position == "top":
            y = margin_y
        elif position == "bottom":
            y = working.height - margin_y - text_height
        else:
            y = (working.height - text_height) // 2

        for line in wrapped:
            bbox = draw.textbbox((0, 0), line, font=font, stroke_width=3)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_x = (working.width - line_width) // 2
            draw.text(
                (line_x, y),
                line,
                font=font,
                fill=(255, 255, 255, 255),
                stroke_width=3,
                stroke_fill=(0, 0, 0, 255),
            )
            y += line_height + LINE_SPACING
        return working

    # ---------- generic image processing helpers ----------

    def _resize_x(self, frame: Image.Image, factor: float) -> Image.Image:
        new_width = max(1, min(4000, int(frame.width * factor)))
        return frame.resize((new_width, frame.height), Image.LANCZOS)

    def _resize_y(self, frame: Image.Image, factor: float) -> Image.Image:
        new_height = max(1, min(4000, int(frame.height * factor)))
        return frame.resize((frame.width, new_height), Image.LANCZOS)

    def _jpegify_frame(self, frame: Image.Image, quality: int) -> Image.Image:
        original_size = frame.size
        rgb = frame.convert("RGB")
        temp = io.BytesIO()
        rgb.save(temp, format="JPEG", quality=max(1, min(quality, 95)))
        temp.seek(0)
        low_quality = Image.open(temp).convert("RGB")
        return low_quality.resize(original_size, Image.LANCZOS)

    def _deepfry_frame(self, frame: Image.Image, sharpen_passes: int) -> Image.Image:
        rgb = frame.convert("RGB")
        rgb = ImageEnhance.Color(rgb).enhance(2.0)
        rgb = ImageEnhance.Contrast(rgb).enhance(1.4)
        rgb = ImageEnhance.Sharpness(rgb).enhance(2.5)
        for _ in range(max(1, sharpen_passes)):
            rgb = rgb.filter(ImageFilter.SHARPEN)
        temp = io.BytesIO()
        rgb.save(temp, format="JPEG", quality=10)
        temp.seek(0)
        return Image.open(temp).convert("RGB")

    def _swirl_frame(self, frame: Image.Image, degrees: float = 180.0) -> Image.Image:
        src = frame.convert("RGBA")
        width, height = src.size
        cx = width / 2.0
        cy = height / 2.0
        max_radius = math.hypot(cx, cy)
        src_px = src.load()
        out = Image.new("RGBA", src.size)
        out_px = out.load()

        strength = math.radians(degrees)
        for y in range(height):
            dy = y - cy
            for x in range(width):
                dx = x - cx
                radius = math.hypot(dx, dy)
                if radius == 0 or radius > max_radius:
                    sx, sy = x, y
                else:
                    theta = math.atan2(dy, dx)
                    twist = strength * (max_radius - radius) / max_radius
                    source_theta = theta - twist
                    sx = int(round(cx + radius * math.cos(source_theta)))
                    sy = int(round(cy + radius * math.sin(source_theta)))

                if 0 <= sx < width and 0 <= sy < height:
                    out_px[x, y] = src_px[sx, sy]
                else:
                    out_px[x, y] = (0, 0, 0, 0)
        return out

    def _shake_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        speed: int,
        frame_count: int = 10,
    ) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            base = next(ImageSequence.Iterator(image)).convert("RGBA") if self.bot.image_tools.is_gif(image, filename) else image.convert("RGBA")
            frames: list[Image.Image] = []
            for _ in range(frame_count):
                x_shift = random.randint(-10, 10)
                y_shift = random.randint(-10, 10)
                shifted = Image.new("RGBA", base.size, (0, 0, 0, 0))
                shifted.paste(base, (x_shift, y_shift), base)
                frames.append(shifted)
            output = io.BytesIO()
            frames[0].save(output, format="GIF", append_images=frames[1:], save_all=True, duration=max(10, speed), loop=0, disposal=2)
            return output.getvalue(), "shaky.gif"

    def _convert_bytes(
        self,
        image_bytes: bytes,
        target_format: str,
    ) -> tuple[bytes, str]:
        target_format = target_format.lower()
        allowed_types = {"jpg", "jpeg", "png", "bmp", "gif", "webp"}
        if target_format not in allowed_types:
            raise ValueError(f"Invalid format. Allowed formats: {', '.join(sorted(allowed_types))}")

        with Image.open(io.BytesIO(image_bytes)) as image:
            output = io.BytesIO()
            if target_format == "gif":
                frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(image)] if getattr(image, "is_animated", False) else [image.convert("RGBA")]
                frames[0].save(
                    output,
                    format="GIF",
                    append_images=frames[1:],
                    save_all=True,
                    loop=image.info.get("loop", 0),
                    duration=image.info.get("duration", 100),
                    disposal=2,
                )
                return output.getvalue(), "converted.gif"

            converted = image.convert("RGB") if target_format in {"jpg", "jpeg", "bmp"} else image.convert("RGBA")
            converted.save(output, format="JPEG" if target_format in {"jpg", "jpeg"} else target_format.upper())
            ext = "jpg" if target_format == "jpeg" else target_format
            return output.getvalue(), f"converted.{ext}"

    def _extract_bytes(self, image_bytes: bytes, filename: str) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            output = io.BytesIO()
            if self.bot.image_tools.is_gif(image, filename):
                frames = [frame.copy().convert("RGBA") for frame in ImageSequence.Iterator(image)]
                frames[0].save(output, format="GIF", append_images=frames[1:], **self.bot.image_tools.gif_save_kwargs(image))
                return output.getvalue(), "extracted.gif"

            image.save(output, format=image.format or "PNG")
            ext = (image.format or "PNG").lower()
            return output.getvalue(), f"extracted.{ext}"

    # ---------- quote helpers ----------

    def _asset_path(self, filename: str) -> Path:
        path = self.assets_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing asset: {filename}. Put the legacy overlay PNGs in `assets/images/`.")
        return path

    async def _build_quote_image(self, message: discord.Message) -> tuple[bytes, str]:
        content = (message.content or "").strip()
        if not content:
            raise ValueError("That message does not have any text to quote.")

        if len(content) > QUOTE_MAX_INPUT_LENGTH:
            content = content[:QUOTE_MAX_INPUT_LENGTH].rstrip() + "..."

        avatar = message.author.display_avatar.replace(size=512)
        avatar_bytes = await avatar.read()

        with Image.open(io.BytesIO(avatar_bytes)) as avatar_image:
            profile_pic = avatar_image.convert("RGBA").resize((400, 400), Image.LANCZOS)

        vig = None
        vig_path = self.assets_dir / "vig.png"
        if vig_path.exists():
            with Image.open(vig_path) as vig_image:
                vig = vig_image.convert("RGBA").resize((400, 400), Image.LANCZOS)

        if vig is not None:
            img = Image.alpha_composite(profile_pic, vig)
        else:
            img = profile_pic

        img = img.convert("RGB").filter(ImageFilter.GaussianBlur(radius=5)).convert("RGBA")

        overlay = Image.new("RGBA", img.size, (0, 0, 0, 110))
        img = Image.alpha_composite(img, overlay)

        draw = ImageDraw.Draw(img)
        font = self._get_quote_font(23)
        base_content = content
        truncate_len = len(base_content)
        text = f'"{base_content}"\n\n- {message.author.display_name}'
        max_width = int(img.width * 0.9)
        lines = self._wrap_text(draw, text, font, max_width)

        # Shrink toward a shorter quote until it fits in 11 lines. Cuts by whichever
        # is bigger of 10 chars or 10% of what's left, so this converges quickly even
        # from the full QUOTE_MAX_INPUT_LENGTH, and is capped to guarantee it always
        # terminates rather than looping on a string that can't shrink further.
        attempts = 0
        while len(lines) > 11 and truncate_len > 20 and attempts < QUOTE_MAX_SHRINK_ATTEMPTS:
            attempts += 1
            truncate_len = max(20, truncate_len - max(10, truncate_len // 10))
            shorter = base_content[:truncate_len].rstrip() + "..."
            text = f'"{shorter}"\n\n- {message.author.display_name}'
            lines = self._wrap_text(draw, text, font, max_width)

        line_metrics = []
        total_height = 0
        for line in lines:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            line_metrics.append((line, line_width, line_height))
            total_height += line_height + 6
        total_height = max(0, total_height - 6)

        y_text = img.height // 2 - total_height // 2
        shadow_offset = 2

        for line, line_width, line_height in line_metrics:
            x_text = img.width // 2 - line_width // 2
            for shadow_x, shadow_y in [
                (x_text - shadow_offset, y_text - shadow_offset),
                (x_text + shadow_offset, y_text + shadow_offset),
                (x_text + shadow_offset, y_text - shadow_offset),
                (x_text - shadow_offset, y_text + shadow_offset),
            ]:
                draw.text((shadow_x, shadow_y), line, font=font, fill=(0, 0, 0, 255))
            draw.text((x_text, y_text), line, font=font, fill=(255, 255, 255, 255))
            y_text += line_height + 6

        output = io.BytesIO()
        img.save(output, "PNG")
        return output.getvalue(), "quote.png"

    # ---------- slash commands ----------

    @misc.command(name="imageinfo", description="Show what the image cog can do.")
    async def imageinfo(self, interaction: discord.Interaction) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "imageinfo"):
            return

        fields = [
            self.bot.embeds.field(
                "Transforms",
                "`/image transform rotate/stretch/pull/squeeze/squash/swirl/shake/flip/flop/crop`",
            ),
            self.bot.embeds.field("Formats", "`/image effects jpegify/convert/extract`"),
            self.bot.embeds.field("Meme Text", "`/image text toptext/middletext/bottomtext` (auto-sized Impact text)"),
            self.bot.embeds.field(
                "Meme Formats",
                "`/image text caption` (white box caption bar) • `/image text motivate` (demotivational poster)",
            ),
            self.bot.embeds.field(
                "Template Overlays",
                "`/image template chimp/cooked/doom/craftify/halflife/murica/point/northkorea`",
            ),
            self.bot.embeds.field(
                "Face & Compositing",
                "`/image effects overlay/facebox/blurfaces/facecrop/contentaware`",
            ),
            self.bot.embeds.field("Source Priority", "Attachment → message link → selected source → latest image in channel"),
            self.bot.embeds.field("Quote", "`/image text quote <message_link>` only uses a Discord message link."),
            self.bot.embeds.field("Other", "`/image effects deepfry` `/image misc pfp` `/image misc clearimagesource`"),
        ]
        await self.bot.embeds.respond(interaction, title="Image Commands", fields=fields)

    @text.command(name="quote", description="Create a quote image from a Discord message link.")
    @app_commands.describe(message_link="Paste a Discord message link.")
    @app_commands.checks.cooldown(1, 5.0)
    async def quote(self, interaction: discord.Interaction, message_link: str) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "quote"):
            return

        try:
            message = await self.bot.image_tools.fetch_message_from_link(interaction, message_link)
            await self.bot.image_tools.defer(interaction)
            data, filename = await self._build_quote_image(message)
            await self.bot.image_tools.send_processed_file(
                interaction,
                data=data,
                filename=filename,
                title="Quote",
                description=f"Quoted message from {message.author.mention}.",
            )
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Quote Failed", str(exc))

    @effects.command(name="extract", description="Re-upload the selected image or GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def extract(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "extract"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self._extract_bytes(image_bytes, filename)
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Extracted Image")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Extract Failed", str(exc))

    @effects.command(name="jpegify", description="Crush image quality into cursed JPEG mush.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        quality="JPEG quality from 1 to 30.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def jpegify(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        quality: app_commands.Range[int, 1, 30] = 1,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "jpegify"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._jpegify_frame(frame, quality),
                static_format="PNG",
                static_name="jpegified.png",
                gif_name="jpegified.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="JPEGified")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "JPEGify Failed", str(exc))

    @transform.command(name="rotate", description="Rotate an image or GIF.")
    @app_commands.describe(
        angle="Rotation angle in degrees.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def rotate(
        self,
        interaction: discord.Interaction,
        angle: float,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "rotate"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: frame.rotate(angle, resample=Image.BICUBIC, expand=True),
                static_format="PNG",
                static_name="rotated.png",
                gif_name="rotated.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Rotated")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Rotate Failed", str(exc))

    @transform.command(name="stretch", description="Double an image's width.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def stretch(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "stretch"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_x(frame, 2.0),
                static_format="PNG",
                static_name="stretched.png",
                gif_name="stretched.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Stretched")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Stretch Failed", str(exc))

    @transform.command(name="pull", description="Double an image's height.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def pull(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "pull"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_y(frame, 2.0),
                static_format="PNG",
                static_name="pulled.png",
                gif_name="pulled.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Pulled")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Pull Failed", str(exc))

    @transform.command(name="squeeze", description="Halve an image's width.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def squeeze(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "squeeze"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_x(frame, 0.5),
                static_format="PNG",
                static_name="squeezed.png",
                gif_name="squeezed.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Squeezed")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Squeeze Failed", str(exc))

    @transform.command(name="squash", description="Halve an image's height.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def squash(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "squash"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._resize_y(frame, 0.5),
                static_format="PNG",
                static_name="squashed.png",
                gif_name="squashed.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Squashed")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Squash Failed", str(exc))

    @transform.command(name="swirl", description="Apply a swirl effect to an image or GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        degrees="How strong the swirl should be.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def swirl(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        degrees: app_commands.Range[int, 45, 720] = 180,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "swirl"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "swirl")

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            def work() -> tuple[bytes, str]:
                return self.bot.image_tools.process_image_bytes(
                    image_bytes,
                    filename,
                    lambda frame: self._swirl_frame(frame, float(degrees)),
                    static_format="PNG",
                    static_name="swirl.png",
                    gif_name="swirl.gif",
                )

            output, out_name = await self._run_cpu_bound(work)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Swirled")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Swirl Failed", str(exc))

    @transform.command(name="shake", description="Turn an image into a shaky GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        speed="Frame duration in milliseconds.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def shake(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        speed: app_commands.Range[int, 10, 250] = 50,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "shake"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "shake")

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = await self._run_cpu_bound(lambda: self._shake_bytes(image_bytes, filename, speed))
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Shaken")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Shake Failed", str(exc))

    @effects.command(name="convert", description="Convert an image into another format.")
    @app_commands.describe(
        format="Target format: png, jpg, jpeg, bmp, gif, or webp.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def convert(
        self,
        interaction: discord.Interaction,
        format: str,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "convert"):
            return

        try:
            image_bytes, _ = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self._convert_bytes(image_bytes, format)
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(
                interaction,
                data=output,
                filename=out_name,
                title="Converted",
                description=f"Converted to `{format.lower()}`.",
            )
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Convert Failed", str(exc))

    @effects.command(name="deepfry", description="Deepfry an image or GIF.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        sharpen_passes="How aggressive the deepfry should be.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def deepfry(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        sharpen_passes: app_commands.Range[int, 1, 20] = 12,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "deepfry"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "deepfry")

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            def work() -> tuple[bytes, str]:
                return self.bot.image_tools.process_image_bytes(
                    image_bytes,
                    filename,
                    lambda frame: self._deepfry_frame(frame, sharpen_passes),
                    static_format="JPEG",
                    static_name="deepfried.jpg",
                    gif_name="deepfried.gif",
                )

            output, out_name = await self._run_cpu_bound(work)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Deepfried")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Deepfry Failed", str(exc))

    @text.command(name="toptext", description="Add meme text to the top of an image or GIF.")
    @app_commands.describe(
        text="The text to place.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        max_size="Optional cap on font size. Left unset, text fills as much space as it can.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def toptext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        max_size: app_commands.Range[int, MEME_TEXT_MIN_SIZE, MEME_TEXT_MAX_SIZE] | None = None,
    ) -> None:
        await self._text_command(interaction, "toptext", text, "top", attachment, message_link, max_size)

    @text.command(name="middletext", description="Add meme text to the middle of an image or GIF.")
    @app_commands.describe(
        text="The text to place.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        max_size="Optional cap on font size. Left unset, text fills as much space as it can.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def middletext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        max_size: app_commands.Range[int, MEME_TEXT_MIN_SIZE, MEME_TEXT_MAX_SIZE] | None = None,
    ) -> None:
        await self._text_command(interaction, "middletext", text, "middle", attachment, message_link, max_size)

    @text.command(name="bottomtext", description="Add meme text to the bottom of an image or GIF.")
    @app_commands.describe(
        text="The text to place.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
        max_size="Optional cap on font size. Left unset, text fills as much space as it can.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def bottomtext(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        max_size: app_commands.Range[int, MEME_TEXT_MIN_SIZE, MEME_TEXT_MAX_SIZE] | None = None,
    ) -> None:
        await self._text_command(interaction, "bottomtext", text, "bottom", attachment, message_link, max_size)

    async def _text_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        text: str,
        position: str,
        attachment: discord.Attachment | None,
        message_link: str | None,
        max_size: int | None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: self._draw_meme_text(frame, text, position=position, max_size=max_size),
                static_format="PNG",
                static_name=f"{position}text.png",
                gif_name=f"{position}text.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title=f"{position.title()} Text Added")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Text Failed", str(exc))

    # ---------- white box caption ----------

    def _caption_frame(
        self,
        frame: Image.Image,
        font: ImageFont.FreeTypeFont | ImageFont.ImageFont,
        wrapped: list[str],
        text_height: int,
        box_height: int,
        *,
        position: str,
    ) -> Image.Image:
        frame = frame.convert("RGBA")
        canvas = Image.new("RGBA", (frame.width, frame.height + box_height), (255, 255, 255, 255))
        if position == "top":
            canvas.paste(frame, (0, box_height), frame)
            text_area_top = 0
        else:
            canvas.paste(frame, (0, 0), frame)
            text_area_top = frame.height

        draw = ImageDraw.Draw(canvas)
        y = text_area_top + max(0, (box_height - text_height) // 2)
        for line in wrapped:
            bbox = draw.textbbox((0, 0), line, font=font)
            line_width = bbox[2] - bbox[0]
            line_height = bbox[3] - bbox[1]
            x = (canvas.width - line_width) // 2
            draw.text((x, y), line, font=font, fill=(0, 0, 0, 255))
            y += line_height + LINE_SPACING
        return canvas

    def _caption_bytes(self, image_bytes: bytes, filename: str, text: str, *, position: str) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            is_gif = self.bot.image_tools.is_gif(image, filename)
            first_frame = (
                next(ImageSequence.Iterator(image)).convert("RGBA") if is_gif else image.convert("RGBA")
            )

            # A throwaway 1x1 image just to get a Draw object for measuring text -
            # PIL's text measurement doesn't depend on the underlying image's size.
            probe_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            margin_x = max(MEME_TEXT_MIN_MARGIN_PX, int(first_frame.width * CAPTION_SIDE_MARGIN_RATIO))
            max_width = max(1, first_frame.width - 2 * margin_x)
            max_height = max(1, int(first_frame.height * CAPTION_MAX_BOX_HEIGHT_RATIO))

            font, wrapped = self._fit_text_to_box(
                probe_draw,
                text,
                self._get_meme_font,
                max_width=max_width,
                max_height=max_height,
                min_size=CAPTION_MIN_FONT_SIZE,
                max_size=CAPTION_MAX_FONT_SIZE,
            )
            _, text_height = self._measure_lines(probe_draw, wrapped, font)
            box_height = max(
                int(first_frame.height * CAPTION_MIN_BOX_HEIGHT_RATIO),
                text_height + CAPTION_VERTICAL_PADDING_PX * 2,
            )

            if is_gif:
                frames = [
                    self._caption_frame(frame, font, wrapped, text_height, box_height, position=position)
                    for frame in ImageSequence.Iterator(image)
                ]
                output = io.BytesIO()
                frames[0].save(output, format="GIF", append_images=frames[1:], **self.bot.image_tools.gif_save_kwargs(image))
                return output.getvalue(), "caption.gif"

            output = io.BytesIO()
            rendered = self._caption_frame(first_frame, font, wrapped, text_height, box_height, position=position)
            rendered.save(output, "PNG")
            return output.getvalue(), "caption.png"

    @text.command(
        name="caption",
        description="Add a white caption bar with black text above or below an image - classic meme format.",
    )
    @app_commands.describe(
        text="The caption text.",
        position="Where to put the caption bar (default: top).",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.choices(
        position=[
            app_commands.Choice(name="top", value="top"),
            app_commands.Choice(name="bottom", value="bottom"),
        ]
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def caption(
        self,
        interaction: discord.Interaction,
        text: app_commands.Range[str, 1, 250],
        position: app_commands.Choice[str] | None = None,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "caption"):
            return

        chosen_position = position.value if position else "top"
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self._caption_bytes(image_bytes, filename, text, position=chosen_position)
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Captioned")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Caption Failed", str(exc))

    # ---------- motivational poster ----------

    def _build_motivate_image(
        self,
        image_bytes: bytes,
        filename: str,
        title: str,
        subtitle: str | None,
    ) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            is_gif = self.bot.image_tools.is_gif(image, filename)
            first_frame = (
                next(ImageSequence.Iterator(image)).convert("RGBA") if is_gif else image.convert("RGBA")
            )

            scale = MOTIVATE_PHOTO_WIDTH / first_frame.width
            target_height = max(1, int(first_frame.height * scale))
            border = MOTIVATE_BORDER_PX
            framed_size = (MOTIVATE_PHOTO_WIDTH + border * 2, target_height + border * 2)

            probe_draw = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
            text_max_width = framed_size[0]

            title_font, title_lines = self._fit_text_to_box(
                probe_draw,
                title.upper(),
                self._get_meme_font,
                max_width=text_max_width,
                max_height=MOTIVATE_TITLE_MAX_HEIGHT,
                min_size=MOTIVATE_TITLE_MIN_SIZE,
                max_size=MOTIVATE_TITLE_MAX_SIZE,
            )
            _, title_height = self._measure_lines(probe_draw, title_lines, title_font)

            subtitle_font = None
            subtitle_lines: list[str] = []
            subtitle_height = 0
            if subtitle:
                subtitle_font, subtitle_lines = self._fit_text_to_box(
                    probe_draw,
                    subtitle,
                    self._get_quote_font,
                    max_width=text_max_width,
                    max_height=MOTIVATE_SUBTITLE_MAX_HEIGHT,
                    min_size=MOTIVATE_SUBTITLE_MIN_SIZE,
                    max_size=MOTIVATE_SUBTITLE_MAX_SIZE,
                )
                _, subtitle_height = self._measure_lines(probe_draw, subtitle_lines, subtitle_font)

            canvas_width = framed_size[0] + MOTIVATE_MARGIN_PX * 2
            canvas_height = (
                MOTIVATE_MARGIN_PX
                + framed_size[1]
                + MOTIVATE_TEXT_GAP_PX
                + title_height
                + (MOTIVATE_TEXT_GAP_PX + subtitle_height if subtitle_lines else 0)
                + MOTIVATE_MARGIN_PX
            )

            def render(photo_frame: Image.Image) -> Image.Image:
                photo = photo_frame.convert("RGBA").resize((MOTIVATE_PHOTO_WIDTH, target_height), Image.LANCZOS)
                framed = Image.new("RGBA", framed_size, (255, 255, 255, 255))
                framed.paste(photo, (border, border))

                canvas = Image.new("RGBA", (canvas_width, canvas_height), (0, 0, 0, 255))
                canvas.paste(framed, (MOTIVATE_MARGIN_PX, MOTIVATE_MARGIN_PX))

                draw = ImageDraw.Draw(canvas)
                y = MOTIVATE_MARGIN_PX + framed_size[1] + MOTIVATE_TEXT_GAP_PX
                for line in title_lines:
                    bbox = draw.textbbox((0, 0), line, font=title_font)
                    line_width = bbox[2] - bbox[0]
                    line_height = bbox[3] - bbox[1]
                    x = (canvas_width - line_width) // 2
                    draw.text((x, y), line, font=title_font, fill=(255, 255, 255, 255))
                    y += line_height + LINE_SPACING

                if subtitle_lines:
                    y += MOTIVATE_TEXT_GAP_PX - LINE_SPACING
                    for line in subtitle_lines:
                        bbox = draw.textbbox((0, 0), line, font=subtitle_font)
                        line_width = bbox[2] - bbox[0]
                        line_height = bbox[3] - bbox[1]
                        x = (canvas_width - line_width) // 2
                        draw.text((x, y), line, font=subtitle_font, fill=(190, 190, 190, 255))
                        y += line_height + LINE_SPACING

                return canvas

            if not is_gif:
                output = io.BytesIO()
                render(first_frame).convert("RGB").save(output, "PNG")
                return output.getvalue(), "motivate.png"

            frames = [render(frame) for frame in ImageSequence.Iterator(image)]
            output = io.BytesIO()
            frames[0].save(
                output, format="GIF", append_images=frames[1:], **self.bot.image_tools.gif_save_kwargs(image)
            )
            return output.getvalue(), "motivate.gif"

    @text.command(
        name="motivate",
        description="Turn an image into a classic black-background 'motivational' poster meme.",
    )
    @app_commands.describe(
        title="Bold caption below the image.",
        subtitle="Optional smaller line below the title.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def motivate(
        self,
        interaction: discord.Interaction,
        title: app_commands.Range[str, 1, 100],
        subtitle: app_commands.Range[str, 1, 150] | None = None,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "motivate"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self._build_motivate_image(image_bytes, filename, title, subtitle)
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Motivate")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Motivate Failed", str(exc))

    @transform.command(name="flip", description="Flip an image vertically.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def flip(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "flip"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: ImageOps.flip(frame),
                static_format="JPEG",
                static_name="flipped.jpg",
                gif_name="flipped.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Flipped")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Flip Failed", str(exc))

    @transform.command(name="flop", description="Flip an image horizontally.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def flop(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "flop"):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                lambda frame: ImageOps.mirror(frame),
                static_format="JPEG",
                static_name="flopped.jpg",
                gif_name="flopped.gif",
            )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Flopped")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Flop Failed", str(exc))

    @misc.command(name="pfp", description="Show a user's profile picture as a file.")
    @app_commands.describe(user="The user whose avatar you want.")
    @app_commands.checks.cooldown(1, 5.0)
    async def pfp(self, interaction: discord.Interaction, user: discord.User | discord.Member) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "pfp"):
            return

        try:
            await self.bot.image_tools.defer(interaction)
            avatar = user.display_avatar.replace(size=1024)
            avatar_bytes = await avatar.read()
            filename = f"{user.name}.gif" if avatar.url.lower().endswith(".gif") else f"{user.name}.png"
            file = discord.File(io.BytesIO(avatar_bytes), filename=filename)
            embed = self.bot.embeds.info_embed("Profile Picture", f"{user.mention}'s avatar.")
            embed.set_image(url=f"attachment://{filename}")
            await interaction.followup.send(embed=embed, file=file)
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Avatar Failed", str(exc))

    @template.command(name="chimp", description="Append the chimp panel to the bottom of an image.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def chimp(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._append_template_command(interaction, "chimp", "chimp.png", attachment, message_link)

    @template.command(name="cooked", description="Append the cooked panel to the bottom of an image.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def cooked(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._append_template_command(interaction, "cooked", "cooked.png", attachment, message_link)

    async def _append_template_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        asset_name: str,
        attachment: discord.Attachment | None,
        message_link: str | None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            asset_path = self._asset_path(asset_name)
            with Image.open(asset_path) as append_image:
                output, out_name = self.bot.image_tools.append_image_to(
                    image_bytes, filename, append_image, placement="bottom", out_stem=asset_path.stem
                )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title=command_name.title())
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, f"{command_name.title()} Failed", str(exc))

    @template.command(name="doom", description="Overlay the classic doom UI.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def doom(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "doom", "doomui.png", attachment, message_link, placement="bottom", strategy="fit")

    @template.command(name="craftify", description="Overlay the crafty template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def craftify(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "craftify", "crafty.png", attachment, message_link, placement="bottom", strategy="fit")

    @template.command(name="halflife", description="Overlay the Half-Life HUD template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def halflife(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "halflife", "hl2.png", attachment, message_link, placement="bottom", strategy="fit")

    @template.command(name="murica", description="Stretch the patriotic overlay across the image.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def murica(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "murica", "murica.png", attachment, message_link, placement="center", strategy="stretch")

    @template.command(name="point", description="Overlay the soyjak pointing template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def point(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "point", "point.png", attachment, message_link, placement="center", strategy="stretch")

    @template.command(name="northkorea", description="Overlay the authoritarian template.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 5.0)
    async def northkorea(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        await self._overlay_template_command(interaction, "northkorea", "nk.png", attachment, message_link, placement="center", strategy="stretch")

    async def _overlay_template_command(
        self,
        interaction: discord.Interaction,
        command_name: str,
        asset_name: str,
        attachment: discord.Attachment | None,
        message_link: str | None,
        *,
        placement: str,
        strategy: str,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, command_name):
            return

        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            asset_path = self._asset_path(asset_name)
            with Image.open(asset_path) as overlay_image:
                output, out_name = self.bot.image_tools.overlay_image_onto(
                    image_bytes,
                    filename,
                    overlay_image,
                    placement=placement,
                    strategy=strategy,
                    opacity=1.0,
                    out_stem=asset_path.stem,
                )
            await self.bot.image_tools.defer(interaction)
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title=command_name.title())
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, f"{command_name.title()} Failed", str(exc))

    # ---------- second-image compositing, cropping, face detection, content-aware scale ----------
    #
    # No local error handler attached to these six (see the on_app_command_error
    # docstring note below) - CommandOnCooldown/TransformerError already get
    # identical messages from the global handler in main.py, so a local override
    # here would be pure duplication.

    @effects.command(name="overlay", description="Composite a second image on top of your image or GIF.")
    @app_commands.describe(
        overlay_attachment="The image to place on top (or use overlay_message_link).",
        overlay_message_link="Discord message link containing the overlay image.",
        attachment="Optional base image attachment.",
        message_link="Optional Discord message link containing the base image.",
        placement="Where to anchor the overlay on the base image.",
        scale_percent="Overlay width as a percent of the base image's width.",
        opacity="Overlay opacity percent.",
    )
    @app_commands.choices(placement=[app_commands.Choice(name=name, value=name) for name in OVERLAY_PLACEMENTS])
    @app_commands.checks.cooldown(1, 5.0)
    async def overlay(
        self,
        interaction: discord.Interaction,
        overlay_attachment: discord.Attachment | None = None,
        overlay_message_link: str | None = None,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
        placement: app_commands.Choice[str] | None = None,
        scale_percent: app_commands.Range[int, 5, 200] = 40,
        opacity: app_commands.Range[int, 1, 100] = 100,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "overlay"):
            return

        if overlay_attachment is None and not overlay_message_link:
            await self.bot.image_tools.send_error(
                interaction,
                "Missing Overlay Image",
                "Provide `overlay_attachment` or `overlay_message_link` for the image to place on top.",
            )
            return

        await self.bot.image_tools.defer(interaction)
        try:
            base_bytes, base_filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)
            overlay_bytes, _ = await self.bot.image_tools.get_source_image(
                interaction, overlay_attachment, overlay_message_link
            )

            with Image.open(io.BytesIO(overlay_bytes)) as overlay_image:
                output, out_name = self.bot.image_tools.overlay_image_onto(
                    base_bytes,
                    base_filename,
                    overlay_image,
                    placement=placement.value if placement else "bottomright",
                    strategy="scale",
                    opacity=opacity / 100.0,
                    scale_percent=float(scale_percent),
                    out_stem="overlay",
                )
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Overlaid")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Overlay Failed", str(exc))

    @transform.command(name="crop", description="Crop an image or GIF by trimming a percent from each side.")
    @app_commands.describe(
        left="Percent to trim from the left edge.",
        top="Percent to trim from the top edge.",
        right="Percent to trim from the right edge.",
        bottom="Percent to trim from the bottom edge.",
        square="Crop to a centered square instead (ignores left/top/right/bottom).",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 3.0)
    async def crop(
        self,
        interaction: discord.Interaction,
        left: app_commands.Range[int, 0, 45] = 0,
        top: app_commands.Range[int, 0, 45] = 0,
        right: app_commands.Range[int, 0, 45] = 0,
        bottom: app_commands.Range[int, 0, 45] = 0,
        square: bool = False,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "crop"):
            return

        if not square and left == 0 and top == 0 and right == 0 and bottom == 0:
            await self.bot.image_tools.send_error(
                interaction,
                "Nothing To Crop",
                "Set at least one of `left`/`top`/`right`/`bottom`, or use `square: True`.",
            )
            return

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            def crop_frame(frame: Image.Image) -> Image.Image:
                width, height = frame.size
                if square:
                    side = min(width, height)
                    x0 = (width - side) // 2
                    y0 = (height - side) // 2
                    return frame.crop((x0, y0, x0 + side, y0 + side))

                x0 = int(width * left / 100)
                y0 = int(height * top / 100)
                x1 = max(x0 + 1, width - int(width * right / 100))
                y1 = max(y0 + 1, height - int(height * bottom / 100))
                return frame.crop((x0, y0, x1, y1))

            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                crop_frame,
                static_format="PNG",
                static_name="cropped.png",
                gif_name="cropped.gif",
            )
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Cropped")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Crop Failed", str(exc))

    @effects.command(name="facebox", description="Draw a box around each detected face.")
    @app_commands.describe(
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 10.0)
    async def facebox(
        self,
        interaction: discord.Interaction,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "facebox"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "facebox")

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            def draw_boxes(frame: Image.Image, boxes: list[tuple[int, int, int, int]]) -> Image.Image:
                frame = frame.convert("RGBA")
                draw = ImageDraw.Draw(frame)
                for index, (x1, y1, x2, y2) in enumerate(boxes, start=1):
                    line_width = max(2, int((x2 - x1) * 0.02))
                    draw.rectangle((x1, y1, x2, y2), outline=(255, 32, 32, 255), width=line_width)
                    draw.text((x1 + 4, max(0, y1 - 18)), f"Face {index}", fill=(255, 32, 32, 255))
                return frame

            def work() -> tuple[bytes, str] | None:
                with Image.open(io.BytesIO(image_bytes)) as image:
                    is_gif = self.bot.image_tools.is_gif(image, filename)
                    first_frame = (
                        next(ImageSequence.Iterator(image)).convert("RGBA") if is_gif else image.convert("RGBA")
                    )
                    boxes = self._detect_faces_in_frame(first_frame)
                    if not boxes:
                        return None

                    if not is_gif:
                        output = io.BytesIO()
                        draw_boxes(first_frame, boxes).convert("RGB").save(output, format="PNG")
                        return output.getvalue(), "facebox.png"

                    frames = [draw_boxes(frame, boxes) for frame in ImageSequence.Iterator(image)]
                    output = io.BytesIO()
                    frames[0].save(output, format="GIF", append_images=frames[1:], **self.bot.image_tools.gif_save_kwargs(image))
                    return output.getvalue(), "facebox.gif"

            result = await self._run_cpu_bound(work)
            if result is None:
                await self.bot.image_tools.send_error(
                    interaction, "No Faces Found", "I could not detect any faces in that image."
                )
                return

            data, out_name = result
            await self.bot.image_tools.send_processed_file(
                interaction,
                data=data,
                filename=out_name,
                title="Face Detection",
                description="Faces are detected once on the first frame; GIF frames reuse those box positions.",
            )
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Face Detection Failed", str(exc))

    @effects.command(name="blurfaces", description="Blur or pixelate every detected face.")
    @app_commands.describe(
        style="Pixelate (blocky censor look) or blur (soft).",
        strength="How strong the effect is.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.choices(
        style=[
            app_commands.Choice(name="pixelate", value="pixelate"),
            app_commands.Choice(name="blur", value="blur"),
        ]
    )
    @app_commands.checks.cooldown(1, 10.0)
    async def blurfaces(
        self,
        interaction: discord.Interaction,
        style: app_commands.Choice[str] | None = None,
        strength: app_commands.Range[int, 1, 20] = 8,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "blurfaces"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "blurfaces")

        await self.bot.image_tools.defer(interaction)
        chosen_style = style.value if style else "pixelate"
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            boxes = await self._detect_faces(image_bytes, filename)
            if not boxes:
                await self.bot.image_tools.send_error(
                    interaction, "No Faces Found", "I could not detect any faces in that image."
                )
                return

            def censor_frame(frame: Image.Image) -> Image.Image:
                frame = frame.convert("RGBA")
                for x1, y1, x2, y2 in boxes:
                    x2, y2 = min(x2, frame.width), min(y2, frame.height)
                    if x2 <= x1 or y2 <= y1:
                        continue
                    region = frame.crop((x1, y1, x2, y2))
                    if chosen_style == "blur":
                        region = region.filter(ImageFilter.GaussianBlur(radius=max(1, strength)))
                    else:
                        factor = max(1, strength)
                        small = region.resize(
                            (max(1, region.width // factor), max(1, region.height // factor)), Image.NEAREST
                        )
                        region = small.resize(region.size, Image.NEAREST)
                    frame.paste(region, (x1, y1))
                return frame

            def work() -> tuple[bytes, str]:
                return self.bot.image_tools.process_image_bytes(
                    image_bytes,
                    filename,
                    censor_frame,
                    static_format="PNG",
                    static_name="blurfaces.png",
                    gif_name="blurfaces.gif",
                )

            output, out_name = await self._run_cpu_bound(work)
            await self.bot.image_tools.send_processed_file(
                interaction, data=output, filename=out_name, title="Faces Censored"
            )
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Blur Faces Failed", str(exc))

    @effects.command(name="facecrop", description="Crop tightly to the largest detected face.")
    @app_commands.describe(
        padding_percent="Extra padding around the face as a percent of its size.",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 10.0)
    async def facecrop(
        self,
        interaction: discord.Interaction,
        padding_percent: app_commands.Range[int, 0, 100] = 30,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "facecrop"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "facecrop")

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            boxes = await self._detect_faces(image_bytes, filename)
            if not boxes:
                await self.bot.image_tools.send_error(
                    interaction, "No Faces Found", "I could not detect any faces in that image."
                )
                return

            x1, y1, x2, y2 = max(boxes, key=lambda box: (box[2] - box[0]) * (box[3] - box[1]))
            pad_x = int((x2 - x1) * padding_percent / 100)
            pad_y = int((y2 - y1) * padding_percent / 100)

            def crop_frame(frame: Image.Image) -> Image.Image:
                fx1 = max(0, x1 - pad_x)
                fy1 = max(0, y1 - pad_y)
                fx2 = min(frame.width, x2 + pad_x)
                fy2 = min(frame.height, y2 + pad_y)
                return frame.crop((fx1, fy1, fx2, fy2))

            output, out_name = self.bot.image_tools.process_image_bytes(
                image_bytes,
                filename,
                crop_frame,
                static_format="PNG",
                static_name="facecrop.png",
                gif_name="facecrop.gif",
            )
            await self.bot.image_tools.send_processed_file(interaction, data=output, filename=out_name, title="Face Crop")
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Face Crop Failed", str(exc))

    @effects.command(
        name="contentaware",
        description="Squish an image or GIF with content-aware seam carving until it looks deliberately stupid.",
    )
    @app_commands.describe(
        width_percent="Target width as a percent of the original (lower = more squish).",
        height_percent="Target height as a percent of the original (lower = more squish).",
        attachment="Optional image attachment.",
        message_link="Optional Discord message link containing the image.",
    )
    @app_commands.checks.cooldown(1, 20.0)
    async def contentaware(
        self,
        interaction: discord.Interaction,
        width_percent: app_commands.Range[int, CONTENTAWARE_MIN_PERCENT, CONTENTAWARE_MAX_PERCENT] = 50,
        height_percent: app_commands.Range[int, CONTENTAWARE_MIN_PERCENT, CONTENTAWARE_MAX_PERCENT] = 50,
        attachment: discord.Attachment | None = None,
        message_link: str | None = None,
    ) -> None:
        if not await self.bot.image_tools.ensure_command_allowed(interaction, "contentaware"):
            return
        await self.bot.image_tools.note_heavy_command(interaction, "contentaware")

        await self.bot.image_tools.defer(interaction)
        try:
            image_bytes, filename = await self.bot.image_tools.get_source_image(interaction, attachment, message_link)

            def work() -> tuple[bytes, str]:
                with Image.open(io.BytesIO(image_bytes)) as image:
                    is_gif = self.bot.image_tools.is_gif(image, filename)
                    first_frame = (
                        next(ImageSequence.Iterator(image)).convert("RGB") if is_gif else image.convert("RGB")
                    )

                    longest_edge = max(first_frame.width, first_frame.height)
                    if longest_edge > CONTENTAWARE_MAX_DIMENSION:
                        scale = CONTENTAWARE_MAX_DIMENSION / longest_edge
                        working_size = (
                            max(1, int(first_frame.width * scale)),
                            max(1, int(first_frame.height * scale)),
                        )
                    else:
                        working_size = first_frame.size

                    source = (
                        first_frame.resize(working_size, Image.LANCZOS)
                        if working_size != first_frame.size
                        else first_frame
                    )
                    target_width = max(2, int(working_size[0] * width_percent / 100))
                    target_height = max(2, int(working_size[1] * height_percent / 100))
                    carved, width_seams, height_seams = self._seam_carve_both_axes(source, target_width, target_height)

                    if not is_gif:
                        output = io.BytesIO()
                        carved.save(output, format="PNG")
                        return output.getvalue(), "contentaware.png"

                    frames = [carved.convert("RGBA")]
                    for frame in list(ImageSequence.Iterator(image))[1:]:
                        carved_frame = self._apply_seams_both_axes(frame, working_size, width_seams, height_seams)
                        frames.append(carved_frame.convert("RGBA"))

                    output = io.BytesIO()
                    frames[0].save(output, format="GIF", append_images=frames[1:], **self.bot.image_tools.gif_save_kwargs(image))
                    return output.getvalue(), "contentaware.gif"

            data, out_name = await self._run_cpu_bound(work, timeout=CPU_BOUND_TIMEOUT_SECONDS)
            await self.bot.image_tools.send_processed_file(
                interaction,
                data=data,
                filename=out_name,
                title="Content-Aware Scale",
                description=f"Input capped to {CONTENTAWARE_MAX_DIMENSION}px on the long edge for speed.",
            )
        except Exception as exc:
            await self.bot.image_tools.send_error(interaction, "Content-Aware Scale Failed", str(exc))

    @imageinfo.error
    @quote.error
    @clear_image_source.error
    @extract.error
    @jpegify.error
    @rotate.error
    @stretch.error
    @pull.error
    @squeeze.error
    @squash.error
    @swirl.error
    @shake.error
    @convert.error
    @deepfry.error
    @toptext.error
    @middletext.error
    @bottomtext.error
    @caption.error
    @motivate.error
    @flip.error
    @flop.error
    @pfp.error
    @chimp.error
    @cooked.error
    @doom.error
    @craftify.error
    @halflife.error
    @murica.error
    @point.error
    @northkorea.error
    async def on_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ) -> None:
        if isinstance(error, app_commands.CommandOnCooldown):
            await self.bot.image_tools.send_error(
                interaction,
                "Slow Down",
                f"Try again in `{error.retry_after:.1f}` seconds.",
            )
            return

        if isinstance(error, app_commands.TransformerError):
            await self.bot.image_tools.send_error(
                interaction,
                "Invalid Value",
                "One of the command options you entered was invalid.",
            )
            return

        raise error


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Images(bot))
