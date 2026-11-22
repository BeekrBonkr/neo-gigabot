"""Forms in Discord: the /form command, the "Open form" button the bot can
post for a form, and answering a short form in a pop-up without leaving
Discord. Forms are built, and longer ones answered, on the website; the rules
of a form are the same either way (utils/forms_service.py).
"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import forms
from utils.forms import FormError
from utils.forms_service import FormsService
from utils.modules import module_enabled
from utils.user_app import home_guild

LOGGER = logging.getLogger(__name__)

FORM_COLOR = discord.Color.blurple()
POPUP_QUESTIONS = 5  # as many fields as a Discord pop-up holds
# What a pop-up can ask: typed answers, and choosing from a list.
POPUP_KINDS = (*forms.TEXT_KINDS, *forms.OPTION_KINDS, "number", "date", "scale")
BUTTON_ID = re.compile(r"gform:open:(?P<form_id>\d+)")


def fits_popup(form: dict[str, Any]) -> bool:
    """Whether the form is short and plain enough to be answered inside Discord."""
    asked = forms.questions(form["items"])
    return bool(form["options"]["in_discord"]) and 1 <= len(asked) <= POPUP_QUESTIONS and all(item["kind"] in POPUP_KINDS for item in asked)


class OpenFormButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_ID.pattern):
    """The button under a posted form. It carries the form's ID, so it keeps
    working after a restart and for as long as the form exists."""

    def __init__(self, form_id: int) -> None:
        super().__init__(discord.ui.Button(label="Open form", emoji="📝", style=discord.ButtonStyle.primary, custom_id=f"gform:open:{form_id}"))
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]) -> "OpenFormButton":
        return cls(int(match["form_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Forms")
        if cog is not None:
            await cog.open(interaction, self.form_id)


def form_message(bot: Any, form: dict[str, Any]) -> tuple[discord.Embed, discord.ui.View]:
    """What the bot posts in a channel for a form: its title, what it is for, and the button."""
    asked = len(forms.questions(form["items"]))
    embed = bot.embeds.create(title=f"📝 {form['title']}"[:256], description=form["description"] or "Press the button below to fill it in.",
                              color=FORM_COLOR, footer=f"{asked} question{'s' if asked != 1 else ''}")
    view = discord.ui.View(timeout=None)
    view.add_item(OpenFormButton(form["id"]))
    return embed, view


class FormPopup(discord.ui.Modal):
    """A short form, asked in a Discord pop-up."""

    def __init__(self, cog: "Forms", form: dict[str, Any]) -> None:
        super().__init__(title=form["title"][:45], timeout=30 * 60)
        self.cog, self.form = cog, form
        self.fields: list[tuple[dict[str, Any], discord.ui.Item]] = []
        for item in forms.questions(form["items"]):
            kind, required = item["kind"], bool(item["required"])
            if kind in forms.OPTION_KINDS:
                many = len(item["options"]) if kind == "checkboxes" else 1
                component: discord.ui.Item = discord.ui.Select(
                    options=[discord.SelectOption(label=option[:100], value=str(index)) for index, option in enumerate(item["options"])],
                    min_values=1 if required else 0, max_values=many, required=required,
                )
            else:
                hint = {"number": f"A whole number from {item.get('min')} to {item.get('max')}", "date": "A date, like 2026-12-24",
                        "scale": f"{item.get('min')} ({item.get('low') or 'lowest'}) to {item.get('max')} ({item.get('high') or 'highest'})"}.get(kind)
                component = discord.ui.TextInput(
                    style=discord.TextStyle.paragraph if kind == "paragraph" else discord.TextStyle.short, required=required,
                    max_length={"paragraph": forms.MAX_ANSWER, "short": forms.MAX_SHORT_ANSWER}.get(kind, 20), placeholder=hint and hint[:100],
                )
            self.add_item(discord.ui.Label(text=item["text"][:45], description=(item["help"] or (item["text"] if len(item["text"]) > 45 else ""))[:100] or None,
                                           component=component))
            self.fields.append((item, component))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        answers: dict[str, Any] = {}
        for item, component in self.fields:
            if isinstance(component, discord.ui.Select):
                picked = [item["options"][int(value)] for value in component.values if value.isdigit() and int(value) < len(item["options"])]
                answers[item["id"]] = picked if item["kind"] == "checkboxes" else (picked[0] if picked else "")
            else:
                answers[item["id"]] = component.value
        await self.cog.take(interaction, self.form, answers)


class Forms(commands.Cog):
    """Forms a server builds on the website, opened from Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = FormsService(bot)

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(OpenFormButton)
        self._tidy.start()

    async def cog_unload(self) -> None:
        self._tidy.cancel()
        self.bot.remove_dynamic_items(OpenFormButton)

    @tasks.loop(hours=1)
    async def _tidy(self) -> None:
        """Responses a form no longer keeps go, and so does everything of a
        server the bot was removed from long enough ago."""
        try:
            removed = forms.prune(self.bot.storage_path)
            gone = forms.delete_departed(self.bot.storage_path, {guild.id for guild in self.bot.guilds})
            if removed or gone:
                LOGGER.info("Forms: removed %s old responses and the forms of %s departed servers", removed, len(gone))
        except Exception:  # noqa: BLE001 - try again next hour
            LOGGER.exception("Tidying forms failed")

    @_tidy.before_loop
    async def _before_tidy(self) -> None:
        await self.bot.wait_until_ready()

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        forms.bot_left(self.bot.storage_path, guild.id)

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild) -> None:
        forms.bot_returned(self.bot.storage_path, guild.id)

    # ---------- opening a form ----------

    def open_forms(self, guild_id: int) -> list[dict[str, Any]]:
        return [form for form in forms.list_forms(self.bot.storage_path, guild_id)
                if form["kind"] == forms.GENERAL and forms.is_open(form, form["responses"])[0]]

    async def open(self, interaction: discord.Interaction, form_id: int) -> None:
        """Someone asked for a form, by its button or by /form: let them
        answer it here if it fits a pop-up, else hand them its link."""
        guild = home_guild(interaction)
        form = self.service.get(guild.id, int(form_id)) if guild is not None else None
        if form is None or form["kind"] != forms.GENERAL:
            await self.bot.embeds.error_interaction(interaction, "Form Not Found", "That form no longer exists.", ephemeral=True)
            return
        if not module_enabled(self.bot.storage_path, guild.id, "forms"):
            await self.bot.embeds.error_interaction(interaction, "Turned Off", "Forms are turned off in this server.", ephemeral=True)
            return
        try:
            standing = await self.service.standing(guild, form, interaction.user.id)
        except FormError as exc:
            await self.bot.embeds.error_interaction(interaction, "Form", str(exc), ephemeral=True)
            return
        if not standing["can_respond"]:
            await self.bot.embeds.info_interaction(interaction, form["title"][:200], standing["reason"] or "You can't answer this form right now.", ephemeral=True)
            return
        if fits_popup(form):
            await interaction.response.send_modal(FormPopup(self, form))
            return
        link = self.service.link(form)
        if not link:
            await self.bot.embeds.error_interaction(interaction, "Form", "This form is answered on the bot's website, which isn't set up here.", ephemeral=True)
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open the form", url=link, style=discord.ButtonStyle.link, emoji="🌐"))
        asked = len(forms.questions(form["items"]))
        await interaction.response.send_message(
            embed=self.bot.embeds.create(title=f"📝 {form['title']}"[:256], color=FORM_COLOR,
                                         description=(form["description"] + "\n\n" if form["description"] else "")
                                         + f"This form has {asked} question{'s' if asked != 1 else ''} and is filled in on the website."),
            view=view, ephemeral=True,
        )

    async def take(self, interaction: discord.Interaction, form: dict[str, Any], answers: dict[str, Any]) -> None:
        """Answers from a pop-up, through the same checks as the website's."""
        guild = home_guild(interaction)
        fresh = self.service.get(guild.id, form["id"]) if guild is not None else None
        if fresh is None:
            await self.bot.embeds.error_interaction(interaction, "Form Not Found", "That form no longer exists.", ephemeral=True)
            return
        try:
            await self.service.respond(guild, fresh, interaction.user.id, str(interaction.user), answers)
        except FormError as exc:
            await self.bot.embeds.error_interaction(interaction, "Not Sent", str(exc), ephemeral=True)
            return
        waiting = " It is waiting to be read, and you will be told what is decided." if fresh["options"]["review"] and fresh["options"]["notify_outcome"] else ""
        await self.bot.embeds.success_interaction(interaction, "Sent", f"Your answers to **{fresh['title']}** were sent.{waiting}", ephemeral=True)

    # ---------- /form ----------

    async def _complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = home_guild(interaction)
        if guild is None:
            return []
        typed = current.lower()
        return [app_commands.Choice(name=form["title"][:100], value=str(form["id"]))
                for form in self.open_forms(guild.id) if typed in form["title"].lower()][:25]

    @app_commands.command(name="form", description="Fill in one of this server's forms.")
    @app_commands.describe(form="The form to open. Leave it out to see the ones that are open.")
    @app_commands.autocomplete(form=_complete)
    @app_commands.guild_only()
    @app_commands.checks.cooldown(1, 5.0)
    async def form(self, interaction: discord.Interaction, form: str | None = None) -> None:
        guild = home_guild(interaction)
        if guild is None:
            await self.bot.embeds.error_interaction(interaction, "Server Only", "Forms belong to a server that has added me.", ephemeral=True)
            return
        available = self.open_forms(guild.id)
        if form is None:
            if not available:
                await self.bot.embeds.info_interaction(interaction, "No Open Forms", "This server has no forms open right now.", ephemeral=True)
                return
            lines = [f"**{entry['title']}**" + (f"\n{entry['description'][:150]}" if entry["description"] else "") for entry in available[:15]]
            await self.bot.embeds.info_interaction(interaction, "Open Forms", "\n\n".join(lines) + "\n\nRun `/form` again and pick one to fill it in.", ephemeral=True)
            return
        chosen = next((entry for entry in available if str(entry["id"]) == form or entry["title"].lower() == form.lower()), None)
        if chosen is None:
            await self.bot.embeds.error_interaction(interaction, "Form Not Found", "No open form here has that name. Pick one from the list as you type.", ephemeral=True)
            return
        await self.open(interaction, chosen["id"])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Forms(bot))
