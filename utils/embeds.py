from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

import discord
from discord.ext import commands


@dataclass(slots=True)
class EmbedField:
    name: str
    value: str
    inline: bool = False


@dataclass(slots=True)
class EmbedPayload:
    title: str | None = None
    description: str | None = None
    color: discord.Color | int | None = None
    fields: list[EmbedField] = field(default_factory=list)
    footer: str | None = None
    author_name: str | None = None
    author_icon_url: str | None = None
    thumbnail_url: str | None = None
    image_url: str | None = None
    timestamp: bool = False


class EmbedManager:
    """Central helper for creating, sending, editing, and responding with embeds."""

    def __init__(self, bot: commands.Bot | None = None) -> None:
        self.bot = bot
        self.default_color = discord.Color.blurple()
        self.success_color = discord.Color.green()
        self.error_color = discord.Color.red()
        self.warning_color = discord.Color.orange()
        self.info_color = discord.Color.blurple()

    def field(self, name: str, value: str, inline: bool = False) -> EmbedField:
        return EmbedField(name=name, value=value, inline=inline)

    def payload(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        color: discord.Color | int | None = None,
        fields: Sequence[EmbedField] | None = None,
        footer: str | None = None,
        author_name: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        timestamp: bool = False,
    ) -> EmbedPayload:
        return EmbedPayload(
            title=title,
            description=description,
            color=color,
            fields=list(fields or []),
            footer=footer,
            author_name=author_name,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            timestamp=timestamp,
        )

    def create(
        self,
        *,
        title: str | None = None,
        description: str | None = None,
        color: discord.Color | int | None = None,
        fields: Sequence[EmbedField] | None = None,
        footer: str | None = None,
        author_name: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        timestamp: bool = False,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=title,
            description=description,
            color=self._coerce_color(color),
            timestamp=discord.utils.utcnow() if timestamp else None,
        )

        for field in fields or []:
            embed.add_field(name=field.name, value=field.value, inline=field.inline)

        if footer:
            embed.set_footer(text=footer)

        if author_name:
            embed.set_author(name=author_name, icon_url=author_icon_url or None)

        if thumbnail_url:
            embed.set_thumbnail(url=thumbnail_url)

        if image_url:
            embed.set_image(url=image_url)

        return embed

    def success_embed(self, title: str, description: str, **kwargs: Any) -> discord.Embed:
        return self.create(title=title, description=description, color=self.success_color, **kwargs)

    def error_embed(self, title: str, description: str, **kwargs: Any) -> discord.Embed:
        return self.create(title=title, description=description, color=self.error_color, **kwargs)

    def warning_embed(self, title: str, description: str, **kwargs: Any) -> discord.Embed:
        return self.create(title=title, description=description, color=self.warning_color, **kwargs)

    def info_embed(self, title: str, description: str, **kwargs: Any) -> discord.Embed:
        return self.create(title=title, description=description, color=self.info_color, **kwargs)

    async def send(
        self,
        target: commands.Context | discord.abc.Messageable,
        *,
        embed: discord.Embed | None = None,
        payload: EmbedPayload | None = None,
        title: str | None = None,
        description: str | None = None,
        color: discord.Color | int | None = None,
        fields: Sequence[EmbedField] | None = None,
        footer: str | None = None,
        author_name: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        timestamp: bool = False,
        content: str | None = None,
        **kwargs: Any,
    ) -> discord.Message:
        final_embed = self._resolve_embed(
            embed=embed,
            payload=payload,
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer=footer,
            author_name=author_name,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            timestamp=timestamp,
        )
        return await target.send(content=content, embed=final_embed, **kwargs)

    async def respond(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed | None = None,
        payload: EmbedPayload | None = None,
        title: str | None = None,
        description: str | None = None,
        color: discord.Color | int | None = None,
        fields: Sequence[EmbedField] | None = None,
        footer: str | None = None,
        author_name: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        timestamp: bool = False,
        content: str | None = None,
        ephemeral: bool = False,
        **kwargs: Any,
    ) -> discord.Message | None:
        final_embed = self._resolve_embed(
            embed=embed,
            payload=payload,
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer=footer,
            author_name=author_name,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            timestamp=timestamp,
        )

        if interaction.response.is_done():
            return await interaction.followup.send(
                content=content,
                embed=final_embed,
                ephemeral=ephemeral,
                wait=True,
                **kwargs,
            )

        await interaction.response.send_message(
            content=content,
            embed=final_embed,
            ephemeral=ephemeral,
            **kwargs,
        )
        return None

    async def edit(
        self,
        message: discord.Message,
        *,
        embed: discord.Embed | None = None,
        payload: EmbedPayload | None = None,
        title: str | None = None,
        description: str | None = None,
        color: discord.Color | int | None = None,
        fields: Sequence[EmbedField] | None = None,
        footer: str | None = None,
        author_name: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        timestamp: bool = False,
        content: str | None = None,
        **kwargs: Any,
    ) -> discord.Message:
        final_embed = self._resolve_embed(
            embed=embed,
            payload=payload,
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer=footer,
            author_name=author_name,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            timestamp=timestamp,
        )
        return await message.edit(content=content, embed=final_embed, **kwargs)

    async def edit_interaction_response(
        self,
        interaction: discord.Interaction,
        *,
        embed: discord.Embed | None = None,
        payload: EmbedPayload | None = None,
        title: str | None = None,
        description: str | None = None,
        color: discord.Color | int | None = None,
        fields: Sequence[EmbedField] | None = None,
        footer: str | None = None,
        author_name: str | None = None,
        author_icon_url: str | None = None,
        thumbnail_url: str | None = None,
        image_url: str | None = None,
        timestamp: bool = False,
        content: str | None = None,
        **kwargs: Any,
    ) -> discord.InteractionMessage:
        final_embed = self._resolve_embed(
            embed=embed,
            payload=payload,
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer=footer,
            author_name=author_name,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            timestamp=timestamp,
        )
        return await interaction.edit_original_response(content=content, embed=final_embed, **kwargs)

    async def success(self, target: commands.Context | discord.abc.Messageable, title: str, description: str, **kwargs: Any) -> discord.Message:
        return await self.send(target, embed=self.success_embed(title, description), **kwargs)

    async def error(self, target: commands.Context | discord.abc.Messageable, title: str, description: str, **kwargs: Any) -> discord.Message:
        return await self.send(target, embed=self.error_embed(title, description), **kwargs)

    async def warning(self, target: commands.Context | discord.abc.Messageable, title: str, description: str, **kwargs: Any) -> discord.Message:
        return await self.send(target, embed=self.warning_embed(title, description), **kwargs)

    async def info(self, target: commands.Context | discord.abc.Messageable, title: str, description: str, **kwargs: Any) -> discord.Message:
        return await self.send(target, embed=self.info_embed(title, description), **kwargs)

    async def success_interaction(self, interaction: discord.Interaction, title: str, description: str, **kwargs: Any) -> discord.Message | None:
        return await self.respond(interaction, embed=self.success_embed(title, description), **kwargs)

    async def error_interaction(self, interaction: discord.Interaction, title: str, description: str, **kwargs: Any) -> discord.Message | None:
        return await self.respond(interaction, embed=self.error_embed(title, description), **kwargs)

    async def warning_interaction(self, interaction: discord.Interaction, title: str, description: str, **kwargs: Any) -> discord.Message | None:
        return await self.respond(interaction, embed=self.warning_embed(title, description), **kwargs)

    async def info_interaction(self, interaction: discord.Interaction, title: str, description: str, **kwargs: Any) -> discord.Message | None:
        return await self.respond(interaction, embed=self.info_embed(title, description), **kwargs)

    def _resolve_embed(
        self,
        *,
        embed: discord.Embed | None,
        payload: EmbedPayload | None,
        title: str | None,
        description: str | None,
        color: discord.Color | int | None,
        fields: Sequence[EmbedField] | None,
        footer: str | None,
        author_name: str | None,
        author_icon_url: str | None,
        thumbnail_url: str | None,
        image_url: str | None,
        timestamp: bool,
    ) -> discord.Embed:
        if embed is not None:
            return embed

        if payload is not None:
            return self.create(
                title=payload.title,
                description=payload.description,
                color=payload.color,
                fields=payload.fields,
                footer=payload.footer,
                author_name=payload.author_name,
                author_icon_url=payload.author_icon_url,
                thumbnail_url=payload.thumbnail_url,
                image_url=payload.image_url,
                timestamp=payload.timestamp,
            )

        return self.create(
            title=title,
            description=description,
            color=color,
            fields=fields,
            footer=footer,
            author_name=author_name,
            author_icon_url=author_icon_url,
            thumbnail_url=thumbnail_url,
            image_url=image_url,
            timestamp=timestamp,
        )

    def _coerce_color(self, color: discord.Color | int | None) -> discord.Color:
        if color is None:
            return self.default_color
        if isinstance(color, discord.Color):
            return color
        return discord.Color(color)
