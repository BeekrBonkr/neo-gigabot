"""Delivers owner newsletters as DMs, slowly and politely.

Bulk DMs are exactly what Discord's anti-spam watches for, so this is
deliberately conservative: one message every few seconds, a long pause when
Discord says DMs are being opened too fast, an unsubscribe button on every
message, and never more than one send running at a time.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

import discord

from utils import newsletter as store

log = logging.getLogger(__name__)

SECONDS_BETWEEN_DMS = 3.0
OPENING_DMS_TOO_FAST = 40003  # Discord JSON error code
TOO_FAST_PAUSE_SECONDS = 600
TOO_FAST_MAX_PAUSES = 3
UNSUBSCRIBE_ID = "newsletter:unsubscribe"
RESUBSCRIBE_ID = "newsletter:resubscribe"


class NewsletterView(discord.ui.View):
    """Persistent (survives restarts) so the button on an old DM keeps working."""

    def __init__(self, storage_path: Path) -> None:
        super().__init__(timeout=None)
        self.storage_path = storage_path

    @discord.ui.button(label="Stop these messages", style=discord.ButtonStyle.secondary, custom_id=UNSUBSCRIBE_ID)
    async def unsubscribe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store.opt_out(self.storage_path, interaction.user.id)
        await interaction.response.send_message(
            "Done. You won't get announcements from this bot any more.", view=ResubscribeView(self.storage_path)
        )


class ResubscribeView(discord.ui.View):
    def __init__(self, storage_path: Path) -> None:
        super().__init__(timeout=None)
        self.storage_path = storage_path

    @discord.ui.button(label="Undo: keep me subscribed", style=discord.ButtonStyle.secondary, custom_id=RESUBSCRIBE_ID)
    async def resubscribe(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        store.opt_in(self.storage_path, interaction.user.id)
        await interaction.response.send_message("You're subscribed again.")


def collect_recipients(guilds: list[Any], audience: str, opted_out: set[int]) -> dict[str, Any]:
    """Who a newsletter would go to. Each person is counted once however many
    servers they run. Bots never receive one."""
    recipients: set[int] = set()
    excluded_optouts: set[int] = set()
    for guild in guilds:
        candidates: set[int] = set()
        if guild.owner_id:
            candidates.add(int(guild.owner_id))
        if audience == "admins":
            for member in guild.members:
                if not member.bot and member.guild_permissions.administrator:
                    candidates.add(int(member.id))
        # A cached owner who is a bot (bot-owned servers exist) is dropped here.
        owner = guild.get_member(guild.owner_id) if guild.owner_id else None
        if owner is not None and owner.bot:
            candidates.discard(int(owner.id))
        for user_id in candidates:
            (excluded_optouts if user_id in opted_out else recipients).add(user_id)
    return {"user_ids": sorted(recipients), "opted_out": len(excluded_optouts - recipients), "guilds": len(guilds)}


def build_embed(title: str, body: str, bot_name: str, *, test: bool = False) -> discord.Embed:
    embed = discord.Embed(title=title, description=body, color=discord.Color.blurple(), timestamp=discord.utils.utcnow())
    footer = f"You're getting this because you own or administer a server that uses {bot_name}."
    embed.set_footer(text=("TEST: only you received this. " if test else "") + footer)
    return embed


class NewsletterSender:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self._task: asyncio.Task | None = None
        self._send_id: int | None = None
        self._cancel = False
        self.note = ""  # human-readable current activity, e.g. a rate-limit pause

    @property
    def storage(self) -> Path:
        return self.bot.storage_path

    @property
    def running_id(self) -> int | None:
        return self._send_id if self._task is not None and not self._task.done() else None

    def _bot_name(self) -> str:
        return getattr(self.bot.user, "name", None) or "this bot"

    async def _dm(self, user_id: int, embed: discord.Embed) -> None:
        user = self.bot.get_user(user_id) or await self.bot.fetch_user(user_id)
        if user.bot:
            raise ValueError("bot account")
        await user.send(embed=embed, view=NewsletterView(self.storage))

    async def send_test(self, title: str, body: str, user_id: int) -> None:
        await self._dm(user_id, build_embed(title, body, self._bot_name(), test=True))

    def start(self, send_id: int) -> None:
        if self.running_id is not None:
            raise store.NewsletterError("A newsletter is already being sent. Wait for it to finish or cancel it.")
        self._send_id, self._cancel, self.note = send_id, False, ""
        store.set_state(self.storage, send_id, "running")
        self._task = asyncio.create_task(self._run(send_id), name=f"newsletter-{send_id}")

    def cancel(self) -> bool:
        if self.running_id is None:
            return False
        self._cancel = True
        return True

    async def _run(self, send_id: int) -> None:
        send = store.get_send(self.storage, send_id)
        if send is None:
            return
        embed = build_embed(send["title"], send["body"], self._bot_name())
        pauses = 0
        try:
            for user_id in store.pending_user_ids(self.storage, send_id):
                if self._cancel:
                    store.set_state(self.storage, send_id, "cancelled")
                    return
                # Re-read at send time: someone may have unsubscribed since the send began.
                if user_id in store.opted_out_ids(self.storage):
                    store.mark_delivery(self.storage, send_id, user_id, "skipped", "Unsubscribed")
                    continue
                while True:
                    try:
                        await self._dm(user_id, embed)
                        store.mark_delivery(self.storage, send_id, user_id, "sent")
                    except discord.Forbidden:
                        store.mark_delivery(self.storage, send_id, user_id, "failed", "DMs closed or bot blocked")
                    except discord.NotFound:
                        store.mark_delivery(self.storage, send_id, user_id, "failed", "Account not found")
                    except discord.HTTPException as exc:
                        if exc.code == OPENING_DMS_TOO_FAST and pauses < TOO_FAST_MAX_PAUSES:
                            pauses += 1
                            self.note = f"Discord asked to slow down; paused for {TOO_FAST_PAUSE_SECONDS // 60} minutes"
                            log.warning("Newsletter %s: opening DMs too fast, pausing (%s/%s)", send_id, pauses, TOO_FAST_MAX_PAUSES)
                            await asyncio.sleep(TOO_FAST_PAUSE_SECONDS)
                            self.note = ""
                            if self._cancel:
                                break
                            continue  # retry the same recipient
                        if exc.code == OPENING_DMS_TOO_FAST:
                            # Still refused after several long pauses: stop rather than keep pushing.
                            self.note = "Stopped: Discord kept refusing new DMs. Resume later."
                            store.set_state(self.storage, send_id, "interrupted")
                            return
                        store.mark_delivery(self.storage, send_id, user_id, "failed", f"Discord error {exc.status}")
                    except ValueError:
                        store.mark_delivery(self.storage, send_id, user_id, "skipped", "Bot account")
                    break
                await asyncio.sleep(SECONDS_BETWEEN_DMS)
            store.set_state(self.storage, send_id, "cancelled" if self._cancel else "done")
        except asyncio.CancelledError:
            store.set_state(self.storage, send_id, "interrupted")
            raise
        except Exception:  # noqa: BLE001 - leave it resumable rather than half-recorded
            log.exception("Newsletter %s failed unexpectedly", send_id)
            store.set_state(self.storage, send_id, "interrupted")

    async def shutdown(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
