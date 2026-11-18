from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from utils import automod as rules
from utils.settings import get_default_settings, update_guild_settings
from utils.warnings import list_warnings


def _settings(**overrides):
    base = {"automod_invites_action": "off", "automod_words_action": "off", "automod_banned_words": "", "automod_mentions_action": "off",
            "automod_mention_limit": 4, "automod_spam_action": "off", "automod_spam_limit": 3}
    return {**base, **overrides}


def test_rules_are_off_by_default(storage):
    defaults = get_default_settings(storage)
    assert not rules.any_rule_enabled(defaults)
    assert rules.content_violation("join discord.gg/abc you BADWORD @everyone", 99, defaults) is None


@pytest.mark.parametrize("text, hit", [
    ("come to discord.gg/abc123", True), ("https://discord.com/invite/Xy-z", True), ("DISCORDAPP.COM/INVITE/q", True),
    ("discord.com/channels/1/2/3 is a message link", False), ("I like discord", False),
])
def test_invite_links_are_found_but_message_links_are_not(text, hit):
    assert (rules.content_violation(text, 0, _settings(automod_invites_action="delete")) == "invites") is hit


def test_banned_words_match_whole_words_and_phrases_in_any_case():
    settings = _settings(automod_words_action="warn", automod_banned_words="darn, Heck\nbad phrase , ")
    assert rules.banned_words(settings["automod_banned_words"]) == ["darn", "heck", "bad phrase"]
    for text in ("well DARN.", "what the heck?", "a Bad Phrase here"):
        assert rules.content_violation(text, 0, settings) == "words"
    for text in ("darning socks", "checkmate", "bad  phrases", ""):
        assert rules.content_violation(text, 0, settings) is None
    assert rules.content_violation("darn", 0, _settings(automod_words_action="warn")) is None  # an empty list bans nothing


def test_mention_limit_and_rule_order():
    settings = _settings(automod_mentions_action="mute", automod_invites_action="delete")
    assert rules.content_violation("hi", 3, settings) is None and rules.content_violation("hi", 4, settings) == "mentions"
    assert rules.content_violation("discord.gg/x", 9, settings) == "invites"  # the first rule that matches is the one reported


def test_spam_is_a_burst_inside_the_window_and_resets_after_a_hit():
    tracker = rules.SpamTracker()
    assert [tracker.hit(1, 5, 3, now=t) for t in (0, 1, 2)] == [False, False, True]
    assert [tracker.hit(1, 5, 3, now=t) for t in (3, 4)] == [False, False]        # counting started over
    assert tracker.hit(1, 6, 3, now=4) is False                                    # other members are separate
    slow = rules.SpamTracker()
    assert [slow.hit(1, 5, 3, now=t) for t in (0, 5, 10, 15)] == [False] * 4       # spread out is not spam


def _message(storage, content, *, manage=False, roles=(), mentions=0, channel_id=7):
    deleted, notices = [], []

    async def delete():
        deleted.append(True)

    async def send(text, **_):
        notices.append(text)

    async def timeout(delta, reason=None):
        author.timed_out = delta

    guild = SimpleNamespace(id=1, owner_id=999, name="G", me=SimpleNamespace(top_role=10))
    author = SimpleNamespace(
        id=5, bot=False, mention="<@5>", roles=[SimpleNamespace(id=r) for r in roles], top_role=1, timeout=timeout, timed_out=None,
        guild_permissions=SimpleNamespace(manage_messages=manage),
    )
    message = SimpleNamespace(
        guild=guild, author=author, content=content, raw_mentions=list(range(mentions)), raw_role_mentions=[], mention_everyone=False,
        channel=SimpleNamespace(id=channel_id, send=send), delete=delete,
    )
    return message, deleted, notices


def _cog(storage, monkeypatch):
    import discord

    from cogs.automod import Automod

    monkeypatch.setattr(discord, "Member", SimpleNamespace)  # the fakes stand in for members
    logged, escalated = [], []

    async def try_dm(*a, **k):
        pass

    async def log(guild, **kwargs):
        logged.append(kwargs["title"])

    async def escalate(guild, member, total):
        escalated.append(total)

    moderation = SimpleNamespace(_try_dm=try_dm, _log_mod_action=log, escalate_warning=escalate)
    embeds = SimpleNamespace(warning_embed=lambda *a, **k: None, warning_color=None)
    bot = SimpleNamespace(storage_path=storage, user=SimpleNamespace(id=42), embeds=embeds, get_cog=lambda name: moderation)
    return Automod(bot), logged, escalated


def test_a_warn_rule_deletes_warns_logs_and_feeds_escalation(storage, monkeypatch):
    cog, logged, escalated = _cog(storage, monkeypatch)
    update_guild_settings(storage, 1, {"automod_invites_action": "warn"})
    message, deleted, notices = _message(storage, "discord.gg/free-nitro")
    assert asyncio.run(cog.check(message)) == "invites"
    assert deleted == [True] and "invite links" in notices[0]
    warnings = list_warnings(storage, 1, 5)
    assert len(warnings) == 1 and warnings[0]["moderator_id"] == 42 and warnings[0]["reason"].startswith("Automod")
    assert logged == ["⚠️ Automod Warning"] and escalated == [1]


def test_a_mute_rule_times_the_member_out_and_delete_only_leaves_no_record(storage, monkeypatch):
    cog, logged, _ = _cog(storage, monkeypatch)
    update_guild_settings(storage, 1, {"automod_mentions_action": "mute", "automod_mention_limit": 3, "automod_mute_duration": "30m",
                                       "automod_words_action": "delete", "automod_banned_words": "heck"})
    message, deleted, _ = _message(storage, "hey", mentions=3)
    assert asyncio.run(cog.check(message)) == "mentions" and message.author.timed_out.total_seconds() == 1800 and logged == ["🔇 Automod Mute"]
    quiet, deleted, _ = _message(storage, "heck")
    assert asyncio.run(cog.check(quiet)) == "words" and deleted == [True]
    assert list_warnings(storage, 1, 5) == [] and logged == ["🔇 Automod Mute"]


def test_moderators_exempt_roles_and_exempt_channels_are_left_alone(storage, monkeypatch):
    cog, _, _ = _cog(storage, monkeypatch)
    update_guild_settings(storage, 1, {"automod_invites_action": "delete", "automod_exempt_roles": [77], "automod_exempt_channels": [8]})
    for kwargs in ({"manage": True}, {"roles": (77,)}, {"channel_id": 8}):
        message, deleted, _ = _message(storage, "discord.gg/x", **kwargs)
        assert asyncio.run(cog.check(message)) is None and deleted == []
    message, deleted, _ = _message(storage, "discord.gg/x")
    assert asyncio.run(cog.check(message)) == "invites"


def test_spam_rule_fires_on_new_messages_only(storage, monkeypatch):
    cog, _, _ = _cog(storage, monkeypatch)
    update_guild_settings(storage, 1, {"automod_spam_action": "delete", "automod_spam_limit": 3})
    results = [asyncio.run(cog.check(_message(storage, "hi")[0])) for _ in range(3)]
    assert results == [None, None, "spam"]
    assert asyncio.run(cog.check(_message(storage, "hi")[0], edited=True)) is None


def test_panel_cannot_save_an_unknown_action_or_a_bad_duration(storage):
    from cogs.panel_bridge import SettingsValidationError, validate_settings

    defaults = get_default_settings(storage)
    saved = validate_settings({**defaults, "automod_spam_action": "mute", "automod_exempt_roles": ["5", 5], "automod_mute_duration": " 1h "}, defaults)
    assert saved["automod_spam_action"] == "mute" and saved["automod_exempt_roles"] == [5] and saved["automod_mute_duration"] == "1h"
    for bad in ({"automod_spam_action": "ban"}, {"automod_mute_duration": "soon"}, {"automod_spam_limit": 1},
                {"automod_banned_words": "x" * 2001}, {"automod_exempt_channels": ["abc"]}):
        with pytest.raises(SettingsValidationError):
            validate_settings({**defaults, **bad}, defaults)
