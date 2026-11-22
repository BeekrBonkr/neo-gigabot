"""Forms, where they meet Discord: who is allowed to answer one, what is
posted and done when an answer arrives or is decided, and the ban appeal,
which is a form with rules of its own. The website (through the panel bridge)
and the bot's own /form command both go through here, so a form behaves the
same wherever it is filled in.
"""

from __future__ import annotations

import json
import logging
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
        form["options"].update(review=True, multiple=False, audience="anyone", anonymous=False, editable=False,
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
        if member is None and (options["audience"] != "anyone" or options["require_role_id"] or options["min_member_days"]):
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

    async def respond(self, guild: discord.Guild, form: dict[str, Any], user_id: int, user_name: str, answers: Any,
                      *, test: bool = False, response_id: int | None = None) -> dict[str, Any]:
        """Take one person's answers. `test` is staff trying the form out: it
        skips who-may-answer and is kept apart. With `response_id`, the
        answers replace those of the person's own earlier response."""
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

    def action_problem(self, guild: discord.Guild, options: dict[str, Any], member: discord.Member | None) -> str | None:
        """Why this person may not save a form with these actions, or None.
        Roles follow the rules of the role commands: none at or above the
        saver's own highest role or the bot's, none managed, and none that
        carries moderation permissions unless a person approves each one."""
        for action in options["actions"]:
            if action["do"] in ("add_role", "remove_role"):
                role = guild.get_role(int(action["role_id"]))
                if role is None or role.is_default() or role.managed:
                    return "One of the roles in the actions can't be given or removed by a bot."
                if not role < guild.me.top_role:
                    return f"{role.name} is above the bot's highest role, so the bot can't give or remove it."
                if member is not None and member.id != guild.owner_id and role >= member.top_role:
                    return f"{role.name} is at or above your own highest role."
                if action["do"] == "add_role" and action["on"] == "submit" and role.permissions.value & ELEVATED:
                    return f"{role.name} carries moderation permissions, so it can only be given when a response is approved."
            if action["do"] in ("post", "thread"):
                channel = guild.get_channel(int(action["channel_id"]))
                if channel is None or not hasattr(channel, "send"):
                    return "One of the actions points at a channel that isn't a text channel of this server."
                if member is not None and not channel.permissions_for(member).send_messages:
                    return f"You can't post in #{channel.name} yourself, so a form of yours can't either."
            if action["do"] == "thread" and options["anonymous"]:
                return "A thread shows who answered, so an anonymous form can't open one."
        if options["channel_id"]:
            channel = guild.get_channel(int(options["channel_id"]))
            if channel is None or not hasattr(channel, "send"):
                return "The channel for new responses isn't a text channel of this server."
        if options["require_role_id"] and guild.get_role(int(options["require_role_id"])) is None:
            return "The role people need to answer isn't a role of this server."
        return None

    def _fill(self, text: str, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any]) -> str:
        return (text.replace("{mention}", self.who(form, response)).replace("{user}", self.who(form, response, mention=False))
                .replace("{form}", form["title"]).replace("{server}", guild.name).replace("{status}", response["status"]))

    async def run_actions(self, guild: discord.Guild, form: dict[str, Any], response: dict[str, Any], moment: str) -> list[str]:
        """Do what the form says should happen at this moment. One action
        failing never stops the others; what failed is returned and logged."""
        failed: list[str] = []
        member = guild.get_member(int(response["user_id"]))
        for action in form["options"]["actions"]:
            if action["on"] != moment:
                continue
            try:
                if action["do"] in ("add_role", "remove_role"):
                    role = guild.get_role(int(action["role_id"]))
                    if member is None or role is None or role.managed or role.is_default() or not role < guild.me.top_role:
                        raise FormError("the role can't be changed for this person")
                    change = member.add_roles if action["do"] == "add_role" else member.remove_roles
                    await change(role, reason=f"Form \"{form['title'][:60]}\": response {moment}")
                elif action["do"] == "post":
                    channel = guild.get_channel(int(action["channel_id"]))
                    if channel is None:
                        raise FormError("the channel is gone")
                    await channel.send(self._fill(action["text"], guild, form, response),
                                       allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=int(response["user_id"]))]))
                elif action["do"] == "dm":
                    user = member or await self.bot.fetch_user(int(response["user_id"]))
                    await user.send(embed=self.bot.embeds.info_embed(form["title"][:200], self._fill(action["text"], guild, form, response)))
                elif action["do"] == "thread":
                    channel = guild.get_channel(int(action["channel_id"]))
                    if channel is None or not hasattr(channel, "create_thread"):
                        raise FormError("the channel can't hold threads")
                    name = f"{form['title'][:60]} - {response['user_name'] or response['user_id']}"[:100]
                    thread = await channel.create_thread(name=name, type=discord.ChannelType.private_thread, invitable=False,
                                                         reason=f"Form \"{form['title'][:60]}\"")
                    forms.set_thread(self.storage, response["id"], thread.id)
                    link = self.responses_link(form, "Their response")
                    await thread.send((self._fill(action["text"], guild, form, response) + "\n\n" if action["text"] else "")
                                      + f"<@{response['user_id']}>" + (f" · {link}" if link else ""),
                                      allowed_mentions=discord.AllowedMentions(everyone=False, roles=False, users=[discord.Object(id=int(response["user_id"]))]))
            except (discord.HTTPException, FormError) as exc:
                failed.append(f"{action['do']}: {exc}")
                log.warning("A form action failed in guild %s (form %s, %s on %s): %s", guild.id, form["id"], action["do"], moment, exc)
        return failed

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
