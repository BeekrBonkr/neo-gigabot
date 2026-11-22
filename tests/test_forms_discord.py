"""Forms in Discord: /form, the posted button and answering in a pop-up."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import discord

import cogs.forms as forms_cog
from cogs.forms import FormPopup, Forms, OpenFormButton, fits_popup, form_message
from modfakes import Member, Role, make_bot, make_interaction, said
from utils import forms

EVERYONE = Role(1, "@everyone", 0, default=True)


def setup(storage, items, **options):
    bot = make_bot(storage)
    bot.config = NS(website_url="https://site.test")
    robin, me = Member(100, "robin", [EVERYONE]), Member(999, "bot", [EVERYONE], bot=True)
    guild = NS(id=4242, name="Server", owner_id=1, me=me, members=[robin, me], get_member=lambda uid: robin if uid == 100 else None,
               get_role=lambda rid: None, get_channel=lambda cid: None)
    robin.guild = guild
    form = forms.create_form(storage, guild.id, 9, title="Quick one", items=items, options=options)
    form = forms.update_form(storage, guild.id, form["id"], status=forms.OPEN)
    return Forms(bot), guild, robin, form


def interaction_for(guild, user):
    interaction = make_interaction(guild, user)
    interaction.modals = []

    async def send_modal(modal):
        interaction.modals.append(modal)

    interaction.response.send_modal = send_modal
    return interaction


SHORT = [{"kind": "short", "text": "Name", "required": True}, {"kind": "checkboxes", "text": "Likes", "options": ["Tea", "Coffee"]},
         {"kind": "scale", "text": "Happy", "min": 1, "max": 5}]


def test_a_short_plain_form_fits_a_popup_and_a_long_or_fancy_one_does_not(storage):
    cog, guild, robin, form = setup(storage, SHORT, in_discord=True)
    assert fits_popup(form)
    assert not fits_popup({**form, "options": {**form["options"], "in_discord": False}})
    assert not fits_popup({**form, "items": forms.clean_items(SHORT + [{"kind": "member", "text": "Who"}])})
    assert not fits_popup({**form, "items": forms.clean_items([{"kind": "short", "text": f"q{n}"} for n in range(6)])})
    embed, view = form_message(cog.bot, form)
    assert embed.title == "📝 Quick one" and embed.footer.text == "3 questions"
    assert view.children[0].custom_id == f"gform:open:{form['id']}" and forms_cog.BUTTON_ID.fullmatch(view.children[0].custom_id)["form_id"] == str(form["id"])
    assert isinstance(OpenFormButton(5).item, discord.ui.Button)


def test_answering_in_a_popup_goes_through_the_same_rules(storage):
    cog, guild, robin, form = setup(storage, SHORT, in_discord=True)

    async def scenario():
        opened = interaction_for(guild, robin)
        await cog.open(opened, form["id"])
        (popup,) = opened.modals
        assert isinstance(popup, FormPopup) and len(popup.children) == 3
        name, likes, happy = (component for _item, component in popup.fields)
        name._value, happy._value = "Robin", "9"
        likes._values = ["0", "1"]
        refused = interaction_for(guild, robin)
        await popup.on_submit(refused)
        assert "1 to 5" in said(refused) and forms.count_responses(storage, form["id"]) == 0
        happy._value = "4"
        sent = interaction_for(guild, robin)
        await popup.on_submit(sent)
        assert "were sent" in said(sent)
        (stored,) = forms.responses_of(storage, form["id"], 100)
        assert [entry["answer"] for entry in stored["answers"]] == ["Robin", "Tea, Coffee", "4"]
        again = interaction_for(guild, robin)
        await cog.open(again, form["id"])
        assert again.modals == [] and "already answered" in said(again)

    asyncio.run(scenario())


def test_a_longer_form_hands_out_its_link_and_the_command_lists_open_forms(storage):
    cog, guild, robin, form = setup(storage, SHORT + [{"kind": "member", "text": "Who"}])

    async def scenario():
        opened = interaction_for(guild, robin)
        await cog.open(opened, form["id"])
        (content, kwargs), = opened.response.sent
        assert kwargs["view"].children[0].url == f"https://site.test/forms/{guild.id}/quick-one" and kwargs["ephemeral"] is True
        listing = interaction_for(guild, robin)
        listing.is_guild_integration = lambda: True
        await Forms.form.callback(cog, listing, None)
        assert "Quick one" in said(listing)
        choices = await cog._complete(listing, "qui")
        assert [(choice.name, choice.value) for choice in choices] == [("Quick one", str(form["id"]))]
        forms.update_form(storage, guild.id, form["id"], status=forms.CLOSED)
        closed = interaction_for(guild, robin)
        await Forms.form.callback(cog, closed, None)
        assert "no forms open" in said(closed)

    asyncio.run(scenario())
