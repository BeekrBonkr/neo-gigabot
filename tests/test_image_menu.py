"""The Edit Image menu: which commands it offers, the form it asks with, and
every offered command run through its real handler on a message's image."""

import asyncio
import datetime
import io
import types

import discord
import pytest
from discord import app_commands
from discord.ext import commands
from PIL import Image

import main
from cogs import image_animate, image_edit, image_filters, image_memes, image_menu, images
from cogs.image_menu import NOT_GIVEN, EffectForm, EffectPicker, build_catalog, form_parameters, parse_field
from utils.embeds import EmbedManager
from utils.imaging import ImageToolkit
from utils.settings import update_guild_settings

GUILD_ID = 4242
IMAGE_COGS = [images, image_filters, image_animate, image_memes, image_edit, image_menu]
# What a person would type into the form for each command that asks.
FORM_ANSWERS = {
    "image transform rotate": {"angle": "90"},
    "image transform crop": {"left": "10", "square": "no"},
    "image text toptext": {"text": "top"},
    "image text middletext": {"text": "middle"},
    "image text bottomtext": {"text": "bottom", "max_size": "40"},
    "image text caption": {"text": "a caption", "position": "bottom"},
    "image text motivate": {"title": "TESTING", "subtitle": "it works"},
    "image effects convert": {"format": "webp"},
    "filter adjust": {"brightness": "150"},
    "edit resize": {"percent": "50"},
    "memegen news": {"headline": "It works"},
    "memegen notification": {"text": "hello", "sender": "Mom"},
    "memegen watchmojo": {"title": "Top 10 tests"},
    "memegen watermark": {"style": "ifunny"},
    "memegen scene": {"scene": "museum"},
}
NEEDS_A_GIF = {"animate frames", "animate reverse", "animate speed"}


def png_bytes(size=(96, 72)) -> bytes:
    image = Image.new("RGBA", size, (200, 60, 40, 255))
    image.paste((40, 60, 200, 255), (0, 0, size[0] // 2, size[1]))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


class FakeAttachment:
    def __init__(self, data: bytes, filename: str = "clicked.png") -> None:
        self.data, self.filename, self.size, self.content_type = data, filename, len(data), "image/png"

    async def read(self) -> bytes:
        return self.data


def gif_bytes(count=4) -> bytes:
    frames = [Image.new("RGBA", (64, 48), (index * 60, 120, 200, 255)) for index in range(count)]
    out = io.BytesIO()
    frames[0].save(out, format="GIF", save_all=True, append_images=frames[1:], duration=80, loop=0)
    return out.getvalue()


def message_with_image(size=(96, 72), gif=False):
    attachment = FakeAttachment(gif_bytes(), "clicked.gif") if gif else FakeAttachment(png_bytes(size))
    return types.SimpleNamespace(attachments=[attachment], embeds=[])


class Response:
    def __init__(self) -> None:
        self.modal, self.sent, self.done = None, [], False

    def is_done(self) -> bool:
        return self.done

    async def send_modal(self, modal) -> None:
        self.modal, self.done = modal, True

    async def send_message(self, *args, **kwargs) -> None:
        self.sent.append(kwargs)
        self.done = True

    async def defer(self, **kwargs) -> None:
        self.done = True


class FakeInteraction:
    """Enough of an interaction for the handlers: who, where, and a reply."""

    def __init__(self, guild_id: int | None = GUILD_ID) -> None:
        self.guild = types.SimpleNamespace(id=guild_id, name="Test", filesize_limit=10 * 1024 * 1024) if guild_id else None
        self.guild_id = guild_id
        self.user = types.SimpleNamespace(id=1, display_name="Tester", mention="<@1>")
        self.channel, self.channel_id, self.command = None, 7, None
        self.type = discord.InteractionType.component
        self.created_at = discord.utils.utcnow()
        self.response = Response()
        self.followups: list[dict] = []
        self.followup = types.SimpleNamespace(send=self._followup)
        self.deleted = self.edited = 0

    async def _followup(self, *args, **kwargs) -> None:
        self.followups.append(kwargs)

    async def delete_original_response(self) -> None:
        self.deleted += 1

    async def edit_original_response(self, **kwargs) -> None:
        self.edited += 1


class Harness:
    """The image cogs loaded into a real command tree, with the toolkit's
    sending replaced so results can be looked at."""

    def __init__(self, storage) -> None:
        self.sent: list[dict] = []
        self.errors: list[tuple[str, str]] = []
        self.storage = storage

    async def __aenter__(self):
        bot = self.bot = commands.Bot(command_prefix="!", intents=discord.Intents.none(), help_command=None)
        bot.project_root = main.Path(main.__file__).resolve().parent
        bot.storage_path = self.storage
        bot.embeds = EmbedManager(bot)
        bot.command_gate = types.MethodType(main.GigaBot.command_gate, bot)
        tools = bot.image_tools = ImageToolkit(bot)

        async def allowed(interaction, name):
            return True

        async def nothing(*args, **kwargs):
            return None

        async def no_other_source(interaction):
            raise AssertionError("the clicked message should have been used")

        async def send_file(interaction, *, data, filename, title, description=None):
            self.sent.append({"data": data, "filename": filename, "title": title})

        async def send_error(interaction, title, description):
            self.errors.append((title, description))

        tools.ensure_command_allowed, tools.defer, tools.note_heavy_command = allowed, nothing, nothing
        tools.selected_message_image = tools.history_image = no_other_source
        tools.send_processed_file, tools.send_error = send_file, send_error
        for module in IMAGE_COGS:  # set up directly, so the classes are the ones imported above
            await module.setup(bot)
        self.cog = bot.get_cog("ImageMenu")
        return self

    async def __aexit__(self, *exc) -> None:
        for name in list(self.bot.cogs):
            await self.bot.remove_cog(name)

    def offered(self) -> dict[str, app_commands.Command]:
        return {c.qualified_name: c for found in build_catalog(self.bot.tree).values() for c in found}


def run(storage, body):
    async def go():
        async with Harness(storage) as harness:
            return await body(harness)

    return asyncio.run(go())


def test_catalog_fits_discord_and_leaves_nothing_out(storage):
    async def body(harness):
        catalog = build_catalog(harness.bot.tree)
        assert list(catalog) == list(image_menu.CATEGORIES) and len(catalog) <= 5
        offered = harness.offered()
        for found in catalog.values():
            assert 0 < len(found) <= image_menu.MAX_SELECT_OPTIONS
        # every command that works on one image has a dropdown to live in
        eligible = {
            c.qualified_name for c in harness.bot.tree.walk_commands()
            if isinstance(c, app_commands.Command) and image_menu.takes_message_image(c)
        }
        assert eligible == set(offered)
        assert {"filter invert", "image effects deepfry", "image effects googlyeyes", "animate spin", "memegen jail"} <= eligible
        # a second picture, no picture at all, or a member: not something a clicked message can supply
        assert not {"image effects overlay", "edit collage", "animate togif", "memegen drake", "image misc pfp", "image text quote"} & eligible
        assert [c.name for c in catalog["Faces and templates"] if c.name in image_menu.FACE_COMMANDS] != []
        assert image_menu.FACE_COMMANDS.isdisjoint(c.name for c in catalog["Filters and effects"])
        picker = EffectPicker(harness.cog, FakeInteraction(), message_with_image())
        assert len(picker.children) == 5 and set(picker.commands) == eligible
        return len(eligible)

    assert run(storage, body) >= 80


def test_only_commands_that_need_an_answer_get_a_form(storage):
    async def body(harness):
        offered = harness.offered()
        asking = {name for name, command in offered.items() if form_parameters(command)}
        assert asking == set(FORM_ANSWERS)
        assert [p.name for p in form_parameters(offered["image text toptext"])] == ["text", "max_size"]  # required first
        assert len(form_parameters(offered["image transform crop"])) == 5
        for name in asking:  # every form can be built, within Discord's limits
            form = EffectForm(harness.cog, None, offered[name], form_parameters(offered[name]))
            assert 1 <= len(form.children) <= image_menu.MAX_FORM_FIELDS and len(form.title) <= 45
            for label in form.children:
                assert len(label.text) <= 45 and len(label.description or "") <= 100

    run(storage, body)


def test_parse_field(storage):
    async def body(harness):
        offered = harness.offered()
        fields = {p.name: p for name in ("image transform rotate", "image transform crop", "memegen scene", "image text toptext") for p in form_parameters(offered[name])}
        assert parse_field(fields["angle"], " 12.5 ") == 12.5
        assert parse_field(fields["left"], "10") == 10 and parse_field(fields["left"], "") is NOT_GIVEN
        assert parse_field(fields["square"], "yes") is True and parse_field(fields["square"], "no") is False
        assert parse_field(fields["scene"], "museum") == "museum"
        assert parse_field(fields["text"], "hello") == "hello" and parse_field(fields["max_size"], None) is NOT_GIVEN
        for name, raw, said in (("left", "46", "from 0 to 45"), ("left", "1.5", "whole number"), ("angle", "sideways", "must be a number"),
                                ("angle", "nan", "must be"), ("scene", "moon", "not one of the choices")):
            with pytest.raises(ValueError, match=said):
                parse_field(fields[name], raw)

    run(storage, body)


def test_every_offered_command_runs_on_the_clicked_image(storage):
    async def body(harness):
        results = {}
        for name, command in harness.offered().items():
            harness.sent.clear(), harness.errors.clear()
            interaction, origin = FakeInteraction(), FakeInteraction()
            picker = EffectPicker(harness.cog, origin, message_with_image((160, 120), gif=name in NEEDS_A_GIF))
            await harness.cog.effect_picked(interaction, picker, name)
            if name in FORM_ANSWERS:
                form = interaction.response.modal
                assert isinstance(form, EffectForm), name
                assert origin.edited == 1 and origin.deleted == 0  # the list is redrawn while the form is open
                for parameter, component in form.fields:
                    answer = FORM_ANSWERS[name].get(parameter.name)
                    if isinstance(component, discord.ui.Select):
                        component._values = [answer] if answer else []
                    else:
                        component._value = answer
                await form.on_submit(FakeInteraction())
            else:
                assert interaction.response.modal is None, name
            assert origin.deleted == 1, name  # and taken away once the effect runs
            results[name] = (list(harness.sent), list(harness.errors))
        return results

    results = run(storage, body)
    log = "\n".join(path.read_text() for path in storage.rglob("*.log"))
    for name, (sent, errors) in results.items():
        if name.rsplit(" ", 1)[-1] in image_menu.FACE_COMMANDS:
            # the test picture has no face in it: the command says so, in its own words
            assert not sent and len(errors) == 1 and "face" in errors[0][1].lower(), (name, errors)
            continue
        assert errors == [] and len(sent) == 1, (name, errors)
        assert Image.open(io.BytesIO(sent[0]["data"])).width > 0
        assert f"command=/{name} | status=OK" in log, name


def test_blocked_commands_stay_blocked_and_cooldowns_are_shared(storage):
    update_guild_settings(storage, GUILD_ID, {"blocked_commands": ["filter", "animate spin"]})

    async def body(harness):
        picker = EffectPicker(harness.cog, FakeInteraction(), message_with_image())
        for name in ("filter invert", "animate spin"):  # a blocked group, and a blocked command
            interaction = FakeInteraction()
            await harness.cog.effect_picked(interaction, picker, name)
            assert harness.sent == [] and interaction.response.sent[0]["embed"].title.endswith("Command Disabled"), name

        first, second = FakeInteraction(), FakeInteraction()
        # the cooldown lives on the command, which other tests here have just run: move past it
        first.created_at = second.created_at = discord.utils.utcnow() + datetime.timedelta(hours=1)
        await harness.cog.effect_picked(first, picker, "memegen jail")
        await harness.cog.effect_picked(second, picker, "memegen jail")  # straight away: the slash command's cooldown applies
        assert len(harness.sent) == 1 and "Slow Down" in second.response.sent[0]["embed"].title
        return harness.sent

    run(storage, body)
    log = "\n".join(path.read_text() for path in storage.rglob("*.log"))
    assert "command=/memegen jail | status=COOLDOWN" in log


def test_menu_needs_an_image_and_opens_privately(storage):
    async def body(harness):
        empty = FakeInteraction()
        await harness.cog.edit_image(empty, types.SimpleNamespace(attachments=[], embeds=[]))
        assert "No Image Found" in empty.response.sent[0]["embed"].title and empty.response.sent[0]["ephemeral"] is True

        opened = FakeInteraction()
        await harness.cog.edit_image(opened, message_with_image())
        reply = opened.response.sent[0]
        assert reply["ephemeral"] is True and isinstance(reply["view"], EffectPicker)
        assert harness.bot.tree.get_command("Edit Image", type=discord.AppCommandType.message) is not None

    run(storage, body)


def test_an_attachment_or_link_still_beats_the_clicked_message(storage):
    async def body(harness):
        tools = harness.bot.image_tools
        clicked, given = message_with_image((40, 30)), FakeAttachment(png_bytes((20, 10)), "given.png")
        with tools.pinned_source(clicked):
            assert (await tools.get_source_image(FakeInteraction(), None, None))[1] == "clicked.png"
            assert (await tools.get_source_image(FakeInteraction(), given, None))[1] == "given.png"
        with pytest.raises(AssertionError):  # nothing stays pinned afterwards
            await tools.get_source_image(FakeInteraction(), None, None)

    run(storage, body)
