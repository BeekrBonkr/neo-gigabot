from __future__ import annotations

import io
import math
import random
from pathlib import Path
from typing import Callable

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageSequence


class ImageRenderer:
    def __init__(self, project_root: Path, assets_dir: Path) -> None:
        self.project_root = project_root
        self.assets_dir = assets_dir

    def is_gif(self, image: Image.Image, filename: str | None = None) -> bool:
        fmt = (image.format or "").upper()
        if fmt == "GIF":
            return True
        if filename and filename.lower().endswith(".gif"):
            return True
        return bool(getattr(image, "is_animated", False))

    def gif_save_kwargs(self, image: Image.Image) -> dict:
        return {
            "save_all": True,
            "loop": image.info.get("loop", 0),
            "duration": image.info.get("duration", 100),
            "disposal": 2,
        }

    def get_font(self, size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
        candidates = [
            str(self.project_root / "assets" / "fonts" / "Impact.ttf"),
            str(self.project_root / "assets" / "fonts" / "impact.ttf"),
            str(self.project_root / "assets" / "fonts" / "arial.ttf"),
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
            "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
        ]
        for path in candidates:
            if Path(path).exists():
                try:
                    return ImageFont.truetype(path, size=size)
                except OSError:
                    pass
        return ImageFont.load_default()

    def wrap_text(
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

            current = words[0]
            for word in words[1:]:
                test = f"{current} {word}"
                bbox = draw.textbbox((0, 0), test, font=font, stroke_width=3)
                if bbox[2] - bbox[0] <= max_width:
                    current = test
                else:
                    lines.append(current)
                    current = word
            lines.append(current)
        return lines

    def draw_meme_text(
        self,
        image: Image.Image,
        text: str,
        *,
        position: str,
        requested_size: int = 50,
    ) -> Image.Image:
        working = image.convert("RGBA")
        draw = ImageDraw.Draw(working)

        font_size = max(12, requested_size)
        font = self.get_font(font_size)
        wrapped = self.wrap_text(draw, text, font, working.width - 20)

        def measure(lines: list[str], fnt: ImageFont.FreeTypeFont | ImageFont.ImageFont) -> tuple[int, int]:
            widths = []
            total_height = 0
            for line in lines:
                bbox = draw.textbbox((0, 0), line, font=fnt, stroke_width=3)
                widths.append(bbox[2] - bbox[0])
                total_height += bbox[3] - bbox[1] + 6
            return (max(widths) if widths else 0), max(0, total_height - 6)

        text_width, text_height = measure(wrapped, font)
        while (text_width > working.width - 20 or text_height > working.height - 20) and font_size > 12:
            font_size -= 2
            font = self.get_font(font_size)
            wrapped = self.wrap_text(draw, text, font, working.width - 20)
            text_width, text_height = measure(wrapped, font)

        if position == "top":
            y = 10
        elif position == "bottom":
            y = working.height - text_height - 20
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
            y += line_height + 6
        return working

    def process_image_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        processor: Callable[[Image.Image], Image.Image],
        *,
        static_format: str = "PNG",
        static_name: str = "image.png",
        gif_name: str = "image.gif",
    ) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            if self.is_gif(image, filename):
                frames: list[Image.Image] = []
                for frame in ImageSequence.Iterator(image):
                    processed = processor(frame.convert("RGBA"))
                    frames.append(processed.convert("RGBA"))
                output = io.BytesIO()
                frames[0].save(output, format="GIF", append_images=frames[1:], **self.gif_save_kwargs(image))
                return output.getvalue(), gif_name

            processed = processor(image.convert("RGBA"))
            output = io.BytesIO()
            save_image = processed
            if static_format.upper() in {"JPEG", "JPG", "BMP"}:
                save_image = processed.convert("RGB")
            save_image.save(output, format=static_format.upper())
            return output.getvalue(), static_name

    def resize_x(self, frame: Image.Image, factor: float) -> Image.Image:
        new_width = max(1, min(4000, int(frame.width * factor)))
        return frame.resize((new_width, frame.height), Image.LANCZOS)

    def resize_y(self, frame: Image.Image, factor: float) -> Image.Image:
        new_height = max(1, min(4000, int(frame.height * factor)))
        return frame.resize((frame.width, new_height), Image.LANCZOS)

    def jpegify_frame(self, frame: Image.Image, quality: int) -> Image.Image:
        original_size = frame.size
        rgb = frame.convert("RGB")
        temp = io.BytesIO()
        rgb.save(temp, format="JPEG", quality=max(1, min(quality, 95)))
        temp.seek(0)
        low_quality = Image.open(temp).convert("RGB")
        return low_quality.resize(original_size, Image.LANCZOS)

    def deepfry_frame(self, frame: Image.Image, sharpen_passes: int) -> Image.Image:
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

    def swirl_frame(self, frame: Image.Image, degrees: float = 180.0) -> Image.Image:
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

    def shake_bytes(self, image_bytes: bytes, filename: str, speed: int, frame_count: int = 10) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            base = next(ImageSequence.Iterator(image)).convert("RGBA") if self.is_gif(image, filename) else image.convert("RGBA")
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

    def convert_bytes(self, image_bytes: bytes, target_format: str) -> tuple[bytes, str]:
        target_format = target_format.lower()
        allowed_types = {"jpg", "jpeg", "png", "bmp", "gif", "webp"}
        if target_format not in allowed_types:
            raise ValueError(f"Invalid format. Allowed formats: {', '.join(sorted(allowed_types))}")

        with Image.open(io.BytesIO(image_bytes)) as image:
            output = io.BytesIO()
            if target_format == "gif":
                frames = [frame.convert("RGBA") for frame in ImageSequence.Iterator(image)] if getattr(image, "is_animated", False) else [image.convert("RGBA")]
                frames[0].save(output, format="GIF", append_images=frames[1:], save_all=True, loop=image.info.get("loop", 0), duration=image.info.get("duration", 100), disposal=2)
                return output.getvalue(), "converted.gif"

            converted = image.convert("RGB") if target_format in {"jpg", "jpeg", "bmp"} else image.convert("RGBA")
            converted.save(output, format="JPEG" if target_format in {"jpg", "jpeg"} else target_format.upper())
            ext = "jpg" if target_format == "jpeg" else target_format
            return output.getvalue(), f"converted.{ext}"

    def extract_bytes(self, image_bytes: bytes, filename: str) -> tuple[bytes, str]:
        with Image.open(io.BytesIO(image_bytes)) as image:
            output = io.BytesIO()
            if self.is_gif(image, filename):
                frames = [frame.copy().convert("RGBA") for frame in ImageSequence.Iterator(image)]
                frames[0].save(output, format="GIF", append_images=frames[1:], **self.gif_save_kwargs(image))
                return output.getvalue(), "extracted.gif"

            image.save(output, format=image.format or "PNG")
            ext = (image.format or "PNG").lower()
            return output.getvalue(), f"extracted.{ext}"

    def asset_path(self, filename: str) -> Path:
        path = self.assets_dir / filename
        if not path.exists():
            raise FileNotFoundError(f"Missing asset: {filename}. Put the legacy overlay PNGs in `assets/images/`.")
        return path

    def overlay_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        overlay_image_path: Path,
        *,
        placement: str = "bottom",
        strategy: str = "fit",
        opacity: float = 1.0,
    ) -> tuple[bytes, str]:
        overlay_image = Image.open(overlay_image_path).convert("RGBA")

        def overlay_to_frame(input_frame: Image.Image) -> Image.Image:
            frame = input_frame.convert("RGBA")
            if strategy == "stretch":
                resized_overlay = overlay_image.resize(frame.size, Image.LANCZOS)
            else:
                if placement in {"top", "bottom"}:
                    new_width = frame.width
                    new_height = int(overlay_image.height * new_width / overlay_image.width)
                else:
                    new_height = frame.height
                    new_width = int(overlay_image.width * new_height / overlay_image.height)
                resized_overlay = overlay_image.resize((new_width, new_height), Image.LANCZOS)

            if opacity < 1:
                alpha = resized_overlay.getchannel("A")
                alpha = ImageEnhance.Brightness(alpha).enhance(opacity)
                resized_overlay.putalpha(alpha)

            paste_coords = {
                "top": (0, 0),
                "bottom": (0, frame.height - resized_overlay.height),
                "left": (0, 0),
                "right": (frame.width - resized_overlay.width, 0),
                "center": ((frame.width - resized_overlay.width) // 2, (frame.height - resized_overlay.height) // 2),
            }
            new_frame = frame.copy()
            new_frame.paste(resized_overlay, paste_coords[placement], resized_overlay)
            return new_frame

        return self.process_image_bytes(
            image_bytes,
            filename,
            overlay_to_frame,
            static_format="PNG",
            static_name=f"{overlay_image_path.stem}.png",
            gif_name=f"{overlay_image_path.stem}.gif",
        )

    def append_bytes(
        self,
        image_bytes: bytes,
        filename: str,
        append_image_path: Path,
        *,
        placement: str = "bottom",
    ) -> tuple[bytes, str]:
        append_image = Image.open(append_image_path).convert("RGBA")

        def add_to_frame(input_frame: Image.Image) -> Image.Image:
            frame = input_frame.convert("RGBA")
            if placement in {"top", "bottom"}:
                scaled = append_image.resize((frame.width, int(frame.width * append_image.height / append_image.width)), Image.LANCZOS)
                new_width = frame.width
                new_height = frame.height + scaled.height
            else:
                scaled = append_image.resize((int(frame.height * append_image.width / append_image.height), frame.height), Image.LANCZOS)
                new_width = frame.width + scaled.width
                new_height = frame.height

            new_frame = Image.new("RGBA", (new_width, new_height), (0, 0, 0, 0))
            if placement == "bottom":
                new_frame.paste(frame, (0, 0), frame)
                new_frame.paste(scaled, (0, frame.height), scaled)
            elif placement == "top":
                new_frame.paste(scaled, (0, 0), scaled)
                new_frame.paste(frame, (0, scaled.height), frame)
            elif placement == "left":
                new_frame.paste(scaled, (0, 0), scaled)
                new_frame.paste(frame, (scaled.width, 0), frame)
            else:
                new_frame.paste(frame, (0, 0), frame)
                new_frame.paste(scaled, (frame.width, 0), scaled)
            return new_frame

        return self.process_image_bytes(
            image_bytes,
            filename,
            add_to_frame,
            static_format="PNG",
            static_name=f"{append_image_path.stem}.png",
            gif_name=f"{append_image_path.stem}.gif",
        )

    async def build_quote_image(self, message) -> tuple[bytes, str]:
        content = (message.content or "").strip()
        if len(content) > 600:
            content = content[:597].rstrip() + "..."
        if not content:
            raise ValueError("That message does not have any text to quote.")

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
        font = self.get_font(23)
        text = f'"{content}"\n\n- {message.author.display_name}'
        max_width = int(img.width * 0.9)
        lines = self.wrap_text(draw, text, font, max_width)

        while len(lines) > 11:
            shorter = content[: max(20, len(content) - 10)].rstrip() + "..."
            text = f'"{shorter}"\n\n- {message.author.display_name}'
            lines = self.wrap_text(draw, text, font, max_width)

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
