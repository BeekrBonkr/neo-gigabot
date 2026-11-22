"""Forms: what goes into one, who may answer when, and what the answers add up to."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from utils import forms
from utils.db import connect

GUILD, MAKER, ROBIN, SAM = 1, 9, 100, 101


def make(storage, template="blank", items=None, status=forms.OPEN, **options):
    form = forms.create_form(storage, GUILD, MAKER, title="Try me", template=template, items=items, options=options)
    return forms.update_form(storage, GUILD, form["id"], status=status) if status == forms.OPEN and forms.questions(form["items"]) else form


QUESTIONS = [
    {"kind": "section", "text": "About you", "help": "Short and sweet."},
    {"kind": "short", "text": "Name", "required": True},
    {"kind": "checkboxes", "text": "Likes", "options": ["Tea", "Coffee", "Water"]},
    {"kind": "dropdown", "text": "Role", "options": ["Mod", "Helper"]},
    {"kind": "number", "text": "Age", "min": 13, "max": 120},
    {"kind": "date", "text": "When"},
    {"kind": "scale", "text": "Happy", "min": 1, "max": 5, "low": "No", "high": "Yes"},
    {"kind": "member", "text": "Who"},
    {"kind": "paragraph", "text": "More"},
]


def test_questions_are_checked_and_keep_their_ids_through_edits():
    items = forms.clean_items(QUESTIONS)
    assert [item["id"] for item in items] == [f"q{n}" for n in range(1, 10)] and "required" not in items[0]
    assert items[6] == {"id": "q7", "kind": "scale", "text": "Happy", "help": "", "required": False, "min": 1, "max": 5, "low": "No", "high": "Yes"}
    edited = forms.clean_items([items[2], {"kind": "short", "text": "New one"}, items[1]])   # reordered, one added
    assert [item["id"] for item in edited] == ["q3", "q4", "q2"]                              # old ones keep theirs
    for bad in ([{"kind": "poem", "text": "x"}], [{"kind": "short", "text": ""}], [{"kind": "choice", "text": "x", "options": ["only"]}],
                [{"kind": "number", "text": "x", "min": 5, "max": 5}], [{"kind": "short", "text": "x" * 201}], "nope",
                [{"kind": "short", "text": f"q{n}"} for n in range(31)]):
        with pytest.raises(forms.FormError):
            forms.clean_items(bad)


def test_answers_are_checked_against_the_questions():
    items = forms.clean_items(QUESTIONS)
    names = forms.Names(member=lambda uid: "robin" if uid == 100 else None)
    good = forms.clean_answers(items, {"q2": "  Robin ", "q3": ["Tea", "Wine", "Water"], "q4": "Mod", "q5": "30", "q6": "2026-12-24",
                                       "q7": "4", "q8": "100", "q9": "x" * 5000}, names)
    assert [entry["answer"] for entry in good[:7]] == ["Robin", "Tea, Water", "Mod", "30", "2026-12-24", "4", "robin"]
    assert good[1]["value"] == ["Tea", "Water"] and good[3]["value"] == 30 and good[6]["value"] == "100" and len(good[7]["answer"]) == 2000
    assert good[0]["question"] == "Name" and good[0]["kind"] == "short"                       # the question as it read then
    for bad, message in (({}, "Please answer: Name"), ({"q2": "x", "q5": "12"}, "13 to 120"), ({"q2": "x", "q5": "old"}, "whole number"),
                         ({"q2": "x", "q6": "soon"}, "a date"), ({"q2": "x", "q8": "555"}, "member of this server"), ({"q2": "x", "q7": "9"}, "1 to 5")):
        with pytest.raises(forms.FormError, match=message):
            forms.clean_answers(items, bad, names)
    assert forms.clean_answers(items, {"q2": "x", "q4": "Owner"})[2]["answer"] == ""       # not one of the options: not an answer
    with pytest.raises(forms.FormError, match="at least one"):
        forms.clean_answers(forms.clean_items([{"kind": "short", "text": "Optional"}]), {})


def test_a_forms_own_rules_are_checked():
    options = forms.clean_options({"audience": "everyone", "retry_days": "9999", "review": "on", "channel_id": "12x", "max_responses": 10**9,
                                   "closes_at": "2027-01-01T10:00", "actions": [{"on": "approve", "do": "add_role", "role_id": "55"}]})
    assert (options["audience"], options["retry_days"], options["review"], options["channel_id"]) == ("members", 365, True, "")
    assert options["max_responses"] == forms.MAX_RESPONSES_PER_FORM and options["closes_at"] == "2027-01-01T10:00:00+00:00"
    (action,) = options["actions"]
    assert (action["on"], action["do"], action["role_id"], action["if_question"], action["by"]) == ("approve", "add_role", "55", "", "")
    for bad in ({"actions": [{"on": "approve", "do": "add_role", "role_id": "55"}]},          # not reviewed: nothing to approve
                {"review": True, "actions": [{"on": "submit", "do": "explode"}]}, {"actions": [{"on": "submit", "do": "post", "channel_id": "5"}]},
                {"opens_at": "2027-02-01", "closes_at": "2027-01-01"}, {"actions": [{"on": "submit", "do": "dm", "text": "hi"}] * 11}):
        with pytest.raises(forms.FormError):
            forms.clean_options(bad)
    assert forms.clean_options({"anonymous": True}, current=options)["review"] is True         # what is not given stays


def test_forms_are_made_from_templates_and_listed_per_server(storage):
    staff = forms.create_form(storage, GUILD, MAKER, template="staff")
    assert staff["slug"] == "staff-application" and staff["status"] == forms.CLOSED and staff["options"]["review"] is True
    assert len(forms.questions(staff["items"])) == 8 and staff["items"][0]["kind"] == "section"
    assert forms.create_form(storage, GUILD, MAKER, template="staff")["slug"] == "staff-application-2"
    for name, template in forms.TEMPLATES.items():
        forms.clean_items(template["items"])                                                   # every template is a valid form
        forms.clean_options(template.get("options", {}))
    assert [form["title"] for form in forms.list_forms(storage, GUILD)] == ["Staff application", "Staff application"]
    assert forms.list_forms(storage, 2) == [] and forms.get_form(storage, 2, staff["id"]) is None
    assert forms.get_form(storage, GUILD, "staff-application")["id"] == staff["id"]
    with pytest.raises(forms.FormError, match="at least one question"):
        forms.update_form(storage, GUILD, forms.create_form(storage, GUILD, MAKER)["id"], status=forms.OPEN)
    for _ in range(forms.MAX_FORMS_PER_GUILD - 3):
        forms.create_form(storage, GUILD, MAKER, title="More")
    with pytest.raises(forms.FormError, match="up to 20 forms"):
        forms.create_form(storage, GUILD, MAKER, title="One too many")
    appeal = forms.create_form(storage, GUILD, 0, title="Ban appeal", kind=forms.APPEAL)        # built-in ones do not count
    assert forms.list_forms(storage, GUILD)[0]["id"] == appeal["id"]
    with pytest.raises(forms.FormError, match="built in"):
        forms.delete_form(storage, GUILD, appeal["id"])
    assert forms.delete_form(storage, GUILD, staff["id"])["id"] == staff["id"] and forms.get_form(storage, GUILD, staff["id"]) is None


def answer(storage, form, user, text="hi", **kwargs):
    stored = forms.clean_answers(form["items"], {forms.questions(form["items"])[0]["id"]: text})
    return forms.submit(storage, form, user, f"user{user}", stored, **kwargs)


def test_who_may_answer_and_when(storage):
    once = make(storage, items=[{"kind": "short", "text": "Hi"}])
    assert forms.can_respond(once, [], 0) == (True, "")
    answer(storage, once, ROBIN)
    assert forms.can_respond(once, forms.responses_of(storage, once["id"], ROBIN), 1) == (False, "You have already answered this form.")
    assert forms.can_respond(once, forms.responses_of(storage, once["id"], SAM), 1)[0] is True
    many = make(storage, items=[{"kind": "short", "text": "Hi"}], multiple=True)
    answer(storage, many, ROBIN)
    assert forms.can_respond(many, forms.responses_of(storage, many["id"], ROBIN), 1)[0] is True

    closed = make(storage, items=[{"kind": "short", "text": "Hi"}], status=forms.CLOSED)
    assert forms.can_respond(closed, [], 0) == (False, "This form isn't open right now.")
    now = datetime(2027, 1, 10, tzinfo=timezone.utc)
    later = make(storage, items=[{"kind": "short", "text": "Hi"}], opens_at="2027-01-20T00:00", closes_at="2027-01-25T00:00")
    assert "opens on January 20, 2027" in forms.can_respond(later, [], 0, now)[1]
    assert forms.can_respond(later, [], 0, now + timedelta(days=12))[0] is True
    assert forms.can_respond(later, [], 0, now + timedelta(days=20)) == (False, "This form has closed.")
    small = make(storage, items=[{"kind": "short", "text": "Hi"}], max_responses=2)
    assert forms.can_respond(small, [], 2)[1] == "This form has all the responses it was open for."


def test_review_retry_edit_and_withdraw(storage):
    form = make(storage, items=[{"kind": "short", "text": "Why"}], review=True, retry_days=7, editable=True)
    first = answer(storage, form, ROBIN)
    mine = forms.responses_of(storage, form["id"], ROBIN)
    assert mine[0]["status"] == forms.PENDING and forms.can_respond(form, mine, 1)[1] == "Your response is waiting to be read."
    assert forms.edit_response(storage, form, first, ROBIN, forms.clean_answers(form["items"], {"q1": "better"})) is True
    assert forms.edit_response(storage, form, first, SAM, forms.clean_answers(form["items"], {"q1": "mine now"})) is False   # not theirs
    assert forms.get_response(storage, GUILD, first)["answers"][0]["answer"] == "better"
    assert forms.decide(storage, 2, first, True, MAKER) is None                                  # another server cannot decide it
    denied = forms.decide(storage, GUILD, first, False, MAKER, "not yet")
    assert denied["status"] == forms.DENIED and denied["decision_note"] == "not yet" and forms.decide(storage, GUILD, first, True, MAKER) is None
    mine = forms.responses_of(storage, form["id"], ROBIN)
    assert "You can send another after" in forms.can_respond(form, mine, 1)[1]
    assert forms.can_respond(form, mine, 1, datetime.now(timezone.utc) + timedelta(days=8))[0] is True
    assert forms.edit_response(storage, form, first, ROBIN, []) is False and forms.withdraw(storage, form["id"], first, ROBIN) is None  # decided: fixed
    second = answer(storage, form, SAM)
    assert forms.withdraw(storage, form["id"], second, ROBIN) is None and forms.withdraw(storage, form["id"], second, SAM)["id"] == second
    assert forms.get_response(storage, GUILD, second) is None
    fixed = make(storage, items=[{"kind": "short", "text": "Why"}])
    with pytest.raises(forms.FormError, match="can't be changed"):
        forms.edit_response(storage, fixed, answer(storage, fixed, ROBIN), ROBIN, [])


def test_a_full_form_closes_or_makes_room(storage):
    closing = make(storage, items=[{"kind": "short", "text": "Hi"}], max_responses=2, multiple=True)
    answer(storage, closing, ROBIN), answer(storage, closing, SAM)
    with pytest.raises(forms.FormError, match="full"):
        answer(storage, closing, 102)
    rolling = make(storage, items=[{"kind": "short", "text": "Hi"}], max_responses=2, when_full="rotate", multiple=True)
    ids = [answer(storage, rolling, user, text=str(user)) for user in (ROBIN, SAM, 102)]
    kept, total = forms.list_responses(storage, rolling["id"])
    assert total == 2 and [response["id"] for response in kept] == [ids[2], ids[1]]             # the oldest made room
    answer(storage, rolling, MAKER, test=True)                                                   # a test never counts or pushes anything out
    assert forms.count_responses(storage, rolling["id"]) == 2 and forms.list_responses(storage, rolling["id"])[1] == 3


def test_the_summary_adds_the_answers_up(storage):
    form = make(storage, items=QUESTIONS, multiple=True)
    for user, likes, role, happy, age in ((1, ["Tea"], "Mod", "5", "20"), (2, ["Tea", "Water"], "Helper", "3", "30"), (3, [], "", "", "")):
        forms.submit(storage, form, user, "u", forms.clean_answers(form["items"], {"q2": "n", "q3": likes, "q4": role, "q5": age, "q7": happy}))
    forms.submit(storage, form, MAKER, "t", forms.clean_answers(form["items"], {"q2": "test", "q3": ["Coffee"]}), test=True)
    by_id = {line["id"]: line for line in forms.summary(storage, form)}
    assert by_id["q2"]["answered"] == 3 and by_id["q2"]["of"] == 3
    assert by_id["q3"]["counts"] == [{"option": "Tea", "n": 2}, {"option": "Coffee", "n": 0}, {"option": "Water", "n": 1}] and by_id["q3"]["answered"] == 2
    assert by_id["q4"]["counts"] == [{"option": "Mod", "n": 1}, {"option": "Helper", "n": 1}]
    assert (by_id["q5"]["average"], by_id["q5"]["low"], by_id["q5"]["high"]) == (25.0, 20, 30)
    assert by_id["q7"]["average"] == 4.0 and by_id["q7"]["counts"][4] == {"option": "5", "n": 1} and len(by_id["q7"]["counts"]) == 5


def test_old_responses_and_departed_servers_are_cleaned_up(storage):
    form = make(storage, items=[{"kind": "short", "text": "Hi"}], keep_days=30, multiple=True, review=True)
    old, waiting, fresh = answer(storage, form, 1), answer(storage, form, 2), answer(storage, form, 3)
    forms.decide(storage, GUILD, old, True, MAKER), forms.decide(storage, GUILD, fresh, True, MAKER)
    with connect(storage) as connection:
        connection.execute("UPDATE form_responses SET created_at = datetime('now', '-90 days') WHERE id IN (?, ?)", (old, waiting))
    assert forms.prune(storage) == 1                                                            # the old one that is waiting stays
    assert {response["id"] for response in forms.list_responses(storage, form["id"])[0]} == {waiting, fresh}

    forms.bot_left(storage, GUILD)
    assert forms.delete_departed(storage, present=set()) == [] and forms.list_forms(storage, GUILD)   # inside the grace period
    with connect(storage) as connection:
        connection.execute("UPDATE form_departures SET left_at = datetime('now', '-40 days')")
    assert forms.delete_departed(storage, present={GUILD}) == [] and forms.list_forms(storage, GUILD)  # it came back in time
    forms.bot_left(storage, GUILD)
    with connect(storage) as connection:
        connection.execute("UPDATE form_departures SET left_at = datetime('now', '-40 days')")
    assert forms.delete_departed(storage, present=set()) == [GUILD]
    assert forms.list_forms(storage, GUILD) == [] and forms.list_responses(storage, form["id"])[1] == 0


def test_one_template_shows_every_kind_of_question(storage):
    form = forms.create_form(storage, GUILD, MAKER, template="everything")
    assert {item["kind"] for item in form["items"]} == set(forms.KINDS)
    assert [item["kind"] for item in forms.questions(form["items"])].count("short") == 1 and len(forms.questions(form["items"])) == len(forms.QUESTION_KINDS)
    assert [name for name, template in forms.TEMPLATES.items() if not template.get("hidden")][:2] == ["blank", "everything"]
