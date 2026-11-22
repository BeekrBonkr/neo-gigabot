"""Forms in Discord: /form, the posted button, and answering over pages of pop-ups."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace as NS

import discord

import cogs.forms as forms_cog
from cogs.forms import FormPopup, Forms, NextPage, OpenFormButton, form_message
from modfakes import Member, Role, make_bot, make_interaction, said
from utils import forms

EVERYONE = Role(1, "@everyone", 0, default=True)
ADMIN_ROLE = Role(2, "Admin", 5, discord.Permissions(administrator=True))


def setup(storage, items, status=forms.OPEN, **options):
    bot = make_bot(storage)
    bot.config = NS(website_url="https://site.test")
    robin, boss, me = Member(100, "robin", [EVERYONE]), Member(200, "boss", [EVERYONE, ADMIN_ROLE]), Member(999, "bot", [EVERYONE], bot=True)
    people = {100: robin, 200: boss, 300: Member(300, "pal", [EVERYONE])}
    guild = NS(id=4242, name="Server", owner_id=1, me=me, members=[robin, boss, me], get_member=people.get, get_role=lambda rid: None, get_channel=lambda cid: None)
    for member in people.values():
        member.guild = guild
    form = forms.create_form(storage, guild.id, 9, title="**Quick** one", items=items, options={"answered_in": forms.DISCORD, **options})
    if status == forms.OPEN:
        form = forms.update_form(storage, guild.id, form["id"], status=forms.OPEN)
    return Forms(bot), guild, robin, boss, form


def interaction_for(guild, user):
    interaction = make_interaction(guild, user)
    interaction.modals = []

    async def send_modal(modal):
        interaction.modals.append(modal)

    interaction.response.send_modal = send_modal
    return interaction


ITEMS = [{"kind": "section", "text": "About **you**", "help": "Be *brief*."},
         {"kind": "short", "text": "Name", "required": True},
         {"kind": "checkboxes", "text": "Likes", "options": ["Tea", "Coffee"]},
         {"kind": "scale", "text": "Happy", "min": 1, "max": 5, "low": "No", "high": "Yes"},
         {"kind": "number", "text": "Age", "min": 13, "max": 99},
         {"kind": "date", "text": "When"},
         {"kind": "section", "text": "Last bit"},
         {"kind": "member", "text": "Who"}, {"kind": "role", "text": "Team"}, {"kind": "paragraph", "text": "More"}]


def fill(popup, **values):
    for item, component in popup.fields:
        if item["text"] in values:
            value = values[item["text"]]
            if isinstance(component, discord.ui.TextInput):
                component._value = value
            elif isinstance(component, (discord.ui.UserSelect, discord.ui.RoleSelect, discord.ui.ChannelSelect)):
                component._values = [NS(id=int(value))] if value else []
            else:
                component._values = value


def test_a_discord_form_is_split_into_pages_of_five(storage):
    cog, guild, robin, boss, form = setup(storage, ITEMS)
    pages = forms.discord_pages(form["items"])
    assert [[item["text"] for item in page] for page in pages] == [
        ["About **you**", "Name", "Likes", "Happy", "Age"], ["When"], ["Last bit", "Who", "Team", "More"]]
    first = cog.popup(form, 0, 100, test=False)
    assert first.title == "Quick one 1/3" and isinstance(first.children[0], discord.ui.TextDisplay)
    assert first.children[0].content == "## About **you**\nBe *brief*."           # headings keep their formatting
    assert [type(component).__name__ for _item, component in first.fields] == ["TextInput", "Select", "Select"] + ["TextInput"]
    assert [option.label for option in first.fields[2][1].options] == ["1", "2", "3", "4", "5"] and first.fields[2][1].options[0].description == "No"
    last = cog.popup(form, 2, 100, test=False)
    assert [type(component).__name__ for _item, component in last.fields] == ["UserSelect", "RoleSelect", "TextInput"]
    _content, embed, view = form_message(cog.bot, form)
    assert embed.title == "📝 Quick one" and view.children[0].custom_id == f"gform:open:{form['id']}"
    assert forms_cog.BUTTON_ID.fullmatch(view.children[0].custom_id) and isinstance(OpenFormButton(5).item, discord.ui.Button)
    with __import__("pytest").raises(forms.FormError, match="up to 25 questions"):
        forms.update_form(storage, guild.id, form["id"], items=[{"kind": "short", "text": f"q{n}"} for n in range(26)])


def test_answering_page_by_page(storage):
    cog, guild, robin, boss, form = setup(storage, ITEMS)
    guild.get_role = lambda rid: NS(id=7, name="Blue", is_default=lambda: False) if rid == 7 else None

    async def scenario():
        opened = interaction_for(guild, robin)
        await cog.open(opened, form["id"])
        (page,) = opened.modals
        fill(page, Name="Robin", Likes=["0", "1"], Happy=["2"], Age="9")
        mistake = interaction_for(guild, robin)
        await page.on_submit(mistake)
        assert "13 to 99" in said(mistake) and isinstance(mistake.response.sent[-1][1]["view"], NextPage)
        retry = interaction_for(guild, robin)
        await mistake.response.sent[-1][1]["view"].go(retry)
        (page,) = retry.modals
        assert page.fields[0][1].default == "Robin" and page.fields[3][1].default == "9"       # nothing typed is lost
        assert [option.default for option in page.fields[1][1].options] == [True, True]          # nor what was picked
        fill(page, Likes=["0", "1"], Happy=["2"], Age="30")                                     # Discord sends the picks shown again
        saved = interaction_for(guild, robin)
        await page.on_submit(saved)
        assert "Page 1 of 3 is saved" in said(saved) and forms.count_responses(storage, form["id"]) == 0
        intruder = interaction_for(guild, boss)
        await saved.response.sent[-1][1]["view"].go(intruder)
        assert intruder.modals == [] and "someone else's" in intruder.response.sent[-1][0]
        onward = interaction_for(guild, robin)
        await saved.response.sent[-1][1]["view"].go(onward)
        (second,) = onward.modals
        fill(second, When="2026-12-24")
        after = interaction_for(guild, robin)
        await second.on_submit(after)
        last_view = after.response.sent[-1][1]["view"]
        assert [child.label for child in last_view.children if isinstance(child, discord.ui.Button)] == ["Continue to page 3 of 3"]
        (menu,) = [child for child in last_view.children if isinstance(child, discord.ui.Select)]     # two pages to go back to
        assert [option.label for option in menu.options] == ["Page 1", "Page 2"] and menu.options[0].description == "Name, Likes, Happy, Age"
        returned = interaction_for(guild, robin)
        await last_view.back(returned, 0)
        (first_again,) = returned.modals
        assert first_again.title == "Quick one 1/3" and first_again.fields[0][1].default == "Robin"   # going back shows what was answered
        fill(first_again, Name="Robin B", Likes=["0"], Happy=["2"], Age="30")
        changed = interaction_for(guild, robin)
        await first_again.on_submit(changed)
        last_view = changed.response.sent[-1][1]["view"]
        assert last_view.children[0].label == "Continue to page 3 of 3"                               # on to the first page not yet filled in
        nosy = interaction_for(guild, boss)
        await last_view.back(nosy, 0)
        assert nosy.modals == [] and "someone else's" in nosy.response.sent[-1][0]
        final_open = interaction_for(guild, robin)
        await last_view.go(final_open)
        (third,) = final_open.modals
        fill(third, Who="300", Team="7", More="thanks")
        review = interaction_for(guild, robin)
        await third.on_submit(review)
        assert "All 3 pages are filled in" in said(review) and forms.count_responses(storage, form["id"]) == 0
        review_view = review.response.sent[-1][1]["view"]
        assert review_view.children[0].label == "Send my answers" and len(review_view.children[1].options) == 3
        second_thoughts = interaction_for(guild, robin)
        await review_view.back(second_thoughts, 2)
        (third,) = second_thoughts.modals
        assert third.fields[2][1].default == "thanks"
        fill(third, Who="300", Team="7", More="thank you")
        review = interaction_for(guild, robin)
        await third.on_submit(review)
        sent = interaction_for(guild, robin)
        await review.response.sent[-1][1]["view"].send(sent)
        assert "were sent" in said(sent)
        (stored,) = forms.responses_of(storage, form["id"], 100)
        assert [entry["answer"] for entry in stored["answers"]] == ["Robin B", "Tea", "3", "30", "2026-12-24", "pal", "@Blue", "thank you"]
        late = interaction_for(guild, robin)
        await review.response.sent[-1][1]["view"].send(late)                                          # a second press finds nothing kept
        assert "no longer kept" in said(late) and forms.count_responses(storage, form["id"]) == 1
        again = interaction_for(guild, robin)
        await cog.open(again, form["id"])
        assert again.modals == [] and "already answered" in said(again)

    asyncio.run(scenario())


def test_website_forms_hand_out_their_link_and_staff_can_try_drafts(storage):
    cog, guild, robin, boss, form = setup(storage, ITEMS[1:3], answered_in=forms.WEBSITE)

    async def scenario():
        opened = interaction_for(guild, robin)
        await cog.open(opened, form["id"])
        (content, kwargs), = opened.response.sent
        assert kwargs["view"].children[0].url == f"https://site.test/forms/{guild.id}/quick-one" and kwargs["ephemeral"] is True
        listing = interaction_for(guild, robin)
        await Forms.form.callback(cog, listing, None)
        assert "Quick one** (website)" in said(listing)

    asyncio.run(scenario())

    other = storage / "drafts"
    other.mkdir()
    cog, guild, robin, boss, draft = setup(other, ITEMS[1:3], status=forms.CLOSED)
    storage = other

    async def drafts():
        member_view = interaction_for(guild, robin)
        await cog.open(member_view, draft["id"])
        assert member_view.modals == [] and "isn't open" in said(member_view)
        assert [choice.name for choice in await cog._complete(interaction_for(guild, robin), "")] == []
        assert [choice.name for choice in await cog._complete(interaction_for(guild, boss), "")] == ["Draft (test): Quick one"]
        trying = interaction_for(guild, boss)
        await cog.open(trying, draft["id"])
        (popup,) = trying.modals
        assert popup.test is True
        fill(popup, Name="Boss")
        done = interaction_for(guild, boss)
        await popup.on_submit(done)
        assert "Test Sent" in said(done)
        assert forms.count_responses(storage, draft["id"]) == 0 and forms.list_responses(storage, draft["id"])[1] == 1   # a test, kept apart

    asyncio.run(drafts())
