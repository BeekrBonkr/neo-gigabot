from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from utils.command_log import record_command

log = logging.getLogger(__name__)

MENU_NAME = "Edit Image"
MAX_SELECT_OPTIONS = 25  # Discord's cap on one dropdown
MAX_FORM_FIELDS = 5  # and on one pop-up form
PICKER_TIMEOUT_SECONDS = 300

# The picker's dropdowns, in order. A message holds five rows, so five is the most there can be.
CATEGORIES = ("Filters and effects", "Resize, rotate and crop", "Animate", "Memes and text", "Faces and templates")
# Which dropdown each command group's commands go in.
GROUP_CATEGORY = {
    "filter": "Filters and effects",
    "image effects": "Filters and effects",
    "image transform": "Resize, rotate and crop",
    "edit": "Resize, rotate and crop",
    "animate": "Animate",
    "image text": "Memes and text",
    "memegen": "Memes and text",
    "image template": "Faces and templates",
}
# /image effects holds the face commands too; they get the last dropdown.
FACE_COMMANDS = frozenset({"facebox", "blurfaces", "facecrop", "faceswap", "joy", "clown", "forehead", "googlyeyes"})

SOURCE_PARAMETERS = frozenset({"attachment", "message_link"})
FORM_TYPES = frozenset({
    discord.AppCommandOptionType.string, discord.AppCommandOptionType.integer,
    discord.AppCommandOptionType.number, discord.AppCommandOptionType.boolean,
})
# Commands whose options are all optional but which do nothing when every one is left alone.
ALWAYS_ASK = frozenset({"image transform crop", "filter adjust", "edit resize"})

NOT_GIVEN = object()


def takes_message_image(command: app_commands.Command) -> bool:
    """Whether the command works on one image it can be handed from a message:
    it has the usual attachment and message_link options, wants no second
    picture, and needs nothing a form can't ask for."""
    parameters = command.parameters
    if not SOURCE_PARAMETERS <= {parameter.name for parameter in parameters}:
        return False
    for parameter in parameters:
        if parameter.name in SOURCE_PARAMETERS:
            continue
        if parameter.type is discord.AppCommandOptionType.attachment:
            return False
        if parameter.required and parameter.type not in FORM_TYPES:
            return False
    return True


def category_of(command: app_commands.Command) -> str | None:
    group = command.parent.qualified_name if command.parent else ""
    if group == "image effects" and command.name in FACE_COMMANDS:
        return "Faces and templates"
    return GROUP_CATEGORY.get(group)


def build_catalog(tree: app_commands.CommandTree) -> dict[str, list[app_commands.Command]]:
    """The commands the picker offers, by dropdown."""
    catalog: dict[str, list[app_commands.Command]] = {name: [] for name in CATEGORIES}
    for command in tree.walk_commands():
        if not isinstance(command, app_commands.Command) or not takes_message_image(command):
            continue
        category = category_of(command)
        if category is not None:
            catalog[category].append(command)
    for name, found in catalog.items():
        found.sort(key=lambda command: command.name)
        if len(found) > MAX_SELECT_OPTIONS:
            log.warning("The %r dropdown of the Edit Image menu has %s commands; only %s fit", name, len(found), MAX_SELECT_OPTIONS)
            del found[MAX_SELECT_OPTIONS:]
    return catalog


def form_parameters(command: app_commands.Command) -> list[app_commands.Parameter]:
    """The options to ask for before running the command, or none when it can
    run as it is. Required ones come first."""
    askable = [p for p in command.parameters if p.name not in SOURCE_PARAMETERS and p.type in FORM_TYPES]
    if not any(p.required for p in askable) and command.qualified_name not in ALWAYS_ASK:
        return []
    return sorted(askable, key=lambda p: not p.required)[:MAX_FORM_FIELDS]


def field_label(parameter: app_commands.Parameter) -> str:
    return parameter.display_name.replace("_", " ").capitalize()


def parse_field(parameter: app_commands.Parameter, raw: str | None) -> object:
    """Turn what was typed or chosen in the form into the option's value.
    NOT_GIVEN when it was left empty, so the command's own default applies.
    Raises ValueError with a message for the user."""
    raw = (raw or "").strip()
    if not raw:
        return NOT_GIVEN
    if parameter.choices:
        for choice in parameter.choices:
            if str(choice.value) == raw:
                return choice.value
        raise ValueError(f"**{field_label(parameter)}**: that is not one of the choices.")
    if parameter.type is discord.AppCommandOptionType.boolean:
        return raw == "yes"
    if parameter.type is discord.AppCommandOptionType.string:
        return raw

    whole = parameter.type is discord.AppCommandOptionType.integer
    try:
        value = int(raw) if whole else float(raw)
    except ValueError:
        raise ValueError(f"**{field_label(parameter)}** must be a {'whole ' if whole else ''}number.") from None
    low, high = parameter.min_value, parameter.max_value
    if value != value or (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f"**{field_label(parameter)}** must be {_range_text(low, high)}.")
    return value


def _range_text(low: int | float | None, high: int | float | None) -> str:
    if low is not None and high is not None:
        return f"from {low} to {high}"
    if low is not None:
        return f"{low} or more"
    if high is not None:
        return f"{high} or less"
    return "a number"


def _field_input(parameter: app_commands.Parameter) -> discord.ui.Item:
    default = None if parameter.required or parameter.default is None else parameter.default
    if parameter.choices:
        options = [
            discord.SelectOption(label=choice.name[:100], value=str(choice.value), default=choice.value == default)
            for choice in parameter.choices[:MAX_SELECT_OPTIONS]
        ]
        return discord.ui.Select(options=options, required=parameter.required)
    if parameter.type is discord.AppCommandOptionType.boolean:
        options = [
            discord.SelectOption(label="Yes", value="yes", default=default is True),
            discord.SelectOption(label="No", value="no", default=default is not True),
        ]
        return discord.ui.Select(options=options, required=False)
    if parameter.type is discord.AppCommandOptionType.string:
        longest = int(parameter.max_value) if parameter.max_value else 200
        return discord.ui.TextInput(
            style=discord.TextStyle.paragraph if longest > 100 else discord.TextStyle.short,
            default=None if default is None else str(default), required=parameter.required, max_length=longest,
        )
    bounded = parameter.min_value is not None or parameter.max_value is not None
    return discord.ui.TextInput(
        default=None if default is None else str(default), required=parameter.required, max_length=12,
        placeholder=_range_text(parameter.min_value, parameter.max_value).capitalize() if bounded else None,
    )


class EffectForm(discord.ui.Modal):
    """Asks for the options a picked command needs, then runs it."""

    def __init__(self, cog: ImageMenu, picker: EffectPicker, command: app_commands.Command, parameters: list[app_commands.Parameter]) -> None:
        super().__init__(title=command.name.capitalize()[:45], timeout=PICKER_TIMEOUT_SECONDS)
        self.cog, self.picker, self.command = cog, picker, command
        self.fields: list[tuple[app_commands.Parameter, discord.ui.Item]] = []
        for parameter in parameters:
            component = _field_input(parameter)
            described = parameter.description if parameter.description and parameter.description != "…" else None
            self.add_item(discord.ui.Label(text=field_label(parameter)[:45], description=described and described[:100], component=component))
            self.fields.append((parameter, component))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        options: dict[str, object] = {}
        problems: list[str] = []
        for parameter, component in self.fields:
            if isinstance(component, discord.ui.Select):
                raw = component.values[0] if component.values else None
            else:
                raw = component.value
            try:
                value = parse_field(parameter, raw)
            except ValueError as exc:
                problems.append(str(exc))
                continue
            if value is not NOT_GIVEN:
                options[parameter.name] = value
        if problems:
            await self.cog.bot.embeds.error_interaction(interaction, "Check Those Values", "\n".join(problems), ephemeral=True)
            return
        await self.picker.close()
        await self.cog.run_effect(interaction, self.command, self.picker.target, options)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Edit Image form failed", exc_info=error)


class EffectSelect(discord.ui.Select):
    def __init__(self, category: str, found: list[app_commands.Command]) -> None:
        options = [
            discord.SelectOption(label=command.name[:100], value=command.qualified_name, description=command.description[:100])
            for command in found
        ]
        super().__init__(placeholder=category, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.view.cog.effect_picked(interaction, self.view, self.values[0])


class EffectPicker(discord.ui.View):
    """The private list of effects shown for one message. `origin` is the menu
    click that opened it, kept so the list can be redrawn or taken away."""

    def __init__(self, cog: ImageMenu, origin: discord.Interaction, target: discord.Message) -> None:
        super().__init__(timeout=PICKER_TIMEOUT_SECONDS)
        self.cog, self.origin, self.target = cog, origin, target
        self.commands: dict[str, app_commands.Command] = {}
        for category, found in build_catalog(cog.bot.tree).items():
            if found:
                self.add_item(EffectSelect(category, found))
                self.commands.update((command.qualified_name, command) for command in found)

    async def close(self) -> None:
        self.stop()
        try:
            await self.origin.delete_original_response()
        except discord.HTTPException:
            pass  # dismissed already, or older than the 15 minutes Discord allows

    async def reset(self) -> None:
        """Clear the choice shown in the list, so the same effect can be
        picked again after its form was closed without sending."""
        self.stop()
        try:
            await self.origin.edit_original_response(view=EffectPicker(self.cog, self.origin, self.target))
        except discord.HTTPException:
            pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("Edit Image picker failed", exc_info=error)


class ImageMenu(commands.Cog):
    """Right-click a message, then Apps > Edit Image: run an image command on
    that message's image without copying a link to it."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.menu = app_commands.ContextMenu(name=MENU_NAME, callback=self.edit_image)

    async def cog_load(self) -> None:
        self.bot.tree.add_command(self.menu)

    async def cog_unload(self) -> None:
        self.bot.tree.remove_command(self.menu.name, type=self.menu.type)

    async def edit_image(self, interaction: discord.Interaction, message: discord.Message) -> None:
        if not self.bot.image_tools.message_has_image(message):
            await self.bot.embeds.error_interaction(
                interaction, "No Image Found", "That message does not contain an image attachment or embedded image.", ephemeral=True,
            )
            return
        embed = self.bot.embeds.info_embed(
            MENU_NAME, "Pick what to do with that image. Anything that needs text or numbers asks for them first.",
        )
        await interaction.response.send_message(embed=embed, view=EffectPicker(self, interaction, message), ephemeral=True)

    async def effect_picked(self, interaction: discord.Interaction, picker: EffectPicker, qualified_name: str) -> None:
        command = picker.commands.get(qualified_name)
        if command is None:
            await self.bot.embeds.error_interaction(interaction, "Not Available", "That effect is not available right now.", ephemeral=True)
            return
        if not await self.bot.command_gate(interaction, command.qualified_name):
            return
        parameters = form_parameters(command)
        if parameters:
            await interaction.response.send_modal(EffectForm(self, picker, command, parameters))
            await picker.reset()
            return
        await picker.close()
        await self.run_effect(interaction, command, picker.target, {})

    async def run_effect(
        self, interaction: discord.Interaction, command: app_commands.Command, target: discord.Message, options: dict[str, object],
    ) -> None:
        """Run a slash command's own handler on the target message's image, as
        if it had been typed: the same cooldown, the same reply, the same line
        in the command log."""
        try:
            for check in command.checks:
                if not await discord.utils.maybe_coroutine(check, interaction):
                    raise app_commands.CheckFailure(f"The check functions for command {command.qualified_name!r} failed.")
        except app_commands.CommandOnCooldown as exc:
            await record_command(self.bot.storage_path, interaction, command, status="COOLDOWN")
            await self.bot.embeds.error_interaction(interaction, "Slow Down", f"Try again in `{exc.retry_after:.1f}` seconds.", ephemeral=True)
            return
        except app_commands.CheckFailure as exc:
            await record_command(self.bot.storage_path, interaction, command, status="CHECK_FAILED", error=str(exc))
            await self.bot.embeds.error_interaction(
                interaction, "You Cannot Use That", "You do not meet the requirements to use this command.", ephemeral=True,
            )
            return

        # A handler written for app_commands.Choice wants the whole choice, not its value.
        for name, value in list(options.items()):
            parameter = command._params[name]
            if parameter.is_choice_annotation():
                options[name] = next(choice for choice in parameter.choices if choice.value == value)

        try:
            with self.bot.image_tools.pinned_source(target):
                await command.callback(command.binding, interaction, **options)
        except Exception as exc:
            log.exception("Edit Image: /%s failed", command.qualified_name, exc_info=exc)
            await record_command(self.bot.storage_path, interaction, command, status="ERROR", error=type(exc).__name__)
            await self.bot.image_tools.send_error(interaction, "Command Failed", "Something went wrong while running that command.")
            return
        await record_command(self.bot.storage_path, interaction, command, status="OK")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(ImageMenu(bot))
