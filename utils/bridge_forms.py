"""The panel bridge's routes for forms (mixed into cogs/panel_bridge.py).

Two kinds of caller come through here. Staff, who need the Forms area of the
server's pages (utils/site_access.py): viewing it reads forms and responses,
managing it builds forms and decides responses. And everyone else, who can
open a form they are allowed to answer, answer it, and withdraw what they
sent. The bot checks which is which for every request.
"""

from __future__ import annotations

from typing import Any

import discord
from aiohttp import web

from utils import forms
from utils.forms import FormError
from utils.forms_service import NOT_AVAILABLE, FormsService
from utils.numbers import is_digits

RESPONSES_PER_PAGE = 25
LOOKUP_RESULTS = 20
PICKER_ENTRIES = 250


class FormsRoutes:
    """Needs, from the class it is mixed into: bot, storage, Error (the
    bridge's error type), _guild, _json, _user_id, _site_access, _member_access."""

    def add_form_routes(self, router: Any) -> None:
        router.add_get("/v1/guilds/{guild_id}/forms", self.h_forms)
        router.add_post("/v1/guilds/{guild_id}/forms", self.h_forms_action)
        router.add_get("/v1/guilds/{guild_id}/forms/{form}", self.h_form)
        router.add_get("/v1/guilds/{guild_id}/forms/{form}/lookup", self.h_form_lookup)
        router.add_post("/v1/guilds/{guild_id}/forms/{form}/respond", self.h_form_respond)
        router.add_post("/v1/guilds/{guild_id}/forms/{form}/withdraw", self.h_form_withdraw)
        router.add_get("/v1/guilds/{guild_id}/forms/{form}/responses", self.h_form_responses)
        router.add_post("/v1/guilds/{guild_id}/forms/{form}/decide", self.h_form_decide)

    @property
    def forms_service(self) -> FormsService:
        return FormsService(self.bot)

    def _staff(self, guild: discord.Guild, user_id: int, level: str = "view") -> bool:
        return bool(self._member_access(guild, user_id)["access"]["forms"][level])

    def _form(self, guild: discord.Guild, raw: str) -> dict[str, Any]:
        service = self.forms_service
        if raw == "appeal":
            return service.appeal_form(guild.id)
        found = service.get(guild.id, int(raw) if is_digits(raw) else raw)
        if found is None:
            raise self.Error(404, NOT_AVAILABLE)
        return found

    def _summary_line(self, form: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": form["id"], "slug": form["slug"], "kind": form["kind"], "title": form["title"], "description": form["description"],
            "status": form["status"], "questions": len(forms.questions(form["items"])), "responses": form.get("responses", 0),
            "waiting": form.get("waiting", 0), "review": form["options"]["review"], "anonymous": form["options"]["anonymous"],
            "audience": form["options"]["audience"], "link": self.forms_service.link(form), "updated_at": form["updated_at"],
        }

    def _for_answering(self, form: dict[str, Any]) -> dict[str, Any]:
        """A form as the people answering it may see it: no staff-only rules."""
        options = form["options"]
        return {
            "id": form["id"], "slug": form["slug"], "kind": form["kind"], "title": form["title"], "description": form["description"],
            "items": form["items"], "review": options["review"], "anonymous": options["anonymous"], "editable": options["editable"],
            "multiple": options["multiple"], "limits": {"answer": forms.MAX_ANSWER, "short": forms.MAX_SHORT_ANSWER},
        }

    @staticmethod
    def _own(response: dict[str, Any]) -> dict[str, Any]:
        return {"id": response["id"], "status": response["status"], "answers": response["answers"], "created_at": response["created_at"],
                "decision_note": response["decision_note"], "decided_at": response["decided_at"]}

    @staticmethod
    def _for_staff(form: dict[str, Any], response: dict[str, Any]) -> dict[str, Any]:
        """A response as staff see it. Who sent it is left out of an anonymous form's."""
        hidden = form["options"]["anonymous"] and not response["test"]
        return {
            "id": response["id"], "status": response["status"], "test": bool(response["test"]), "answers": response["answers"],
            "created_at": response["created_at"], "updated_at": response["updated_at"], "decided_at": response["decided_at"],
            "decided_by": str(response["decided_by"]) if response["decided_by"] else None, "decision_note": response["decision_note"],
            "thread_id": str(response["thread_id"]) if response["thread_id"] and not hidden else None,
            "user_id": None if hidden else str(response["user_id"]), "user_name": "Anonymous" if hidden else response["user_name"],
        }

    def _pickers(self, guild: discord.Guild, form: dict[str, Any], member: discord.Member | None) -> dict[str, list[dict[str, str]]]:
        """The roles and channels a role or channel question can be answered with."""
        kinds = {item["kind"] for item in form["items"]}
        found: dict[str, list[dict[str, str]]] = {}
        if "role" in kinds:
            found["roles"] = [{"id": str(role.id), "name": role.name} for role in sorted(guild.roles, key=lambda r: -r.position) if not role.is_default()][:PICKER_ENTRIES]
        if "channel" in kinds:
            visible = [channel for channel in guild.text_channels if member is None or channel.permissions_for(member).view_channel]
            found["channels"] = [{"id": str(channel.id), "name": channel.name} for channel in visible][:PICKER_ENTRIES]
        return found

    # ---------- lists and one form ----------

    async def h_forms(self, request: web.Request) -> web.Response:
        """A server's forms. Staff get all of them, with counts and what a new
        form can start from; anyone else gets the open ones they could answer."""
        guild = self._guild(request)
        user_id = self._user_id(request, request.query.get("user_id", ""))
        service = self.forms_service
        if self._staff(guild, user_id):
            service.appeal_form(guild.id)  # the built-in one is always there to be found
            listed = [service.with_appeal_settings(form, self._settings(guild)) for form in forms.list_forms(self.storage, guild.id)]
            return web.json_response({
                "staff": True, "can_manage": self._staff(guild, user_id, "manage"), "forms": [self._summary_line(form) for form in listed],
                "templates": [{"key": key, "label": template["label"], "description": template["description"],
                               "questions": len(forms.questions(forms.clean_items(template["items"])))} for key, template in forms.TEMPLATES.items()],
                "limits": {"forms": forms.MAX_FORMS_PER_GUILD, "questions": forms.MAX_QUESTIONS, "responses": forms.MAX_RESPONSES_PER_FORM},
            })
        member = guild.get_member(user_id)
        if member is None:
            raise self.Error(404, NOT_AVAILABLE)
        open_ones = [form for form in forms.list_forms(self.storage, guild.id)
                     if form["kind"] == forms.GENERAL and forms.is_open(form, form["responses"])[0]]
        return web.json_response({"staff": False, "can_manage": False, "forms": [
            {"id": form["id"], "slug": form["slug"], "title": form["title"], "description": form["description"], "link": service.link(form)}
            for form in open_ones]})

    def _settings(self, guild: discord.Guild) -> dict[str, Any]:
        from utils.settings import get_guild_settings

        return get_guild_settings(self.storage, guild.id)

    async def h_form(self, request: web.Request) -> web.Response:
        """One form. With staff=1, for the people who run it: everything about
        it. Otherwise for someone who wants to answer: what it asks, whether
        they may answer right now, and what they sent before."""
        guild = self._guild(request)
        user_id = self._user_id(request, request.query.get("user_id", ""))
        form = self._form(guild, request.match_info["form"])
        service = self.forms_service
        if request.query.get("staff") == "1":
            self._site_access(guild, "forms", "view", user_id)
            return web.json_response({"form": {**self._summary_line(form), "items": form["items"], "options": form["options"]},
                                      "can_manage": self._staff(guild, user_id, "manage"), "responses": forms.count_responses(self.storage, form["id"]),
                                      "kinds": list(forms.QUESTION_KINDS), "action_moments": list(forms.ACTION_MOMENTS), "action_kinds": list(forms.ACTION_KINDS)})
        preview = request.query.get("preview") == "1"
        if preview:
            self._site_access(guild, "forms", "view", user_id)
            standing = {"available": True, "can_respond": True, "reason": "", "mine": []}
        else:
            try:
                standing = await service.standing(guild, form, user_id)
            except FormError as exc:
                raise self.Error(409, str(exc)) from exc
            if not standing["available"]:
                raise self.Error(404, NOT_AVAILABLE)
        return web.json_response({
            "guild": {"id": str(guild.id), "name": guild.name, "icon": str(guild.icon.url) if guild.icon else None},
            "form": self._for_answering(form), "preview": preview, "can_respond": standing["can_respond"], "reason": standing["reason"],
            "mine": [self._own(response) for response in standing["mine"][:10]],
            "pickers": self._pickers(guild, form, guild.get_member(user_id)),
        })

    async def h_form_lookup(self, request: web.Request) -> web.Response:
        """Members matching what was typed, for a form's "pick a member" question."""
        guild = self._guild(request)
        user_id = self._user_id(request, request.query.get("user_id", ""))
        form = self._form(guild, request.match_info["form"])
        if not any(item["kind"] == "member" for item in form["items"]):
            raise self.Error(404, NOT_AVAILABLE)
        if not self._staff(guild, user_id):
            allowed, _ = await self.forms_service.eligible(guild, form, user_id)
            if not allowed or guild.get_member(user_id) is None:
                raise self.Error(404, NOT_AVAILABLE)
        query = str(request.query.get("q", "")).strip().lower()[:40]
        found = []
        if len(query) >= 2:
            for member in guild.members:
                if query in member.display_name.lower() or query in str(member).lower() or query == str(member.id):
                    found.append({"id": str(member.id), "name": str(member), "display_name": member.display_name})
                    if len(found) >= LOOKUP_RESULTS:
                        break
        return web.json_response({"members": found})

    # ---------- building ----------

    async def h_forms_action(self, request: web.Request) -> web.Response:
        """create, update, delete a form, or post its button in a channel. For whoever manages the Forms area."""
        guild = self._guild(request)
        body = await self._json(request)
        member = self._site_access(guild, "forms", "manage", self._user_id(request, str(body.get("user_id", ""))))
        action, service = str(body.get("action", "")), self.forms_service
        try:
            if action == "create":
                made = forms.create_form(self.storage, guild.id, member.id, title=str(body.get("title") or ""), template=str(body.get("template") or "blank"))
                return web.json_response({"ok": True, "detail": f"Created the form {made['title']}", "form": self._summary_line(made)})
            form = self._form(guild, str(body.get("form_id", "")))
            if action == "delete":
                forms.delete_form(self.storage, guild.id, form["id"])
                return web.json_response({"ok": True, "detail": f"Deleted the form {form['title']} and its responses", "before": self._summary_line(form)})
            if action == "update":
                options = body.get("options")
                if options is not None:
                    cleaned = forms.clean_options(options, form["options"])
                    problem = service.action_problem(guild, cleaned, member)
                    if problem:
                        raise FormError(problem)
                    if form["kind"] == forms.APPEAL:
                        cleaned["actions"] = [action_ for action_ in cleaned["actions"] if action_["do"] != "thread"]
                    options = cleaned
                status = body.get("status") if form["kind"] == forms.GENERAL else None  # a built-in form is switched by its module
                changed = forms.update_form(self.storage, guild.id, form["id"], title=body.get("title") if form["kind"] == forms.GENERAL else None,
                                            description=body.get("description") if form["kind"] == forms.GENERAL else None,
                                            items=body.get("items"), options=options, status=status)
                return web.json_response({"ok": True, "detail": f"Saved the form {changed['title']}", "form": self._summary_line(changed),
                                          "before": {"title": form["title"], "status": form["status"], "questions": len(forms.questions(form["items"]))}})
            if action == "post":
                return await self._post_form_button(guild, member, form, str(body.get("channel_id", "")))
        except FormError as exc:
            raise self.Error(400, str(exc)) from exc
        raise self.Error(400, "Unknown action.")

    async def _post_form_button(self, guild: discord.Guild, member: discord.Member, form: dict[str, Any], channel_raw: str) -> web.Response:
        channel = guild.get_channel(int(channel_raw)) if is_digits(channel_raw) else None
        if channel is None or not hasattr(channel, "send"):
            raise self.Error(400, "Choose a text channel of this server.")
        if not channel.permissions_for(member).send_messages:
            raise self.Error(403, f"You can't post in #{channel.name} yourself.")
        mine = channel.permissions_for(guild.me)
        if not (mine.view_channel and mine.send_messages and mine.embed_links):
            raise self.Error(409, f"I can't post in #{channel.name}: I need View Channel, Send Messages and Embed Links there.")
        if form["status"] != forms.OPEN:
            raise self.Error(409, "Open the form before posting its button.")
        from cogs.forms import form_message  # the cog's own button, so presses are handled the same way

        embed, view = form_message(self.bot, form)
        try:
            message = await channel.send(embed=embed, view=view)
        except discord.HTTPException as exc:
            raise self.Error(502, f"Discord refused the message: {exc}") from exc
        return web.json_response({"ok": True, "detail": f"Posted the form's button in #{channel.name}", "url": message.jump_url})

    # ---------- answering ----------

    async def h_form_respond(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        body = await self._json(request)
        user_id = self._user_id(request, str(body.get("user_id", "")))
        form = self._form(guild, request.match_info["form"])
        test = bool(body.get("test"))
        if test:
            self._site_access(guild, "forms", "view", user_id)
        raw_edit = str(body.get("response_id") or "")
        try:
            result = await self.forms_service.respond(guild, form, user_id, str(body.get("user_name", "")), body.get("answers"), test=test,
                                                      response_id=int(raw_edit) if is_digits(raw_edit) else None)
        except FormError as exc:
            raise self.Error(404 if str(exc) == NOT_AVAILABLE else 409, str(exc)) from exc
        channel = guild.get_channel(int(form["options"]["channel_id"])) if is_digits(str(form["options"]["channel_id"])) else None
        return web.json_response({"ok": True, **result, "review": form["options"]["review"], "channel": getattr(channel, "name", None)})

    async def h_form_withdraw(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        body = await self._json(request)
        user_id = self._user_id(request, str(body.get("user_id", "")))
        form = self._form(guild, request.match_info["form"])
        raw = str(body.get("response_id", ""))
        gone = forms.withdraw(self.storage, form["id"], int(raw), user_id) if is_digits(raw) and form["kind"] == forms.GENERAL else None
        if gone is None:
            raise self.Error(409, "That response can't be withdrawn any more.")
        return web.json_response({"ok": True, "detail": "Your response was removed."})

    # ---------- reading and deciding ----------

    async def h_form_responses(self, request: web.Request) -> web.Response:
        """A form's responses for its staff: a page of them, all of them (for
        the spreadsheet), and what they add up to."""
        guild = self._guild(request)
        user_id = self._user_id(request, request.query.get("user_id", ""))
        form = self._form(guild, request.match_info["form"])
        self._site_access(guild, ("forms", "moderation") if form["kind"] == forms.APPEAL else "forms", "view", user_id)
        everything = request.query.get("all") == "1"
        page = int(request.query.get("page", "1")) if is_digits(request.query.get("page", "1")) else 1
        status = str(request.query.get("status", ""))
        found, total = forms.list_responses(
            self.storage, form["id"], status=status, limit=forms.MAX_RESPONSES_PER_FORM if everything else RESPONSES_PER_PAGE,
            offset=0 if everything else (max(1, page) - 1) * RESPONSES_PER_PAGE, include_tests=not everything,
        )
        return web.json_response({
            "form": {**self._summary_line(form), "items": form["items"], "options": form["options"]},
            "can_manage": self._staff(guild, user_id, "manage") or (form["kind"] == forms.APPEAL and self._member_access(guild, user_id)["access"]["moderation"]["manage"]),
            "responses": [self._for_staff(form, response) for response in found],
            "total": total, "page": page, "per_page": RESPONSES_PER_PAGE,
            "summary": forms.summary(self.storage, form) if request.query.get("summary") == "1" else None,
        })

    async def h_form_decide(self, request: web.Request) -> web.Response:
        """approve, deny or remove a response. For whoever manages the Forms area."""
        guild = self._guild(request)
        body = await self._json(request)
        form = self._form(guild, request.match_info["form"])
        member = self._site_access(guild, ("forms", "moderation") if form["kind"] == forms.APPEAL else "forms", "manage",
                                   self._user_id(request, str(body.get("user_id", ""))))
        action, raw = str(body.get("action", "")), str(body.get("response_id", ""))
        if not is_digits(raw):
            raise self.Error(400, "A response ID is required.")
        if action == "remove":
            target = forms.get_response(self.storage, guild.id, int(raw))
            if target is None or target["form_id"] != form["id"] or (form["kind"] == forms.APPEAL and not target["test"]):
                raise self.Error(404, "That response is gone, or is not one that can be removed.")
            forms.remove_response(self.storage, guild.id, int(raw))
            return web.json_response({"ok": True, "detail": "Removed the test response" if target["test"] else "Removed the response",
                                      "before": self._for_staff(form, target)})
        if action not in ("approve", "deny"):
            raise self.Error(400, "Unknown action.")
        try:
            result = await self.forms_service.decide(guild, form, int(raw), action == "approve", member, str(body.get("note", "")))
        except FormError as exc:
            raise self.Error(409, str(exc)) from exc
        if form["kind"] == forms.APPEAL:
            detail = "Approved the appeal and unbanned the user" if action == "approve" else "Denied the appeal"
        else:
            detail = ("Approved the response" if action == "approve" else "Denied the response") + (", and told them" if result["told"] else "")
        if result["failed"]:
            detail += f". {len(result['failed'])} of the form's actions could not be done ({'; '.join(result['failed'])[:200]})"
        return web.json_response({"ok": True, "detail": detail, "before": self._for_staff(form, result["response"])})
