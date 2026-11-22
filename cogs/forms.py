"""Forms in Discord: the /form command, the "Open form" button the bot can
post for a form, and answering a short form in a pop-up without leaving
Discord. Forms are built, and longer ones answered, on the website; the rules
of a form are the same either way (utils/forms_service.py).
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands, tasks

from utils import forms, site_access
from utils.forms import FormError
from utils.forms_service import FormsService
from utils.modules import module_enabled
from utils.settings import get_guild_settings
from utils.user_app import home_guild

LOGGER = logging.getLogger(__name__)

FORM_COLOR = discord.Color.blurple()
BUTTON_ID = re.compile(r"gform:open:(?P<form_id>\d+)")
PROGRESS_SECONDS = 30 * 60  # how long half-answered pages are kept between pop-ups


def label(text: str, limit: int) -> str:
    """Text the form's maker wrote, as a pop-up shows it: no Markdown, cut to fit."""
    flat = " ".join(forms.plain(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


class OpenFormButton(discord.ui.DynamicItem[discord.ui.Button], template=BUTTON_ID.pattern):
    """The button under a posted form. It carries the form's ID, so it keeps
    working after a restart and for as long as the form exists."""

    def __init__(self, form_id: int, text: str = "Open form") -> None:
        super().__init__(discord.ui.Button(label=text[:80] or "Open form", emoji="📝", style=discord.ButtonStyle.primary, custom_id=f"gform:open:{form_id}"))
        self.form_id = form_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]) -> "OpenFormButton":
        return cls(int(match["form_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Forms")
        if cog is not None:
            await cog.open(interaction, self.form_id)


def form_message(bot: Any, form: dict[str, Any], message: dict[str, Any] | None = None) -> tuple[str | None, discord.Embed | None, discord.ui.View]:
    """What the bot posts in a channel for a form: the message chosen for it
    (plain text or an embed), or its title and introduction, and the button."""
    message = message or form["options"].get("button_message") or {}
    view = discord.ui.View(timeout=None)
    view.add_item(OpenFormButton(form["id"], message.get("label") or ("Open a ticket" if form["options"].get("ticket") else "Open form")))
    if message.get("text") or message.get("title"):
        content, embed = FormsService(bot).compose(None, form, None, message)
        return content, embed, view
    asked = len(forms.questions(form["items"]))
    embed = bot.embeds.create(title=f"📝 {label(form['title'], 250)}", description=form["description"] or "Press the button below to fill it in.",
                              color=FORM_COLOR, footer=f"{asked} question{'s' if asked != 1 else ''}")
    return None, embed, view


class CloseTicketButton(discord.ui.DynamicItem[discord.ui.Button], template=r"gform:close:(?P<response_id>\d+)"):
    """The button in a ticket channel that closes it. Keeps working after a restart."""

    def __init__(self, response_id: int) -> None:
        super().__init__(discord.ui.Button(label="Close ticket", emoji="🔒", style=discord.ButtonStyle.danger, custom_id=f"gform:close:{response_id}"))
        self.response_id = response_id

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item: discord.ui.Button, match: re.Match[str]) -> "CloseTicketButton":
        return cls(int(match["response_id"]))

    async def callback(self, interaction: discord.Interaction) -> None:
        cog = interaction.client.get_cog("Forms")
        if cog is not None:
            await cog.ask_to_close(interaction, self.response_id)


def ticket_view(response_id: int) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(CloseTicketButton(response_id))
    return view


class ConfirmClose(discord.ui.View):
    def __init__(self, cog: "Forms", response_id: int, user_id: int) -> None:
        super().__init__(timeout=120)
        self.cog, self.response_id, self.user_id = cog, response_id, user_id

    @discord.ui.button(label="Close and delete this channel", style=discord.ButtonStyle.danger, emoji="🔒")
    async def confirm(self, interaction: discord.Interaction, _button: discord.ui.Button) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("Only whoever asked to close it can confirm.", ephemeral=True)
            return
        await self.cog.close(interaction, self.response_id)


class FormPopup(discord.ui.Modal):
    """One page of a form answered in Discord: up to five questions and section
    headings. Headings and their text are shown with their formatting."""

    def __init__(self, cog: "Forms", form: dict[str, Any], page: int, *, test: bool = False) -> None:
        pages = forms.discord_pages(form["items"])
        title = forms.plain(form["title"])
        title = f"{title[:38].rstrip()} {page + 1}/{len(pages)}" if len(pages) > 1 else title
        super().__init__(title=title[:45], timeout=PROGRESS_SECONDS)
        self.cog, self.form, self.page, self.last, self.test = cog, form, page, page == len(pages) - 1, test
        self.fields: list[tuple[dict[str, Any], discord.ui.Item]] = []
        self.items = pages[page]

    def build(self, saved: dict[str, Any]) -> "FormPopup":
        """Add the page's components, showing what was answered before (after a mistake, or on going back)."""
        for item in self.items:
            if item["kind"] == forms.SECTION:
                self.add_item(discord.ui.TextDisplay(f"## {item['text']}" + (f"\n{item['help']}" if item["help"] else "")))
                continue
            component = self._component(item, saved.get(item["id"]))
            described = label(item["help"], 100) if item["help"] else (label(item["text"], 100) if len(forms.plain(item["text"])) > 45 else None)
            self.add_item(discord.ui.Label(text=label(item["text"], 45), description=described, component=component))
            self.fields.append((item, component))
        return self

    @staticmethod
    def _component(item: dict[str, Any], before: Any) -> discord.ui.Item:
        kind, required = item["kind"], bool(item["required"])
        if kind in ("choice", "dropdown", "checkboxes", "scale"):
            if kind == "scale":
                choices = [str(step) for step in range(item["min"], item["max"] + 1)]
                notes = {choices[0]: item.get("low", ""), choices[-1]: item.get("high", "")}
            else:
                choices, notes = list(item["options"]), {}
            picked = {str(entry) for entry in (before if isinstance(before, list) else [before]) if entry not in (None, "")}
            return discord.ui.Select(
                options=[discord.SelectOption(label=choice[:100], value=str(index), description=(notes.get(choice) or None) and notes[choice][:100],
                                              default=choice in picked) for index, choice in enumerate(choices)],
                min_values=1 if required else 0, max_values=len(choices) if kind == "checkboxes" else 1, required=required,
            )
        if kind in forms.PICKER_KINDS:
            picker = {"member": discord.ui.UserSelect, "role": discord.ui.RoleSelect, "channel": discord.ui.ChannelSelect}[kind]
            extra = {"channel_types": [discord.ChannelType.text, discord.ChannelType.news]} if kind == "channel" else {}
            default = [discord.Object(id=int(before))] if str(before or "").isdigit() else []
            return picker(min_values=1 if required else 0, max_values=1, required=required, default_values=default, **extra)
        hint = {"number": f"A whole number from {item.get('min')} to {item.get('max')}", "date": "A date, like 2026-12-24"}.get(kind)
        return discord.ui.TextInput(
            style=discord.TextStyle.paragraph if kind == "paragraph" else discord.TextStyle.short, required=required,
            max_length={"paragraph": forms.MAX_ANSWER, "short": forms.MAX_SHORT_ANSWER}.get(kind, 20), placeholder=hint,
            default=str(before)[:forms.MAX_ANSWER] if before not in (None, "") else None,
        )

    def entered(self) -> dict[str, Any]:
        answers: dict[str, Any] = {}
        for item, component in self.fields:
            kind = item["kind"]
            if kind in forms.PICKER_KINDS:
                answers[item["id"]] = str(component.values[0].id) if component.values else ""
            elif isinstance(component, discord.ui.Select):
                choices = [str(step) for step in range(item["min"], item["max"] + 1)] if kind == "scale" else item["options"]
                picked = [choices[int(value)] for value in component.values if value.isdigit() and int(value) < len(choices)]
                answers[item["id"]] = picked if kind == "checkboxes" else (picked[0] if picked else "")
            else:
                answers[item["id"]] = component.value
        return answers

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.page_done(interaction, self.form, self.page, self.entered(), test=self.test)


class NextPage(discord.ui.View):
    """Between two pop-ups: Discord opens a pop-up only in answer to a press."""

    def __init__(self, cog: "Forms", form: dict[str, Any], page: int, user_id: int, *, test: bool, again: bool = False) -> None:
        super().__init__(timeout=PROGRESS_SECONDS)
        self.cog, self.form, self.page, self.user_id, self.test = cog, form, page, user_id, test
        total = len(forms.discord_pages(form["items"]))
        button = discord.ui.Button(label="Try again" if again else f"Continue to page {page + 1} of {total}", style=discord.ButtonStyle.primary,
                                   emoji="✏️" if again else "➡️")
        button.callback = self.go
        self.add_item(button)

    async def go(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("This form is someone else's to fill in.", ephemeral=True)
            return
        await interaction.response.send_modal(self.cog.popup(self.form, self.page, interaction.user.id, test=self.test))


class Forms(commands.Cog):
    """Forms a server builds on the website, opened from Discord."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.service = FormsService(bot)
        self._progress: dict[tuple[int, int], dict[str, Any]] = {}  # (user, form) -> pages answered so far

    async def cog_load(self) -> None:
        self.bot.add_dynamic_items(OpenFormButton, CloseTicketButton)
        self._tidy.start()

    async def cog_unload(self) -> None:
        self._tidy.cancel()
        self.bot.remove_dynamic_items(OpenFormButton, CloseTicketButton)

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

    def open_forms(self, guild_id: int, *, drafts: bool = False) -> list[dict[str, Any]]:
        listed = [form for form in forms.list_forms(self.bot.storage_path, guild_id) if form["kind"] == forms.GENERAL]
        return [form for form in listed if forms.is_open(form, form["responses"])[0] or (drafts and form["status"] != forms.OPEN)]

    def is_staff(self, guild: discord.Guild, user: Any) -> bool:
        """Whether this person may read the server's forms: they can try a draft out in Discord."""
        permissions = getattr(user, "guild_permissions", None)
        if permissions is None:
            return False
        settings = get_guild_settings(self.bot.storage_path, guild.id)
        return site_access.access_for(permissions, settings, is_owner=user.id == guild.owner_id)["forms"]["view"]

    def saved(self, form_id: int, user_id: int) -> dict[str, Any]:
        now = time.monotonic()
        for key in [key for key, entry in self._progress.items() if now - entry["at"] > PROGRESS_SECONDS]:
            del self._progress[key]
        return self._progress.get((user_id, form_id), {}).get("answers", {})

    def popup(self, form: dict[str, Any], page: int, user_id: int, *, test: bool) -> FormPopup:
        return FormPopup(self, form, page, test=test).build(self.saved(form["id"], user_id))

    async def open(self, interaction: discord.Interaction, form_id: int) -> None:
        """Someone asked for a form, by its button or by /form. A Discord form
        opens its first pop-up; a website form hands out its link. Staff can
        try out a form that is still a draft: what they send is a test."""
        guild = home_guild(interaction)
        form = self.service.get(guild.id, int(form_id)) if guild is not None else None
        if form is None or form["kind"] != forms.GENERAL:
            await self.bot.embeds.error_interaction(interaction, "Form Not Found", "That form no longer exists.", ephemeral=True)
            return
        if not module_enabled(self.bot.storage_path, guild.id, "forms"):
            await self.bot.embeds.error_interaction(interaction, "Turned Off", "Forms are turned off in this server.", ephemeral=True)
            return
        test = form["status"] != forms.OPEN and self.is_staff(guild, interaction.user)
        if form["options"]["answered_in"] != forms.DISCORD:
            await self._send_link(interaction, form)
            return
        if not test:
            try:
                standing = await self.service.standing(guild, form, interaction.user.id)
            except FormError as exc:
                await self.bot.embeds.error_interaction(interaction, "Form", str(exc), ephemeral=True)
                return
            if not standing["can_respond"]:
                await self.bot.embeds.info_interaction(interaction, label(form["title"], 200), standing["reason"] or "You can't answer this form right now.", ephemeral=True)
                return
        if not forms.discord_pages(form["items"]):
            await self.bot.embeds.error_interaction(interaction, "Form", "This form has no questions yet.", ephemeral=True)
            return
        self._progress[(interaction.user.id, form["id"])] = {"answers": {}, "at": time.monotonic()}
        await interaction.response.send_modal(self.popup(form, 0, interaction.user.id, test=test))

    async def _send_link(self, interaction: discord.Interaction, form: dict[str, Any]) -> None:
        link = self.service.link(form)
        if not link:
            await self.bot.embeds.error_interaction(interaction, "Form", "This form is answered on the bot's website, which isn't set up here.", ephemeral=True)
            return
        view = discord.ui.View()
        view.add_item(discord.ui.Button(label="Open the form", url=link, style=discord.ButtonStyle.link, emoji="🌐"))
        asked = len(forms.questions(form["items"]))
        await interaction.response.send_message(
            embed=self.bot.embeds.create(title=f"📝 {label(form['title'], 250)}", color=FORM_COLOR,
                                         description=(form["description"] + "\n\n" if form["description"] else "")
                                         + f"This form has {asked} question{'s' if asked != 1 else ''} and is filled in on the website."),
            view=view, ephemeral=True,
        )

    async def page_done(self, interaction: discord.Interaction, form: dict[str, Any], page: int, answers: dict[str, Any], *, test: bool) -> None:
        """One page was sent: check it, keep it, and offer the next page, or send the whole response."""
        guild = home_guild(interaction)
        fresh = self.service.get(guild.id, form["id"]) if guild is not None else None
        if fresh is None:
            await self.bot.embeds.error_interaction(interaction, "Form Not Found", "That form no longer exists.", ephemeral=True)
            return
        pages = forms.discord_pages(fresh["items"])
        key = (interaction.user.id, fresh["id"])
        progress = self._progress.setdefault(key, {"answers": {}, "at": time.monotonic()})
        progress["answers"].update(answers)
        progress["at"] = time.monotonic()
        try:
            forms.clean_answers(pages[page] if page < len(pages) else [], answers, self.service.names(guild), partial=True)
        except FormError as exc:
            await interaction.response.send_message(embed=self.bot.embeds.error_embed("Check That Page", str(exc)),
                                                    view=NextPage(self, fresh, page, interaction.user.id, test=test, again=True), ephemeral=True)
            return
        if page + 1 < len(pages):
            await interaction.response.send_message(
                embed=self.bot.embeds.info_embed(label(fresh["title"], 200), f"Page {page + 1} of {len(pages)} is saved. Nothing is sent until the last page."),
                view=NextPage(self, fresh, page + 1, interaction.user.id, test=test), ephemeral=True)
            return
        await self.take(interaction, fresh, dict(progress["answers"]), test=test)

    async def take(self, interaction: discord.Interaction, form: dict[str, Any], answers: dict[str, Any], *, test: bool = False) -> None:
        """A whole response from the pop-ups, through the same checks as any other."""
        guild = home_guild(interaction)
        try:
            await self.service.respond(guild, form, interaction.user.id, str(interaction.user), answers, test=test, via=forms.DISCORD)
        except FormError as exc:
            await self.bot.embeds.error_interaction(interaction, "Not Sent", str(exc), ephemeral=True)
            return
        self._progress.pop((interaction.user.id, form["id"]), None)
        if test:
            await self.bot.embeds.success_interaction(interaction, "Test Sent", f"Your test response to **{label(form['title'], 200)}** is listed with the others, marked as a test. "
                                                      "None of the form's actions ran.", ephemeral=True)
            return
        waiting = " It is waiting to be read, and you will be told what is decided." if form["options"]["review"] and form["options"]["notify_outcome"] else ""
        await self.bot.embeds.success_interaction(interaction, "Sent", f"Your answers to **{label(form['title'], 200)}** were sent.{waiting}", ephemeral=True)

    # ---------- tickets ----------

    def _ticket(self, interaction: discord.Interaction, response_id: int) -> tuple[Any, dict[str, Any] | None, dict[str, Any] | None]:
        guild = home_guild(interaction)
        response = forms.get_response(self.bot.storage_path, guild.id, response_id) if guild is not None else None
        form = self.service.get(guild.id, response["form_id"]) if response is not None else None
        return guild, form, response

    async def ask_to_close(self, interaction: discord.Interaction, response_id: int) -> None:
        guild, form, response = self._ticket(interaction, response_id)
        if form is None or response is None:
            await self.bot.embeds.error_interaction(interaction, "Ticket", "This ticket's form no longer exists. Delete the channel by hand.", ephemeral=True)
            return
        if not self.service.may_close(guild, form, response, interaction.user):
            await self.bot.embeds.error_interaction(interaction, "Ticket", "Only the person who opened this ticket and the staff can close it.", ephemeral=True)
            return
        action = self.service.ticket_action(form) or {}
        saved = " A transcript is saved for the staff first." if action.get("log_channel_id") else ""
        await interaction.response.send_message(embed=self.bot.embeds.warning_embed("Close This Ticket?", f"This channel will be deleted.{saved}"),
                                                view=ConfirmClose(self, response_id, interaction.user.id), ephemeral=True)

    async def close(self, interaction: discord.Interaction, response_id: int) -> None:
        guild, form, response = self._ticket(interaction, response_id)
        if form is None or response is None or not self.service.may_close(guild, form, response, interaction.user):
            await self.bot.embeds.error_interaction(interaction, "Ticket", "This ticket can't be closed from here.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await self.service.close_ticket(guild, interaction.channel, form, response, interaction.user)
        except discord.HTTPException as exc:
            await interaction.followup.send(embed=self.bot.embeds.error_embed("Not Closed", f"Discord refused: {exc}"), ephemeral=True)

    # ---------- /form ----------

    async def _complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        guild = home_guild(interaction)
        if guild is None:
            return []
        typed, staff = current.lower(), self.is_staff(guild, interaction.user)
        return [app_commands.Choice(name=(("Draft (test): " if form["status"] != forms.OPEN else "") + label(form["title"], 80))[:100], value=str(form["id"]))
                for form in self.open_forms(guild.id, drafts=staff) if typed in form["title"].lower()][:25]

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
        available = self.open_forms(guild.id, drafts=self.is_staff(guild, interaction.user))
        if form is None:
            available = [entry for entry in available if entry["status"] == forms.OPEN]
            if not available:
                await self.bot.embeds.info_interaction(interaction, "No Open Forms", "This server has no forms open right now.", ephemeral=True)
                return
            lines = [f"**{label(entry['title'], 100)}**" + (" (website)" if entry["options"]["answered_in"] == forms.WEBSITE else "")
                     + (f"\n{entry['description'][:150]}" if entry["description"] else "") for entry in available[:15]]
            await self.bot.embeds.info_interaction(interaction, "Open Forms", "\n\n".join(lines) + "\n\nRun `/form` again and pick one to fill it in.", ephemeral=True)
            return
        chosen = next((entry for entry in available if str(entry["id"]) == form or entry["title"].lower() == form.lower()), None)
        if chosen is None:
            await self.bot.embeds.error_interaction(interaction, "Form Not Found", "No open form here has that name. Pick one from the list as you type.", ephemeral=True)
            return
        await self.open(interaction, chosen["id"])


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Forms(bot))
