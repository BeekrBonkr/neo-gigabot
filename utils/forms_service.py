"""Forms, where they meet Discord: who is allowed to answer one, what is
posted and done when an answer arrives or is decided, and the ban appeal,
which is a form with rules of its own. The website (through the panel bridge)
and the bot's own /form command both go through here, so a form behaves the
same wherever it is filled in.
"""

from __future__ import annotations

import io
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from utils import appeals as appeal_store
from utils import forms
from utils import tempbans as tempban_store
from utils.db import connect
from utils.forms import FormError
from utils.numbers import is_digits
from utils.settings import get_guild_settings, get_mod_log_channel_id
from utils.website import site_link, site_url

log = logging.getLogger(__name__)

NOT_AVAILABLE = "That form isn't available."
ELEVATED = discord.Permissions.elevated().value
SHOWN_ANSWERS = 6  # how many answers an announcement in a channel carries


def days_since(moment: datetime | None) -> int:
    return (datetime.now(timezone.utc) - moment).days if moment else 0


class FormsService:
    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @property
    def storage(self) -> Path:
        return self.bot.storage_path

    # ---------- finding forms ----------

    def link(self, form: dict[str, Any]) -> str:
        """Where the form is filled in on the website ("" when the bot has none)."""
        if form["kind"] == forms.APPEAL:
            return site_url(self.bot, f"/appeal/{form['guild_id']}")
        return site_url(self.bot, f"/forms/{form['guild_id']}/{form['slug']}")

    def responses_link(self, form: dict[str, Any], label: str = "Read it on the website") -> str:
        return site_link(self.bot, label, f"/app/{form['guild_id']}/forms/{form['id']}/responses")

    def appeal_form(self, guild_id: int) -> dict[str, Any]:
        """This server's ban appeal: a built-in form, made the first time it is
        needed from the questions the server already had, with the appeals
        sent before forms existed brought along."""
        settings = get_guild_settings(self.storage, guild_id)
        found = next((form for form in forms.list_forms(self.storage, guild_id) if form["kind"] == forms.APPEAL), None)
        if found is None:
            items = [{"kind": question["type"], "text": question["text"], "options": question["options"], "required": question["required"]}
                     for question in appeal_store.questions_for(settings)]
            found = forms.create_form(self.storage, guild_id, 0, title="Ban appeal", kind=forms.APPEAL, items=items, description="",
                                      options={"review": True, "notify_outcome": True}, status=forms.OPEN)
            self._bring_old_appeals(found)
        return self.with_appeal_settings(found, settings)

    def _bring_old_appeals(self, form: dict[str, Any]) -> None:
        appeal_store.ensure_table(self.storage)
        with connect(self.storage) as connection:
            for row in connection.execute("SELECT * FROM ban_appeals WHERE guild_id = ? ORDER BY id", (form["guild_id"],)).fetchall():
                answers = [{"id": "", "kind": "paragraph", "question": pair.get("question", ""), "answer": pair.get("answer", ""), "value": pair.get("answer", "")}
                           for pair in json.loads(row["answers"])]
                connection.execute(
                    "INSERT INTO form_responses (form_id, guild_id, user_id, user_name, answers, status, test, decided_by, decision_note, created_at, updated_at, decided_at)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (form["id"], form["guild_id"], row["user_id"], row["user_name"], json.dumps(answers), row["status"], row["test"], row["decided_by"],
                     row["decision_note"], row["created_at"], row["decided_at"] or row["created_at"], row["decided_at"]),
                )

    @staticmethod
    def with_appeal_settings(form: dict[str, Any], settings: dict[str, Any]) -> dict[str, Any]:
        """A ban appeal takes its switch, its introduction, its wait after a
        denial and its channel from the Ban appeals module's settings."""
        if form["kind"] != forms.APPEAL:
            return form
        form = {**form, "options": dict(form["options"])}
        form["status"] = forms.OPEN if settings.get("appeals_enabled") else forms.CLOSED
        form["description"] = str(settings.get("appeals_intro") or "")
        form["options"].update(review=True, multiple=False, audience="anyone", anonymous=False, editable=False, answered_in=forms.WEBSITE,
                               retry_days=appeal_store.reappeal_days(settings), channel_id=str(settings.get("appeals_channel_id") or ""))
        return form

    def get(self, guild_id: int, form: int | str) -> dict[str, Any] | None:
        found = forms.get_form(self.storage, guild_id, form)
        if found is not None and found["kind"] == forms.APPEAL:
            return self.with_appeal_settings(found, get_guild_settings(self.storage, guild_id))
        return found

    def names(self, guild: discord.Guild) -> forms.Names:
        def member(user_id: int) -> str | None:
            found = guild.get_member(user_id)
            return str(found) if found is not None else None

        def role(role_id: int) -> str | None:
            found = guild.get_role(role_id)
            return f"@{found.name}" if found is not None and not found.is_default() else None

        def channel(channel_id: int) -> str | None:
            found = guild.get_channel(channel_id)
            return f"#{found.name}" if found is not None else None

        return forms.Names(member=member, role=role, channel=channel)

    # ---------- who may answer ----------

    async def eligible(self, guild: discord.Guild, form: dict[str, Any], user_id: int) -> tuple[bool, str]:
        """Whether this person is someone the form is for at all. A refusal
        that would tell an outsider the form exists reads NOT_AVAILABLE."""
        options = form["options"]
        if form["kind"] == forms.APPEAL:
            if form["status"] != forms.OPEN:
                return False, NOT_AVAILABLE
            try:
                await guild.fetch_ban(discord.Object(id=user_id))
            except discord.NotFound:
                return False, NOT_AVAILABLE
            except discord.HTTPException as exc:
                raise FormError("The bot can't read this server's ban list right now.") from exc
            return True, ""
        member = guild.get_member(user_id)
        if member is None and (options["audience"] != "anyone" or options["require_role_id"] or options["min_member_days"] or options.get("answered_in") == forms.DISCORD):
            return False, NOT_AVAILABLE
        if options["require_role_id"] and not any(str(role.id) == options["require_role_id"] for role in member.roles):
            role = guild.get_role(int(options["require_role_id"]))
            return False, f"This form is for members with the {role.name if role else 'required'} role."
        if options["min_account_days"] and days_since(discord.utils.snowflake_time(user_id)) < options["min_account_days"]:
            return False, f"This form is for Discord accounts at least {options['min_account_days']} days old."
        if options["min_member_days"] and days_since(getattr(member, "joined_at", None)) < options["min_member_days"]:
            return False, f"This form is for people who have been in the server at least {options['min_member_days']} days."
        return True, ""

    async def standing(self, guild: discord.Guild, form: dict[str, Any], user_id: int) -> dict[str, Any]:
        """Everything the form's page needs to know about this person: may they
        answer now, why not, and what they have sent before."""
        allowed, why = await self.eligible(guild, form, user_id)
        if not allowed and why == NOT_AVAILABLE:
            return {"available": False, "can_respond": False, "reason": why, "mine": []}
        mine = forms.responses_of(self.storage, form["id"], user_id)
        if allowed:
            allowed, why = forms.can_respond(form, mine, forms.count_responses(self.storage, form["id"]))
        return {"available": True, "can_respond": allowed, "reason": why, "mine": mine}

    # ---------- answering ----------

    def wrong_place(self, form: dict[str, Any], via: str) -> str:
        """Why a form can't be answered from here, or "" when it can."""
        where = form["options"].get("answered_in", forms.WEBSITE)
        if where == via:
            return ""
        if where == forms.DISCORD:
            return f"This form is answered in Discord: run /form in the server and pick \"{forms.plain(form['title'])}\", or use its button."
        link = self.link(form)
        return "This form is answered on the website" + (f": {link}" if link else ".")

    async def respond(self, guild: discord.Guild, form: dict[str, Any], user_id: int, user_name: str, answers: Any,
                      *, test: bool = False, response_id: int | None = None, via: str = forms.WEBSITE) -> dict[str, Any]:
        """Take one person's answers. `test` is staff trying the form out: it
        skips who-may-answer and is kept apart. With `response_id`, the
        answers replace those of the person's own earlier response. `via` is
        where they were given; a form only takes answers from its own place."""
        refused = "" if test else self.wrong_place(form, via)
        if refused:
            raise FormError(refused)
        if not test:
            allowed, why = await self.eligible(guild, form, user_id)
            if not allowed:
                raise FormError(why)
        cleaned = forms.clean_answers(form["items"], answers, self.names(guild))
        if response_id is not None:
            if not forms.edit_response(self.storage, form, response_id, user_id, cleaned):
                raise FormError("That response can't be changed any more.")
            return {"response_id": response_id, "edited": True, "notified": False}
        if not test:
            allowed, why = forms.can_respond(form, forms.responses_of(self.storage, form["id"], user_id), forms.count_responses(self.storage, form["id"]))
            if not allowed:
                raise FormError(why)
        new_id = forms.submit(self.storage, form, user_id, user_name, cleaned, test=test)
        response = forms.get_response(self.storage, guild.id, new_id)
        notified = await self.announce(guild, form, response)
        if not test:
            await self.run_actions(guild, form, response, "submit")
        return {"response_id": new_id, "edited": False, "notified": notified}

    def who(self, form: dict[str, Any], response: dict[str, Any], *, mention: bool = True) -> str:
        if form["options"]["anonymous"]:
            return "Someone"
        return f"<@{response['user_id']}>" if mention else (response["user_name"] or str(response["user_id"]))

    async def announce(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any]) -> bool:
        """Tell the staff, in the form's channel, that a response came in. A
        ban appeal without a channel of its own goes to the mod log."""
        raw = str(form["options"]["channel_id"] or "")
        channel_id = int(raw) if is_digits(raw) else (get_mod_log_channel_id(self.storage, guild.id) if form["kind"] == forms.APPEAL else 0)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is None:
            return False
        test = bool(response["test"])
        if form["kind"] == forms.APPEAL:
            where = site_link(self.bot, "Review it on the website", f"/app/{guild.id}/moderation#appeals") or "Review it on the website's Moderation tab"
            title = "🧪 Test Ban Appeal" if test else "📨 New Ban Appeal"
            text = (f"<@{response['user_id']}> sent a test appeal to try the form out. Nobody was banned or unbanned. {where}." if test
                    else f"<@{response['user_id']}> (`{response['user_id']}`) appealed their ban. {where}.")
            embed = self.bot.embeds.info_embed(title, text)
        else:
            lines = [f"**{entry['question'][:80]}**\n{(entry['answer'] or '*(no answer)*')[:300]}" for entry in response["answers"][:SHOWN_ANSWERS]]
            more = len(response["answers"]) - SHOWN_ANSWERS
            waiting = " It is waiting to be approved or denied." if response["status"] == forms.PENDING else ""
            link = self.responses_link(form)
            embed = self.bot.embeds.info_embed(
                ("🧪 Test response: " if test else "📝 ") + form["title"][:200],
                f"{self.who(form, response)} answered.{waiting}\n\n" + "\n\n".join(lines) + (f"\n\n...and {more} more." if more > 0 else "")
                + (f"\n\n{link}." if link else ""),
            )
        try:
            await channel.send(embed=embed)  # an embed pings nobody
            return True
        except discord.HTTPException:
            return False

    # ---------- what a form does in Discord ----------

    def prepare_actions(self, guild: discord.Guild, options: dict[str, Any], member: discord.Member, items: list[dict[str, Any]],
                        before: list[dict[str, Any]]) -> None:
        """Check the form's actions before they are saved, and note who set each
        one up. An action already on the form, unchanged, keeps whoever set it
        up and is not checked again, so someone with fewer permissions can
        still edit the questions of a form an administrator gave actions.
        Raises FormError pointing at the action that is not allowed."""
        perms, is_owner = member.guild_permissions, member.id == guild.owner_id
        if perms.administrator:
            perms = discord.Permissions.all()  # Administrator counts as every permission
        kinds = {item["id"]: item["kind"] for item in items}
        for index, action in enumerate(options["actions"]):
            kept = next((old for old in before if forms.same_action(old, action)), None)
            if kept is not None:
                action["by"] = kept.get("by", "")
                continue
            action["by"] = str(member.id)
            try:
                self._check_action(guild, action, member, perms, is_owner, kinds)
            except FormError as exc:
                exc.field = f"action:{index}"
                raise
        if options["channel_id"]:
            channel = guild.get_channel(int(options["channel_id"]))
            if channel is None or not hasattr(channel, "send"):
                raise FormError("The channel for new responses isn't a text channel of this server.", "option:channel_id")
        if options["require_role_id"] and guild.get_role(int(options["require_role_id"])) is None:
            raise FormError("The role people need to answer isn't a role of this server.", "option:require_role_id")

    def _check_action(self, guild: discord.Guild, action: dict[str, Any], member: discord.Member, perms: discord.Permissions,
                      is_owner: bool, kinds: dict[str, str]) -> None:
        def role_ok(role: discord.Role | None, label: str = "That role") -> discord.Role:
            if role is None or role.is_default() or role.managed:
                raise FormError(f"{label} can't be given or removed by a bot.")
            if not role < guild.me.top_role:
                raise FormError(f"{role.name} is above the bot's highest role, so the bot can't give or remove it.")
            if not is_owner and role >= member.top_role:
                raise FormError(f"{role.name} is at or above your own highest role.")
            return role

        def text_channel(raw: str, label: str = "the channel") -> Any:
            channel = guild.get_channel(int(raw)) if raw else None
            if channel is None or not hasattr(channel, "send"):
                raise FormError(f"Choose a text channel of this server for {label}.")
            if not channel.permissions_for(member).send_messages:
                raise FormError(f"You can't post in #{channel.name} yourself, so a form of yours can't either.")
            return channel

        for question, what in ((action["if_question"], "condition"), (action["question"], "role question")):
            if question and question not in kinds:
                raise FormError(f"The {what} points at a question that is no longer on the form.")
        do = action["do"]
        if do in ("add_role", "remove_role", "picked_role") and not (is_owner or perms.manage_roles):
            raise FormError("Giving or removing roles needs the Manage Roles permission.")
        if do in ("add_role", "remove_role"):
            role = role_ok(guild.get_role(int(action["role_id"])))
            if do == "add_role" and action["on"] == "submit" and role.permissions.value & ELEVATED:
                raise FormError(f"{role.name} carries moderation permissions, so it can only be given when a response is approved.")
        elif do == "picked_role":
            if kinds.get(action["question"]) != "role":
                raise FormError("Choose a 'Pick a role' question: its answer is the role that is given.")
        elif do == "nickname":
            if not (is_owner or perms.manage_nicknames):
                raise FormError("Changing nicknames needs the Manage Nicknames permission.")
        elif do in ("post", "summary", "thread"):
            channel = text_channel(action["channel_id"])
            if do == "thread" and not hasattr(channel, "create_thread"):
                raise FormError(f"#{channel.name} can't hold threads.")
        elif do == "channel":
            if not (is_owner or perms.manage_channels):
                raise FormError("Opening a channel needs the Manage Channels permission.")
            if action["log_channel_id"]:
                text_channel(action["log_channel_id"], "the transcripts")
            if action["category_id"]:
                category = guild.get_channel(int(action["category_id"]))
                if not isinstance(category, discord.CategoryChannel):
                    raise FormError("Choose a category of this server for the new channels, or none.")
            for raw in action["roles"]:
                if guild.get_role(int(raw)) is None:
                    raise FormError("One of the roles that can see the new channel isn't a role of this server.")
        if action["ping_role_id"]:
            role = guild.get_role(int(action["ping_role_id"]))
            if role is None:
                raise FormError("The role to ping isn't a role of this server.")
            if not role.mentionable and not (is_owner or perms.mention_everyone):
                raise FormError(f"@{role.name} can't be pinged by everyone, and pinging it anyway needs the Mention @everyone permission.")

    def _role_for(self, guild: discord.Guild, action: dict[str, Any], role: discord.Role | None, *, picked: bool) -> discord.Role:
        """A role an action is about to give or remove, checked again now: the
        server may have changed since the action was set up. It has to be below
        the bot, and below whoever set the action up (unless that is the owner).
        A role someone picked in their own answer can never carry moderation permissions."""
        if role is None or role.is_default() or role.managed or not role < guild.me.top_role:
            raise FormError("the role can't be changed by the bot")
        if picked and role.permissions.value & ELEVATED:
            raise FormError(f"{role.name} carries moderation permissions, so it is never given from someone's own answer")
        setter = guild.get_member(int(action["by"])) if action.get("by") else None
        if setter is None:
            raise FormError("whoever set this action up is no longer in the server")
        if setter.id != guild.owner_id and role >= setter.top_role:
            raise FormError(f"{role.name} is no longer below the highest role of whoever set this action up")
        return role

    PLACEHOLDER = re.compile(r"\{([^{}\n]{1,200})\}")
    # The form data a message can use besides the answers, and what each is, for the builder's list.
    DATA = {
        "user": "Their name", "mention": "Pings them", "name": "Their name in this server", "user_id": "Their Discord ID",
        "number": "The response's number in this form", "answers": "Every question and answer", "date": "The day it was sent (UTC)",
        "time": "The time it was sent (UTC)", "form": "This form's title", "form_link": "This form's link", "responses_link": "Where staff read the responses",
        "responses": "How many responses the form has", "server": "The server's name", "members": "How many members the server has",
        "status": "submitted, pending, approved or denied", "note": "The staff's note when deciding", "decided_by": "Who decided it",
        "account_created": "When their Discord account was made", "joined": "When they joined the server",
    }

    def data(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any]) -> dict[str, str]:
        """Everything a message can say about a response, by name. On an anonymous form, nothing that says who answered."""
        hidden = form["options"]["anonymous"] and not response.get("test")
        member = guild.get_member(int(response["user_id"]))
        sent = forms._moment(response.get("created_at")) or datetime.now(timezone.utc)
        decider = response.get("decided_by")
        lines = [f"**{forms.plain(entry['question'])}**\n{entry['answer'] or '(no answer)'}" for entry in response["answers"]]
        values = {
            "user": self.who(form, response, mention=False), "mention": self.who(form, response),
            "name": "Someone" if hidden else (member.display_name if member else (response["user_name"] or str(response["user_id"]))),
            "user_id": "" if hidden else str(response["user_id"]), "number": str(response.get("number") or "test"),
            "answers": "\n\n".join(lines), "date": f"{sent:%Y-%m-%d}", "time": f"{sent:%H:%M} UTC", "form": forms.plain(form["title"]),
            "form_link": self.link(form), "responses_link": site_url(self.bot, f"/app/{form['guild_id']}/forms/{form['id']}/responses"),
            "responses": str(forms.count_responses(self.storage, form["id"])), "server": guild.name, "members": f"{guild.member_count or 0:,}",
            "status": response["status"], "note": response.get("decision_note") or "", "decided_by": f"<@{decider}>" if decider else "",
            "account_created": "" if hidden else f"{discord.utils.snowflake_time(int(response['user_id'])):%Y-%m-%d}",
            "joined": "" if hidden or member is None or member.joined_at is None else f"{member.joined_at:%Y-%m-%d}",
        }
        return values

    def fill(self, text: str, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any]) -> str:
        """A message with its form data filled in: the names in DATA, and any
        answer, by its question's ID ({q3}) or by the question itself
        ({What should we call you?}). Anything else is left as it is."""
        fixed = self.data(guild, form, response)
        answers = {entry["id"]: entry["answer"] for entry in response["answers"] if entry.get("id")}
        by_text = {forms.plain(entry["question"]).strip().casefold(): entry["answer"] for entry in response["answers"]}

        def one(match: re.Match[str]) -> str:
            key = match.group(1).strip()
            if key in fixed:
                return fixed[key]
            if key in answers:
                return answers[key]
            return by_text.get(forms.plain(key).strip().casefold(), match.group(0))

        return self.PLACEHOLDER.sub(one, text)

    def compose(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any] | None, message: dict[str, Any],
                *, prefix: str = "") -> tuple[str | None, discord.Embed | None]:
        """A message as the bot posts it: plain text, or an embed with its
        title, color, image and footer. Form data is filled in where there is a
        response to take it from. `prefix` goes before it as plain text (a ping)."""
        fill = (lambda text: self.fill(text, guild, form, response)) if response is not None else (lambda text: text)
        text = fill(message.get("text") or "")
        if message.get("style") != "embed":
            content = (prefix + text)[:2000]
            return content or None, None
        embed = discord.Embed(title=fill(message.get("title") or "")[:256] or None, description=text[:4096] or None,
                              color=discord.Color(int(message["color"][1:], 16)) if message.get("color") else discord.Color.blurple())
        if message.get("image"):
            embed.set_image(url=message["image"])
        if message.get("footer"):
            embed.set_footer(text=fill(message["footer"])[:2048])
        return prefix or None, embed

    async def run_actions(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any], moment: str) -> list[str]:
        """Do what the form says should happen at this moment, where the
        response meets the action's condition. One action failing never stops
        the others; what failed is returned and logged."""
        failed: list[str] = []
        member = guild.get_member(int(response["user_id"]))
        only_them = discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=int(response["user_id"]))])
        reason = f"Form \"{forms.plain(form['title'])[:60]}\": response {moment}"
        for action in form["options"]["actions"]:
            if action["on"] != moment or not forms.matches(action, response["answers"]):
                continue
            do = action["do"]
            try:
                if do in ("add_role", "remove_role", "picked_role", "nickname") and member is None:
                    raise FormError("they are not in the server")
                if do in ("add_role", "remove_role"):
                    role = self._role_for(guild, action, guild.get_role(int(action["role_id"])), picked=False)
                    await (member.add_roles if do == "add_role" else member.remove_roles)(role, reason=reason)
                elif do == "picked_role":
                    entry = next((entry for entry in response["answers"] if entry.get("id") == action["question"]), None)
                    raw = str((entry or {}).get("value") or "")
                    if not is_digits(raw):
                        continue  # the question was left unanswered: nothing to give
                    await member.add_roles(self._role_for(guild, action, guild.get_role(int(raw)), picked=True), reason=reason)
                elif do == "nickname":
                    if member.id == guild.owner_id or not member.top_role < guild.me.top_role:
                        raise FormError("the bot can't change this person's nickname")
                    nick = " ".join(self.fill(action["text"], guild, form, response).split())[:32]
                    await member.edit(nick=nick or None, reason=reason)
                elif do == "post":
                    channel = guild.get_channel(int(action["channel_id"]))
                    if channel is None:
                        raise FormError("the channel is gone")
                    ping = guild.get_role(int(action["ping_role_id"])) if action["ping_role_id"] else None
                    content, embed = self.compose(guild, form, response, action, prefix=f"{ping.mention} " if ping else "")
                    await channel.send(content, embed=embed, allowed_mentions=discord.AllowedMentions(
                        everyone=False, roles=[ping] if ping else False, users=[discord.Object(id=int(response["user_id"]))]))
                elif do == "summary":
                    channel = guild.get_channel(int(action["channel_id"]))
                    if channel is None:
                        raise FormError("the channel is gone")
                    ping = guild.get_role(int(action["ping_role_id"])) if action["ping_role_id"] else None
                    await channel.send(ping.mention if ping else None, embed=self.summary_embed(guild, form, response, action),
                                       allowed_mentions=discord.AllowedMentions(everyone=False, users=False, roles=[ping] if ping else False))
                elif do == "dm":
                    user = member or await self.bot.fetch_user(int(response["user_id"]))
                    content, embed = self.compose(guild, form, response, action)
                    await user.send(content, embed=embed)
                elif do == "thread":
                    channel = guild.get_channel(int(action["channel_id"]))
                    if channel is None or not hasattr(channel, "create_thread"):
                        raise FormError("the channel can't hold threads")
                    name = self.fill(action["name"], guild, form, response) if action["name"] else f"{forms.plain(form['title'])[:60]} - {response['user_name'] or response['user_id']}"
                    thread = await channel.create_thread(name=name[:100], type=discord.ChannelType.private_thread, invitable=False, reason=reason)
                    forms.set_thread(self.storage, response["id"], thread.id)
                    content, embed = self._opening(guild, form, response, action)
                    await thread.send(content, embed=embed, allowed_mentions=only_them)
                elif do == "channel":
                    await self._open_channel(guild, form, response, action, member, reason, only_them)
            except (discord.HTTPException, FormError) as exc:
                failed.append(f"{do}: {exc}")
                log.warning("A form action failed in guild %s (form %s, %s on %s): %s", guild.id, form["id"], do, moment, exc)
            except Exception:  # noqa: BLE001 - the response is already stored; one broken action must not stop the others
                failed.append(f"{do}: something went wrong")
                log.exception("A form action broke in guild %s (form %s, %s on %s)", guild.id, form["id"], do, moment)
        return failed

    def _opening(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any], action: dict[str, Any]) -> tuple[str | None, discord.Embed | None]:
        """The first message in a thread or channel opened for a response: it pings them, and says what the action says."""
        link = self.responses_link(form, "Their response")
        ping = f"<@{response['user_id']}>"
        if action.get("style") == "embed":
            content, embed = self.compose(guild, form, response, action, prefix=ping)
            return content, embed
        return ((self.fill(action["text"], guild, form, response) + "\n\n") if action["text"] else "") + ping + (f" · {link}" if link else ""), None

    async def _open_channel(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any], action: dict[str, Any],
                            member: discord.Member | None, reason: str, only_them: discord.AllowedMentions) -> None:
        """A text channel only the person, the chosen roles and the bot can see, like a ticket."""
        if member is None:
            raise FormError("they are not in the server")
        category = guild.get_channel(int(action["category_id"])) if action["category_id"] else None
        if action["category_id"] and not isinstance(category, discord.CategoryChannel):
            raise FormError("the category is gone")
        seen = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True)
        overwrites: dict[Any, discord.PermissionOverwrite] = {guild.default_role: discord.PermissionOverwrite(view_channel=False), member: seen,
                                                               guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, manage_channels=True)}
        for raw in action["roles"]:
            role = guild.get_role(int(raw))
            if role is not None:
                overwrites[role] = seen
        wanted = self.fill(action["name"], guild, form, response) if action["name"] else f"{forms.plain(form['title'])}-{response['user_name'] or response['user_id']}"
        name = re.sub(r"[^a-z0-9_-]+", "-", wanted.lower()).strip("-")[:90] or "form-response"
        channel = await guild.create_text_channel(name, category=category, overwrites=overwrites, reason=reason)
        forms.set_thread(self.storage, response["id"], channel.id)
        content, embed = self._opening(guild, form, response, action)
        view = None
        if action.get("close_button"):
            from cogs.forms import ticket_view  # the cog's own button, so presses are handled the same way

            view = ticket_view(response["id"])
        await channel.send(content, embed=embed, view=view, allowed_mentions=only_them)

    def summary_embed(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any], action: dict[str, Any]) -> discord.Embed:
        """The whole response, one field a question, for a summary posted in a channel."""
        intro = self.fill(action["text"], guild, form, response) if action["text"] else f"{self.who(form, response)} answered."
        embed = self.bot.embeds.create(title=f"📝 {forms.plain(form['title'])}"[:256], description=intro[:4000])
        for entry in response["answers"][:24]:
            embed.add_field(name=forms.plain(entry["question"])[:256] or "Question", value=(entry["answer"] or "*(no answer)*")[:1024], inline=False)
        link = self.responses_link(form, "All responses")
        if len(response["answers"]) > 24 or link:
            embed.add_field(name="More", value=(f"{len(response['answers']) - 24} more answers. " if len(response["answers"]) > 24 else "") + (link or ""), inline=False)
        return embed

    # ---------- tickets ----------

    def ticket_action(self, form: dict[str, Any]) -> dict[str, Any] | None:
        return next((action for action in form["options"]["actions"] if action["do"] == "channel" and action.get("close_button")), None)

    def may_close(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any], user: Any) -> bool:
        """The person the ticket is for, the staff roles that can see it, and anyone who can manage channels."""
        if user.id in (int(response["user_id"]), guild.owner_id):
            return True
        permissions = getattr(user, "guild_permissions", None)
        if permissions is not None and (permissions.administrator or permissions.manage_channels):
            return True
        action = self.ticket_action(form) or {}
        staff = {int(raw) for raw in action.get("roles", [])}
        return any(role.id in staff for role in getattr(user, "roles", []))

    async def close_ticket(self, guild: discord.Guild, channel: Any, form: dict[str, Any], response: dict[str, Any], closer: Any) -> bool:
        """Save the ticket's conversation to the transcript channel, if one was
        chosen, then delete the ticket channel. Returns whether a transcript was saved."""
        action = self.ticket_action(form) or {}
        log_channel = guild.get_channel(int(action["log_channel_id"])) if action.get("log_channel_id") else None
        saved = False
        if log_channel is not None:
            lines = []
            async for message in channel.history(limit=2000, oldest_first=True):
                when = f"{message.created_at:%Y-%m-%d %H:%M}"
                body = message.content or ""
                for embed in message.embeds:
                    body += "".join(f"\n[embed] {part}" for part in (embed.title, embed.description) if part)
                for attachment in message.attachments:
                    body += f"\n[file] {attachment.url}"
                lines.append(f"[{when} UTC] {message.author} ({message.author.id}): {body}")
            text = f"{forms.plain(form['title'])}, response {response.get('number') or response['id']}, in #{channel.name}\n\n" + "\n".join(lines)
            embed = self.bot.embeds.info_embed(
                f"🎫 Ticket closed: {forms.plain(form['title'])[:200]} #{response.get('number') or response['id']}",
                f"Opened by <@{response['user_id']}>, closed by {closer.mention}. {len(lines)} message{'s' if len(lines) != 1 else ''}.")
            try:
                await log_channel.send(embed=embed, file=discord.File(io.BytesIO(text.encode("utf-8")), filename=f"ticket-{response.get('number') or response['id']}.txt"))
                saved = True
            except discord.HTTPException as exc:
                log.warning("Could not save a ticket transcript in guild %s: %s", guild.id, exc)
        await channel.delete(reason=f"Ticket closed by {closer} ({closer.id})")
        return saved

    async def make_ticket(self, guild: discord.Guild, member: discord.Member, *, title: str, button_channel_id: str, staff_roles: list[str],
                          category_id: str, log_channel_id: str, post_now: bool) -> tuple[dict[str, Any], str]:
        """A Ticket form, set up and ready: a Discord form whose responses each
        open a private channel for the person and the staff, with a close
        button, and an "Open a ticket" message in a channel. Returns the form
        and a sentence about what was done."""
        action = forms.clean_action({
            "on": "submit", "do": "channel", "category_id": category_id, "roles": staff_roles, "name": "ticket-{number}",
            "style": "embed", "title": "Ticket #{number}: {q1}", "text": "{answers}\n\nThe staff will be with you soon. Press the button below when this is sorted.",
            "close_button": True, "log_channel_id": log_channel_id,
        })
        button = forms.clean_button_message({"style": "embed", "title": "Need help?", "label": "Open a ticket",
                                             "text": "Press the button below to open a ticket. Only you and the staff will see it."})
        options = forms.clean_options({"answered_in": forms.DISCORD, "ticket": True, "multiple": True, "actions": [action], "button_message": button})
        template = forms.TEMPLATES["ticket"]
        self.prepare_actions(guild, options, member, forms.clean_items(template["items"]), [])
        if not button_channel_id or guild.get_channel(int(button_channel_id)) is None:
            raise FormError("Choose the channel for the 'Open a ticket' message.", "option:button_channel_id")
        made = forms.create_form(self.storage, guild.id, member.id, title=title or template["title"], template="ticket", options=options,
                                 status=forms.OPEN if post_now else forms.CLOSED)
        if not post_now:
            return made, "It is a draft: open it and post its button when you are ready"
        return made, ""

    # ---------- deciding ----------

    async def decide(self, guild: discord.Guild, form: dict[str, Any], response_id: int, approved: bool, moderator: discord.Member, note: str = "") -> dict[str, Any]:
        """Approve or deny a waiting response, then tell the person and run the
        form's actions. Approving a ban appeal unbans first: if Discord refuses,
        the appeal stays open instead of reading approved."""
        waiting = forms.get_response(self.storage, guild.id, response_id)
        if waiting is None or waiting["form_id"] != form["id"] or waiting["status"] != forms.PENDING or waiting["test"]:
            raise FormError("That response was already decided.")
        if form["kind"] == forms.APPEAL and approved:
            try:
                await guild.unban(discord.Object(id=int(waiting["user_id"])), reason=f"Ban appeal approved by {moderator} ({moderator.id})")
            except discord.NotFound:
                pass  # already unbanned by hand
            except discord.HTTPException as exc:
                raise FormError(f"Discord refused the unban: {exc}") from exc
            tempban_store.remove_tempban(self.storage, guild.id, int(waiting["user_id"]))
        decided = forms.decide(self.storage, guild.id, response_id, approved, moderator.id, note)
        if decided is None:
            raise FormError("That response was already decided.")
        failed = await self.run_actions(guild, form, decided, "approve" if approved else "deny")
        told = False
        if form["options"]["notify_outcome"]:
            if form["kind"] == forms.APPEAL:
                title = "Ban Appeal"
                text = f"Your ban appeal for **{guild.name}** was approved. You can rejoin the server." if approved else f"Your ban appeal for **{guild.name}** was denied."
            else:
                title = form["title"][:200]
                text = f"Your response to **{form['title']}** in **{guild.name}** was {'approved' if approved else 'not accepted'}."
            text += f"\nNote from the staff: {decided['decision_note']}" if decided["decision_note"] else ""
            try:
                user = guild.get_member(int(decided["user_id"])) or self.bot.get_user(int(decided["user_id"])) or await self.bot.fetch_user(int(decided["user_id"]))
                await user.send(embed=self.bot.embeds.info_embed(title, text))
                told = True
            except discord.HTTPException:
                pass  # they share no server with the bot, or have DMs closed
        return {"response": decided, "told": told, "failed": failed}
