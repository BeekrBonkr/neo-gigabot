"""Localhost-only control API for the owner web panel (neogigabot-panel).

The panel is a separate process; anything that only exists inside the running
bot (the guild cache, voice sessions, gateway latency, the settings cache) is
reached through this small HTTP API instead.

Security posture:
- Disabled unless PANEL_BRIDGE_TOKEN (32+ chars) is set in .env.
- Binds to 127.0.0.1 only - never reachable from another machine.
- Every request must carry `Authorization: Bearer <token>`.

All snowflake IDs are serialized as strings so no consumer can lose precision.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import math
import os
import platform
import random
import re
import time
from collections import deque
from typing import Any

import discord
import psutil
import yaml
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from utils import appeals as appeal_store
from utils import cases as case_store
from utils import newsletter as newsletter_store
from utils import playlists as playlist_store
from utils import reactionroles as reaction_role_store
from utils import rolemenus as role_menu_store
from utils import schedules as schedule_store
from utils import starboard as starboard_store
from utils import stickies as sticky_store
from utils import sticky_roles as sticky_role_store
from utils import tags as tag_store
from utils import tempbans as tempban_store
from utils import warnings as warning_store
from utils.db import connect
from utils.discord_links import parse_message_link
from utils.durations import parse_duration
from utils.newsletter_sender import NewsletterSender, NewsletterView, ResubscribeView, collect_recipients
from utils.website import site_link
from utils.settings import (
    get_mod_log_channel_id,
    SETTING_CHOICES,
    SETTING_COLORS,
    SETTING_URLS,
    SETTING_DURATIONS,
    SETTING_ID_LISTS,
    SETTING_IDS,
    SETTING_INT_RANGES,
    SETTING_TEXT_LIMITS,
    MOD_LOG_EVENT_KINDS,
    WARN_ACTION_CHOICES,
    clear_settings_caches,
    get_default_settings,
    get_guild_settings,
    parse_hex_color,
    save_guild_settings,
)

log = logging.getLogger(__name__)

BRIDGE_HOST = "127.0.0.1"
DEFAULT_BRIDGE_PORT = 8791
MIN_TOKEN_LENGTH = 32
MAX_AVATAR_BYTES = 10 * 1024 * 1024
MAX_QUEUE_ITEMS_REPORTED = 50
MAX_ID_LIST_LENGTH = 100
MAX_TEXT_LENGTH = 1000

ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "watching": discord.ActivityType.watching,
    "listening": discord.ActivityType.listening,
    "competing": discord.ActivityType.competing,
}
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
EXTENSION_PATTERN = re.compile(r"^cogs\.[a-z_]+$")

# Keys whose value is a list of snowflakes (stored as ints) / a single snowflake
# (stored as a string, "" when unset) - mirrors what cogs/settings.py writes.
ID_LIST_KEYS = {
    "bot_channels",
    "ignored_log_channels",
    "ignored_users",
    "welcome_channel_ids",
    "leave_channel_ids",
    "autoroles",
}
ID_SCALAR_KEYS = {
    "logging_channel_id",
    "mod_log_channel_id",
    "starboard_channel_id",
    "music_djadmin_role_id",
}
INT_RANGES = {
    "music_default_volume": (0, 100),
    "warn_threshold": (0, 999),
    "warn_window_days": (0, 9999),
    "starboard_threshold": (1, 999),
    "welcome_delete_after": (0, 86400),
    "music_max_queue_length": (0, 10000),
    "music_max_track_minutes": (0, 100000),
}


class SettingsValidationError(ValueError):
    """Raised with a message that is safe to show to the panel user."""


def validate_settings(data: Any, defaults: dict[str, Any]) -> dict[str, Any]:
    """Check a full settings dict against the default schema and normalize it
    to the exact shapes the bot's own /settings dashboard writes. Unknown keys
    are rejected rather than silently dropped so a typo can't look like a save."""
    if not isinstance(data, dict):
        raise SettingsValidationError("Settings must be an object.")

    unknown = sorted(set(data) - set(defaults))
    if unknown:
        raise SettingsValidationError(f"Unknown setting(s): {', '.join(unknown)}")

    cleaned: dict[str, Any] = {}
    for key, default_value in defaults.items():
        value = data.get(key, default_value)

        if key in ID_LIST_KEYS or key in SETTING_ID_LISTS:
            if not isinstance(value, list) or len(value) > MAX_ID_LIST_LENGTH:
                raise SettingsValidationError(f"{key} must be a list of up to {MAX_ID_LIST_LENGTH} IDs.")
            ids: list[int] = []
            for item in value:
                if not str(item).isdigit():
                    raise SettingsValidationError(f"{key} contains a non-numeric ID.")
                if int(item) not in ids:
                    ids.append(int(item))
            cleaned[key] = ids
        elif key in ID_SCALAR_KEYS or key in SETTING_IDS:
            text = str(value or "").strip()
            if text and not text.isdigit():
                raise SettingsValidationError(f"{key} must be an ID or empty.")
            cleaned[key] = text
        elif key == "blocked_commands":
            if not isinstance(value, list) or len(value) > 500:
                raise SettingsValidationError("blocked_commands must be a list of command names.")
            names = sorted({str(name).strip().lower() for name in value if str(name).strip()})
            if "settings" in names:
                raise SettingsValidationError("/settings can't be blocked.")
            cleaned[key] = names
        elif key == "mod_log_events":
            if not isinstance(value, list) or any(v not in MOD_LOG_EVENT_KINDS for v in value):
                raise SettingsValidationError(f"mod_log_events must be a subset of {', '.join(MOD_LOG_EVENT_KINDS)}.")
            cleaned[key] = [kind for kind in MOD_LOG_EVENT_KINDS if kind in value]
        elif key == "warn_action":
            if value not in WARN_ACTION_CHOICES:
                raise SettingsValidationError(f"warn_action must be one of {', '.join(WARN_ACTION_CHOICES)}.")
            cleaned[key] = value
        elif key in SETTING_CHOICES:
            if value not in SETTING_CHOICES[key]:
                raise SettingsValidationError(f"{key} must be one of {', '.join(SETTING_CHOICES[key])}.")
            cleaned[key] = value
        elif key == "warn_mute_duration" or key in SETTING_DURATIONS:
            if not isinstance(value, str) or parse_duration(value) is None:
                raise SettingsValidationError(f"{key} must be a duration like 1h or 1d12h.")
            cleaned[key] = value.strip()
        elif key == "embed_color" or key in SETTING_COLORS:
            text = str(value or "").strip()
            if text and parse_hex_color(text) is None:
                raise SettingsValidationError(f"{key} must be a hex color like #5865F2, or empty.")
            cleaned[key] = text
        elif key in SETTING_URLS:
            text = str(value or "").strip()
            if text and not (text.startswith("https://") and len(text) <= 500 and " " not in text):
                raise SettingsValidationError(f"{key} must be an https:// link, or empty.")
            cleaned[key] = text
        elif isinstance(default_value, bool):
            if not isinstance(value, bool):
                raise SettingsValidationError(f"{key} must be true or false.")
            cleaned[key] = value
        elif isinstance(default_value, int):
            if isinstance(value, bool) or not isinstance(value, int):
                raise SettingsValidationError(f"{key} must be a whole number.")
            low, high = SETTING_INT_RANGES.get(key) or INT_RANGES.get(key, (0, 1_000_000))
            if not low <= value <= high:
                raise SettingsValidationError(f"{key} must be between {low} and {high}.")
            cleaned[key] = value
        elif isinstance(default_value, str):
            limit = SETTING_TEXT_LIMITS.get(key, MAX_TEXT_LENGTH)
            if not isinstance(value, str) or len(value) > limit:
                raise SettingsValidationError(f"{key} must be text of up to {limit} characters.")
            if key in ("starboard_emoji", "music_dj_role_name") and not value.strip():
                raise SettingsValidationError(f"{key} can't be empty.")
            cleaned[key] = value
        else:
            # A default type this validator doesn't know: only accept the same type.
            if type(value) is not type(default_value):
                raise SettingsValidationError(f"{key} has the wrong type.")
            cleaned[key] = value

    return cleaned


def _iso(moment) -> str | None:
    return moment.isoformat() if moment else None


def _asset_url(asset) -> str | None:
    return str(asset.url) if asset else None


WEB_PLAYER_OFF = "The web player is turned off for this server."
# Live updates: how often a stream looks for changes, how long it may stay
# quiet, how long one connection lasts (the browser reconnects), and caps.
STREAM_TICK_SECONDS = 0.5
STREAM_HEARTBEAT_SECONDS = 15
STREAM_MAX_SECONDS = 600
STREAM_MAX_PER_USER = 4
STREAM_MAX_TOTAL = 300
POPULAR_PLAYLISTS = 12
PLAYLIST_SEARCH_RESULTS = 30


class BridgeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class PanelBridge(commands.Cog):
    """Serves the localhost control API used by the owner web panel."""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self._runner: web.AppRunner | None = None
        self._token = ""
        self._process = psutil.Process()
        self._presence: dict[str, str] | None = None
        # Open live-update streams, per user, so they can be capped and closed on unload.
        self._streams: dict[int, set[asyncio.Task]] = {}
        self.newsletter = NewsletterSender(bot)

    # ---------- lifecycle ----------

    async def cog_load(self) -> None:
        # Registered even when the bridge itself is off, so the unsubscribe
        # button on newsletters already delivered always keeps working.
        self.bot.add_view(NewsletterView(self.storage))
        self.bot.add_view(ResubscribeView(self.storage))
        if newsletter_store.mark_running_as_interrupted(self.storage):
            log.warning("A newsletter send was interrupted by the restart; resume it from the panel")

        token = os.getenv("PANEL_BRIDGE_TOKEN", "").strip()
        if not token:
            log.info("Panel bridge disabled (PANEL_BRIDGE_TOKEN not set)")
            return
        if len(token) < MIN_TOKEN_LENGTH:
            log.warning("Panel bridge disabled: PANEL_BRIDGE_TOKEN must be at least %s characters", MIN_TOKEN_LENGTH)
            return

        try:
            port = int(os.getenv("PANEL_BRIDGE_PORT", str(DEFAULT_BRIDGE_PORT)))
        except ValueError:
            port = DEFAULT_BRIDGE_PORT

        self._token = token
        self._process.cpu_percent(None)  # prime the counter; first reading is meaningless
        runner = web.AppRunner(self.build_app(), access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, BRIDGE_HOST, port).start()
        except OSError as exc:
            # Never let the panel bridge stop the bot from booting.
            log.error("Panel bridge could not bind %s:%s: %s", BRIDGE_HOST, port, exc)
            await runner.cleanup()
            return
        self._runner = runner
        log.info("Panel bridge listening on %s:%s", BRIDGE_HOST, port)

    async def cog_unload(self) -> None:
        for tasks in list(self._streams.values()):
            for task in list(tasks):
                task.cancel()
        await self.newsletter.shutdown()
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    def build_app(self) -> web.Application:
        app = web.Application(middlewares=[self._middleware], client_max_size=MAX_AVATAR_BYTES * 2)
        r = app.router
        r.add_get("/v1/status", self.h_status)
        r.add_get("/v1/guilds", self.h_guilds)
        r.add_get("/v1/guilds/{guild_id}", self.h_guild)
        r.add_get("/v1/guilds/{guild_id}/settings", self.h_settings_get)
        r.add_put("/v1/guilds/{guild_id}/settings", self.h_settings_put)
        r.add_post("/v1/guilds/{guild_id}/leave", self.h_guild_leave)
        r.add_post("/v1/guilds/{guild_id}/invite", self.h_guild_invite)
        r.add_get("/v1/defaults", self.h_defaults_get)
        r.add_put("/v1/defaults", self.h_defaults_put)
        r.add_get("/v1/music", self.h_music)
        r.add_get("/v1/users/{user_id}/guilds", self.h_user_guilds)
        r.add_get("/v1/users/{user_id}/listening", self.h_user_listening)
        r.add_get("/v1/users/{user_id}/playlists", self.h_playlists)
        r.add_get("/v1/users/{user_id}/playlists/discover", self.h_playlists_discover)
        r.add_get("/v1/users/{user_id}/playlists/search", self.h_playlists_search)
        r.add_get("/v1/users/{user_id}/profiles/{owner_id}", self.h_profile)
        r.add_get("/v1/users/{user_id}/playlists/{owner_id}/{name}", self.h_playlist_view)
        r.add_post("/v1/users/{user_id}/playlists/action", self.h_playlist_action)
        r.add_get("/v1/guilds/{guild_id}/members/{user_id}", self.h_member)
        r.add_get("/v1/guilds/{guild_id}/music", self.h_guild_music)
        r.add_get("/v1/guilds/{guild_id}/music/events", self.h_guild_music_events)
        r.add_get("/v1/guilds/{guild_id}/moderation", self.h_moderation)
        r.add_get("/v1/guilds/{guild_id}/membersearch", self.h_member_search)
        r.add_post("/v1/guilds/{guild_id}/tags", self.h_tags_action)
        r.add_post("/v1/guilds/{guild_id}/schedules", self.h_schedules_action)
        r.add_post("/v1/guilds/{guild_id}/stickies", self.h_stickies_action)
        r.add_post("/v1/guilds/{guild_id}/rolemenus", self.h_rolemenus_action)
        r.add_post("/v1/guilds/{guild_id}/moderation/action", self.h_moderation_action)
        r.add_get("/v1/guilds/{guild_id}/appeal", self.h_appeal_form)
        r.add_post("/v1/guilds/{guild_id}/appeal", self.h_appeal_submit)
        r.add_post("/v1/guilds/{guild_id}/music/action", self.h_music_action)
        r.add_get("/v1/commands", self.h_commands)
        r.add_post("/v1/users/resolve", self.h_users_resolve)
        r.add_post("/v1/actions/reload", self.h_reload)
        r.add_post("/v1/actions/sync", self.h_sync)
        r.add_post("/v1/actions/loglevel", self.h_loglevel)
        r.add_post("/v1/actions/presence", self.h_presence)
        r.add_post("/v1/actions/avatar", self.h_avatar)
        r.add_post("/v1/actions/restart", self.h_restart)
        r.add_post("/v1/notify", self.h_notify)
        r.add_post("/v1/data/{kind}/delete", self.h_data_delete)
        r.add_post("/v1/data/playlists/update", self.h_playlist_update)
        r.add_get("/v1/guilds/{guild_id}/reactionroles", self.h_rr_overview)
        r.add_post("/v1/guilds/{guild_id}/reactionroles/add", self.h_rr_add)
        r.add_post("/v1/guilds/{guild_id}/reactionroles/create", self.h_rr_create)
        r.add_post("/v1/guilds/{guild_id}/reactionroles/remove", self.h_rr_remove)
        r.add_get("/v1/newsletter", self.h_newsletter_overview)
        r.add_post("/v1/newsletter/test", self.h_newsletter_test)
        r.add_post("/v1/newsletter/send", self.h_newsletter_send)
        r.add_post("/v1/newsletter/{send_id}/resume", self.h_newsletter_resume)
        r.add_post("/v1/newsletter/cancel", self.h_newsletter_cancel)
        return app

    @web.middleware
    async def _middleware(self, request: web.Request, handler):
        supplied = request.headers.get("Authorization", "")
        expected = f"Bearer {self._token}"
        if not self._token or not hmac.compare_digest(supplied.encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        try:
            return await handler(request)
        except BridgeError as exc:
            return web.json_response({"error": exc.message}, status=exc.status)
        except playlist_store.PlaylistError as exc:
            # The store's messages use `backticks` for Discord; plain quotes read better on a web page.
            return web.json_response({"error": str(exc).replace("`", '"')}, status=400)
        except (SettingsValidationError, newsletter_store.NewsletterError, reaction_role_store.ReactionRoleError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except web.HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001 - report, never crash the bot
            log.exception("Panel bridge request failed: %s %s", request.method, request.path)
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=500)

    # ---------- helpers ----------

    @property
    def storage(self):
        return self.bot.storage_path

    def _guild(self, request: web.Request) -> discord.Guild:
        raw = request.match_info["guild_id"]
        guild = self.bot.get_guild(int(raw)) if raw.isdigit() else None
        if guild is None:
            raise BridgeError(404, "The bot is not in a server with that ID.")
        return guild

    @staticmethod
    async def _json(request: web.Request) -> dict[str, Any]:
        try:
            body = await request.json()
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(400, "Body must be JSON.") from exc
        if not isinstance(body, dict):
            raise BridgeError(400, "Body must be a JSON object.")
        return body

    def _guild_summary(self, guild: discord.Guild) -> dict[str, Any]:
        me = guild.me
        return {
            "id": str(guild.id),
            "name": guild.name,
            "icon": _asset_url(guild.icon),
            "member_count": guild.member_count or 0,
            "owner_id": str(guild.owner_id) if guild.owner_id else None,
            "created_at": _iso(guild.created_at),
            "joined_at": _iso(me.joined_at) if me else None,
            "unavailable": bool(guild.unavailable),
            "voice_active": guild.voice_client is not None,
        }

    def _presence_dict(self) -> dict[str, str] | None:
        if self._presence is not None:
            return self._presence
        activity = self.bot.activity
        if activity is None or not getattr(activity, "name", None):
            return None
        kind = getattr(getattr(activity, "type", None), "name", "playing")
        return {"type": kind, "text": str(activity.name)}

    # ---------- read endpoints ----------

    async def h_status(self, request: web.Request) -> web.Response:
        bot = self.bot
        latency = bot.latency
        latency_ms = None if (latency is None or math.isnan(latency) or math.isinf(latency)) else round(latency * 1000, 1)
        memory = self._process.memory_info()
        user = bot.user
        config = bot.config
        return web.json_response(
            {
                "ready": bot.is_ready(),
                "user": {
                    "id": str(user.id),
                    "name": str(user),
                    "avatar": _asset_url(user.display_avatar),
                }
                if user
                else None,
                "latency_ms": latency_ms,
                "started_at": _iso(bot.start_time),
                "guild_count": len(bot.guilds),
                "member_total": sum(g.member_count or 0 for g in bot.guilds),
                "cached_users": len(bot.users),
                "voice_sessions": len(bot.voice_clients),
                "process": {
                    "pid": self._process.pid,
                    "rss_bytes": memory.rss,
                    "cpu_percent": self._process.cpu_percent(None),
                    "threads": self._process.num_threads(),
                    "started_at": self._process.create_time(),
                },
                "python": platform.python_version(),
                "discord_py": discord.__version__,
                "log_level": logging.getLevelName(logging.getLogger().getEffectiveLevel()),
                "extensions": sorted(bot.extensions.keys()),
                "presence": self._presence_dict(),
                "features": {
                    "weather": bool(getattr(config, "weather_enabled", False)),
                    "giphy": bool(getattr(config, "giphy_enabled", False)),
                },
                "owner_id": str(config.owner_id),
                "invite_url": self._invite_url(),
                "support_invite": getattr(config, "support_server_invite", "") or "",
                "time": time.time(),
            }
        )

    async def h_guilds(self, request: web.Request) -> web.Response:
        return web.json_response({"guilds": [self._guild_summary(g) for g in self.bot.guilds]})

    async def h_guild(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        me = guild.me
        owner = guild.owner
        permissions = me.guild_permissions if me else discord.Permissions.none()
        members = guild.members
        bots = sum(1 for m in members if m.bot)

        channels = []
        for channel in sorted(guild.channels, key=lambda c: (c.position, c.id)):
            channels.append(
                {
                    "id": str(channel.id),
                    "name": channel.name,
                    "type": channel.type.name,
                    "category": channel.category.name if channel.category else None,
                    "position": channel.position,
                    "nsfw": bool(getattr(channel, "nsfw", False)),
                }
            )
        roles = [
            {
                "id": str(role.id),
                "name": role.name,
                "color": f"#{role.color.value:06x}" if role.color.value else None,
                "position": role.position,
                "managed": role.managed,
                "members": len(role.members),
                "elevated": bool(role.permissions.administrator or role.permissions.manage_guild),
            }
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if not role.is_default()
        ]

        detail = self._guild_summary(guild)
        detail.update(
            {
                "owner_name": str(owner) if owner else None,
                "description": guild.description,
                "humans": max(0, len(members) - bots) if members else None,
                "bots": bots if members else None,
                "boost_tier": guild.premium_tier,
                "boosts": guild.premium_subscription_count or 0,
                "locale": str(guild.preferred_locale),
                "features": sorted(guild.features),
                "channels": channels,
                "roles": roles,
                "emoji_count": len(guild.emojis),
                "bot_nick": me.nick if me else None,
                "bot_permissions": {
                    "administrator": permissions.administrator,
                    "manage_guild": permissions.manage_guild,
                    "manage_roles": permissions.manage_roles,
                    "manage_channels": permissions.manage_channels,
                    "kick_members": permissions.kick_members,
                    "ban_members": permissions.ban_members,
                    "moderate_members": permissions.moderate_members,
                    "manage_messages": permissions.manage_messages,
                    "create_instant_invite": permissions.create_instant_invite,
                    "connect": permissions.connect,
                    "speak": permissions.speak,
                },
            }
        )
        return web.json_response(detail)

    async def h_settings_get(self, request: web.Request) -> web.Response:
        raw = request.match_info["guild_id"]
        if not raw.isdigit():
            raise BridgeError(400, "Invalid server ID.")
        return web.json_response({"settings": get_guild_settings(self.storage, int(raw))})

    async def h_settings_put(self, request: web.Request) -> web.Response:
        raw = request.match_info["guild_id"]
        if not raw.isdigit():
            raise BridgeError(400, "Invalid server ID.")
        body = await self._json(request)
        defaults = get_default_settings(self.storage)
        before = get_guild_settings(self.storage, int(raw))
        cleaned = validate_settings(body.get("settings"), defaults)
        # Goes through the bot's own save path so its in-memory cache updates
        # immediately - the change is live for the very next command.
        save_guild_settings(self.storage, int(raw), cleaned)
        log.info("Panel updated settings for guild %s", raw)
        return web.json_response({"before": before, "settings": get_guild_settings(self.storage, int(raw))})

    async def h_defaults_get(self, request: web.Request) -> web.Response:
        return web.json_response({"settings": get_default_settings(self.storage)})

    async def h_defaults_put(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        before = get_default_settings(self.storage)
        cleaned = validate_settings(body.get("settings"), before)
        path = self.storage / "settings" / "default.yml"
        temp = path.with_suffix(".yml.tmp")
        with temp.open("w", encoding="utf-8") as handle:
            yaml.safe_dump(cleaned, handle, sort_keys=False, allow_unicode=True)
        os.replace(temp, path)
        clear_settings_caches()
        log.info("Panel updated the default settings template")
        return web.json_response({"before": before, "settings": get_default_settings(self.storage)})

    def _music_session(self, guild: discord.Guild) -> dict[str, Any] | None:
        """The guild's live voice session as plain data, or None if the bot
        isn't in voice there."""
        voice_client = guild.voice_client
        if voice_client is None:
            return None
        music = self.bot.get_cog("Music")
        states = getattr(music, "guild_states", {}) if music else {}
        state = states.get(guild.id)
        channel = getattr(voice_client, "channel", None)

        def track_dict(track) -> dict[str, Any] | None:
            if track is None:
                return None
            return {
                "title": track.title,
                "url": track.webpage_url,
                "uploader": track.uploader,
                "duration": track.duration,
                "thumbnail": track.thumbnail,
                "requested_by": track.requested_by_name,
            }

        queue = list(state.queue) if state else []
        position = music.playback_position(guild) if music is not None and hasattr(music, "playback_position") else None
        return {
            "position": round(position, 1) if position is not None else None,
            "autoplay": bool(get_guild_settings(self.storage, guild.id).get("music_autoplay", False)),
            "guild_id": str(guild.id),
            "guild_name": guild.name,
            "guild_icon": _asset_url(guild.icon),
            "channel": channel.name if channel else None,
            "listeners": sum(1 for m in channel.members if not m.bot) if channel else 0,
            "playing": bool(voice_client.is_playing()),
            "paused": bool(voice_client.is_paused()),
            "volume": round(state.volume * 100) if state else None,
            "loop": state.loop_mode if state else "off",
            "current": track_dict(state.current) if state else None,
            "queue_length": len(queue),
            "queue": [track_dict(t) for t in queue[:MAX_QUEUE_ITEMS_REPORTED]],
        }

    async def h_music(self, request: web.Request) -> web.Response:
        sessions = []
        for voice_client in self.bot.voice_clients:
            guild = getattr(voice_client, "guild", None)
            session = self._music_session(guild) if guild is not None else None
            if session is not None:
                sessions.append(session)
        return web.json_response({"sessions": sessions})

    # ---------- per-user access (for the public site) ----------

    def _invite_url(self) -> str:
        """Same permission set as the invite link in /about (cogs/info.py)."""
        application_id = getattr(self.bot, "application_id", None)
        if not application_id:
            return ""
        return discord.utils.oauth_url(
            application_id,
            permissions=discord.Permissions(
                manage_roles=True, manage_channels=True, kick_members=True, ban_members=True,
                manage_nicknames=True, moderate_members=True, view_audit_log=True, send_messages=True,
                embed_links=True, attach_files=True, read_message_history=True, add_reactions=True,
                connect=True, speak=True, manage_messages=True, create_instant_invite=True,
            ),
            scopes=("bot", "applications.commands"),
        )

    @staticmethod
    def _user_id(request: web.Request, source: str | None = None) -> int:
        raw = source if source is not None else request.match_info.get("user_id", "")
        if not str(raw).isdigit():
            raise BridgeError(400, "A user ID is required.")
        return int(raw)

    def _member_access(self, guild: discord.Guild, user_id: int) -> dict[str, Any]:
        """What a user is to a guild, from the bot's own member cache. `admin`
        matches the bot's /settings gate (Administrator), plus the guild owner."""
        member = guild.get_member(user_id)
        if member is None:
            return {"member": False, "owner": False, "admin": False}
        is_owner = guild.owner_id == member.id
        return {
            "member": True,
            "owner": is_owner,
            "admin": bool(is_owner or member.guild_permissions.administrator),
            "name": member.display_name,
        }

    def _music_access(self, guild: discord.Guild, user_id: int, command: str | None = None) -> tuple[bool, str]:
        """Whether this user may control playback right now. The same gate as
        the Discord music remote (cogs/music.py check_remote_access): the bot is
        in voice, the user is in that channel, and DJ mode applies. A command
        the server has disabled stays disabled here too."""
        member = guild.get_member(user_id)
        if member is None:
            return False, "You are not a member of this server."
        if not self._web_enabled(guild, "web_player_enabled"):
            return False, WEB_PLAYER_OFF
        voice_client = guild.voice_client
        if voice_client is None or not voice_client.is_connected() or voice_client.channel is None:
            return False, "The bot is not in a voice channel right now."
        if member.voice is None or member.voice.channel != voice_client.channel:
            return False, f"Join {voice_client.channel.name} to control the music."

        refusal = self._command_refusal(guild, member, command)
        return not refusal, refusal

    def _web_enabled(self, guild: discord.Guild, key: str) -> bool:
        """A server's website switches: web_player_enabled, web_join_enabled,
        web_discovery_enabled. All default to on."""
        return bool(get_guild_settings(self.storage, guild.id).get(key, True))

    def _command_refusal(self, guild: discord.Guild, member: discord.Member, command: str | None) -> str:
        """Why this server's settings stop the member from using a music
        command (disabled command, DJ mode), or "" if they don't."""
        settings = get_guild_settings(self.storage, guild.id)
        if command:
            blocked = {str(name).lower() for name in (settings.get("blocked_commands") or [])}
            if command in blocked:
                return f"/{command} is disabled in this server."
        if settings.get("music_dj_enabled"):
            role_name = str(settings.get("music_dj_role_name", "dj"))
            djadmin = str(settings.get("music_djadmin_role_id") or "")
            has_dj = any(role.name == role_name for role in member.roles)
            has_djadmin = djadmin.isdigit() and any(role.id == int(djadmin) for role in member.roles)
            if not (has_dj or has_djadmin):
                return f"DJ mode is on. You need the {role_name} role."
        return ""

    def _join_target(self, guild: discord.Guild, user_id: int) -> tuple[Any, str]:
        """The voice channel this user could bring the bot to right now (what
        /music join would do), or None and why not. The reason is "" when the
        bot is simply already in their channel. The bot is never pulled out of
        a channel where it has music playing, paused or queued."""
        member = guild.get_member(user_id)
        if member is None:
            return None, "You are not a member of this server."
        if not self._web_enabled(guild, "web_player_enabled"):
            return None, WEB_PLAYER_OFF
        if not self._web_enabled(guild, "web_join_enabled"):
            return None, "Bringing the bot into a voice channel from the website is turned off for this server."
        channel = getattr(getattr(member, "voice", None), "channel", None)
        if channel is None:
            return None, "Join a voice channel in Discord first."
        voice_client = guild.voice_client
        if voice_client is not None and voice_client.is_connected() and voice_client.channel is not None:
            if voice_client.channel == channel:
                return None, ""
            music = self.bot.get_cog("Music")
            state = getattr(music, "guild_states", {}).get(guild.id) if music else None
            if voice_client.is_playing() or voice_client.is_paused() or (state and (state.current or state.queue)):
                return None, f"The bot is already playing music in {voice_client.channel.name}."
        refusal = self._command_refusal(guild, member, "music join")
        return (None, refusal) if refusal else (channel, "")

    async def _music_join(self, guild: discord.Guild, user_id: int, music: Any) -> web.Response:
        channel, reason = self._join_target(guild, user_id)
        if channel is None and reason:
            raise BridgeError(403, reason)
        if channel is None:
            detail = f"Already in {guild.voice_client.channel.name}"
        else:
            try:
                voice_client = await music.connect_to_channel(guild, channel)
            except RuntimeError as exc:
                raise BridgeError(409, str(exc)) from exc
            # Nothing is queued yet: leave again if it stays that way.
            if not (voice_client.is_playing() or voice_client.is_paused()):
                await music.schedule_idle_disconnect(guild.id)
            detail = f"Joined {channel.name}"
            log.info("Panel music action join in guild %s by user %s", guild.id, user_id)
        return web.json_response({"ok": True, "detail": detail, "session": self._music_session(guild)})

    async def h_user_guilds(self, request: web.Request) -> web.Response:
        """Every guild the bot shares with this user, with their standing in each."""
        user_id = self._user_id(request)
        guilds = []
        for guild in self.bot.guilds:
            access = self._member_access(guild, user_id)
            if not access["member"]:
                continue
            summary = self._guild_summary(guild)
            session = self._music_session(guild) if self._web_enabled(guild, "web_player_enabled") else None
            summary["voice_active"] = session is not None
            summary.update(
                {
                    "owner": access["owner"],
                    "admin": access["admin"],
                    "now_playing": (session["current"] or {}).get("title") if session else None,
                    "voice_channel": session["channel"] if session else None,
                    # Whether this user could control playback here right now
                    # (in the bot's voice channel, DJ mode allowing).
                    "can_control": self._music_access(guild, user_id)[0] if session else False,
                    # The user's own voice channel here, if the bot could be
                    # brought into it right now.
                    "join_channel": getattr(self._join_target(guild, user_id)[0], "name", None),
                }
            )
            guilds.append(summary)
        guilds.sort(key=lambda g: (not g["voice_active"], g["name"].lower()))
        return web.json_response({"guilds": guilds})

    async def h_user_listening(self, request: web.Request) -> web.Response:
        """Where this user is listening right now: the guild whose voice
        channel they share with the bot while a track is loaded (paused
        counts), or None. Only walks the live voice sessions, so the site can
        ask on every page."""
        user_id = self._user_id(request)
        for voice_client in self.bot.voice_clients:
            guild = getattr(voice_client, "guild", None)
            channel = getattr(voice_client, "channel", None)
            if guild is None or channel is None or not any(getattr(m, "id", None) == user_id for m in channel.members):
                continue
            if not self._web_enabled(guild, "web_player_enabled"):
                continue
            session = self._music_session(guild)
            if session and session["current"]:
                return web.json_response(
                    {
                        "listening": {
                            "guild_id": session["guild_id"],
                            "guild_name": session["guild_name"],
                            "channel": session["channel"],
                            "title": session["current"]["title"],
                            "paused": session["paused"],
                        }
                    }
                )
        return web.json_response({"listening": None})

    async def h_member(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        access = self._member_access(guild, self._user_id(request))
        access["guild"] = self._guild_summary(guild)
        return web.json_response(access)

    def _music_view(self, guild: discord.Guild, user_id: int) -> dict[str, Any]:
        """A member's view of this server's player: what the website shows."""
        if not self._web_enabled(guild, "web_player_enabled"):
            return {"session": None, "can_control": False, "reason": WEB_PLAYER_OFF, "join_channel": None,
                    "queue_limit": self._queue_limit(guild.id), "disabled": True}
        can_control, reason = self._music_access(guild, user_id)
        return {
            "session": self._music_session(guild),
            "can_control": can_control,
            "reason": reason,
            "join_channel": getattr(self._join_target(guild, user_id)[0], "name", None),
            "queue_limit": self._queue_limit(guild.id),
            "disabled": False,
        }

    async def h_guild_music(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        user_id = self._user_id(request, request.query.get("user_id", ""))
        if not self._member_access(guild, user_id)["member"]:
            raise BridgeError(403, "You are not a member of this server.")
        return web.json_response(self._music_view(guild, user_id))

    async def h_guild_music_events(self, request: web.Request) -> web.StreamResponse:
        """The same view as h_guild_music, as a server-sent event stream: a
        message whenever something changes, so the website doesn't have to
        poll. The position isn't part of "changed" (it always moves); a message
        goes out when it jumps (a seek) and with each heartbeat."""
        guild = self._guild(request)
        user_id = self._user_id(request, request.query.get("user_id", ""))
        if not self._member_access(guild, user_id)["member"]:
            raise BridgeError(403, "You are not a member of this server.")
        mine = self._streams.setdefault(user_id, set())
        if len(mine) >= STREAM_MAX_PER_USER or sum(len(tasks) for tasks in self._streams.values()) >= STREAM_MAX_TOTAL:
            raise BridgeError(429, "Too many live connections.")

        response = web.StreamResponse(headers={"Content-Type": "text/event-stream", "Cache-Control": "no-cache"})
        await response.prepare(request)
        task = asyncio.current_task()
        mine.add(task)
        started = time.monotonic()
        last_key, last_sent, expected = None, 0.0, None
        try:
            while time.monotonic() - started < STREAM_MAX_SECONDS:
                guild = self.bot.get_guild(guild.id)
                if guild is None or guild.get_member(user_id) is None:
                    break
                view = self._music_view(guild, user_id)
                session = view["session"] or {}
                position, now = session.get("position"), time.monotonic()
                key = json.dumps({**view, "session": {**session, "position": None} if session else None}, sort_keys=True, default=str)
                jumped = position is not None and expected is not None and abs(position - expected(now)) > 1.5
                if key != last_key or jumped or now - last_sent >= STREAM_HEARTBEAT_SECONDS:
                    await response.write(b"data: " + json.dumps(view, default=str).encode() + b"\n\n")
                    last_key, last_sent = key, now
                    if position is not None:
                        rate = 1.0 if session.get("playing") else 0.0
                        expected = lambda at, p=position, t=now, r=rate: p + (at - t) * r  # noqa: E731
                    else:
                        expected = None
                await asyncio.sleep(STREAM_TICK_SECONDS)
        except (ConnectionError, asyncio.CancelledError):
            pass  # the browser went away, or the bridge is shutting down
        finally:
            mine.discard(task)
            if not mine:
                self._streams.pop(user_id, None)
        return response

    def _queue_limit(self, guild_id: int) -> int:
        music = self.bot.get_cog("Music")
        return music.effective_queue_limit(guild_id) if music else 0

    async def h_music_action(self, request: web.Request) -> web.Response:
        """One playback action on behalf of a website user. Each branch does
        what the matching /music command or remote button does."""
        guild = self._guild(request)
        body = await self._json(request)
        user_id = self._user_id(request, str(body.get("user_id", "")))
        action = str(body.get("action", ""))
        music = self.bot.get_cog("Music")
        if music is None:
            raise BridgeError(503, "Music is not available right now.")
        if action == "join":  # the one action that doesn't need the bot in voice already
            return await self._music_join(guild, user_id, music)

        voice_client = guild.voice_client
        command = {
            "toggle": "music resume" if voice_client is not None and voice_client.is_paused() else "music pause",
            "skip": "music skip", "stop": "music stop", "shuffle": "music shuffle", "loop": "music loop",
            "volume": "music volume", "remove": "music remove", "move": "music move", "play": "music play",
            "playlist": "music playlist play", "seek": "music seek",
        }.get(action)
        if command is None:
            raise BridgeError(400, "Unknown action.")
        allowed, reason = self._music_access(guild, user_id, command)
        if not allowed:
            raise BridgeError(403, reason)

        state = music.get_state(guild.id)
        member = guild.get_member(user_id)
        detail = ""

        def position(name: str) -> int:
            """A 1-based queue position, checked against the track the user
            was looking at so a stale page can't act on the wrong track."""
            raw = str(body.get(name, ""))
            if not raw.isdigit() or not 1 <= int(raw) <= len(state.queue):
                raise BridgeError(409, "The queue changed. Try again.")
            return int(raw)

        if action == "toggle":
            if voice_client.is_paused():
                voice_client.resume()
                detail = "Resumed"
            elif voice_client.is_playing():
                voice_client.pause()
                detail = "Paused"
            else:
                raise BridgeError(409, "Nothing is playing.")
        elif action == "skip":
            if not (voice_client.is_playing() or voice_client.is_paused()):
                raise BridgeError(409, "Nothing is playing.")
            detail = f"Skipped {state.current.title}" if state.current else "Skipped"
            state.skip_requested = True
            voice_client.stop()
        elif action == "stop":
            state.clear_queue()
            music.stop_playback_task(guild.id)
            voice_client.stop()
            detail = "Stopped playback and cleared the queue"
        elif action == "shuffle":
            items = list(state.queue)
            random.shuffle(items)
            state.queue = deque(items)
            detail = "Shuffled the queue"
        elif action == "loop":
            mode = str(body.get("mode", ""))
            if mode not in ("off", "track", "queue"):
                raise BridgeError(400, "Loop mode must be off, track or queue.")
            state.loop_mode = mode
            detail = f"Loop: {mode}"
        elif action == "volume":
            raw = str(body.get("percent", ""))
            if not raw.isdigit() or int(raw) > 100:
                raise BridgeError(400, "Volume must be between 0 and 100.")
            state.volume = int(raw) / 100
            source = getattr(voice_client, "source", None)
            if isinstance(source, discord.PCMVolumeTransformer):
                source.volume = state.volume
            detail = f"Volume {raw}%"
        elif action == "seek":
            raw = str(body.get("seconds", ""))
            if not raw.isdigit():
                raise BridgeError(400, "Seek needs a time in seconds.")
            try:
                track = music.seek(guild, int(raw))
            except RuntimeError as exc:
                raise BridgeError(409, str(exc)) from exc
            detail = f"Jumped to {int(raw) // 60}:{int(raw) % 60:02d} in {track.title}"
        elif action in ("remove", "move"):
            index = position("position")
            items = list(state.queue)
            if str(body.get("expect_title", items[index - 1].title)) != items[index - 1].title:
                raise BridgeError(409, "The queue changed. Try again.")
            track = items.pop(index - 1)
            if action == "move":
                target = str(body.get("to", ""))
                if not target.isdigit() or not 1 <= int(target) <= len(items) + 1:
                    raise BridgeError(409, "The queue changed. Try again.")
                items.insert(int(target) - 1, track)
                detail = f"Moved {track.title} to position {target}"
            else:
                detail = f"Removed {track.title}"
            state.queue = deque(items)
        elif action == "play":
            try:
                track = await music.extract_track(str(body.get("query", "")), requester=member, guild_id=guild.id)
            except RuntimeError as exc:
                raise BridgeError(409, str(exc)) from exc
            # Extraction can take seconds; the user may have left voice meanwhile.
            allowed, reason = self._music_access(guild, user_id, command)
            if not allowed:
                raise BridgeError(403, reason)
            _, added = await music.enqueue_tracks(guild.id, [track])
            if added == 0:
                raise BridgeError(409, f"The queue is full. Maximum size is {music.effective_queue_limit(guild.id)}.")
            await music.start_playback_if_needed(guild)
            detail = f"Queued {track.title}"

        elif action == "playlist":
            from cogs.music import Track  # the cog's own track type, so the player loop treats these like any other

            owner_raw = str(body.get("owner_id") or user_id)
            if not owner_raw.isdigit():
                raise BridgeError(400, "Unknown playlist.")
            # Raises PlaylistError for a missing playlist or someone else's private one.
            playlist = playlist_store.get_visible_playlist(self.storage, int(owner_raw), str(body.get("name", "")), viewer_id=user_id)
            if not playlist["songs"]:
                raise BridgeError(409, f"{playlist['display_name']} has no songs.")
            songs = playlist["songs"]
            one = str(body.get("position") or "")
            if one:  # a single song from the playlist rather than all of it
                if not one.isdigit() or not 1 <= int(one) <= len(songs) or str(body.get("expect_title", songs[int(one) - 1].get("title"))) != str(songs[int(one) - 1].get("title")):
                    raise BridgeError(409, "That playlist changed. Reload the page and try again.")
                songs = [songs[int(one) - 1]]
            tracks = [Track.from_storage_dict(song, requester=member) for song in songs]
            _, added = await music.enqueue_tracks(guild.id, tracks)
            if added == 0:
                raise BridgeError(409, f"The queue is full. Maximum size is {music.effective_queue_limit(guild.id)}.")
            await music.start_playback_if_needed(guild)
            detail = f"Queued {tracks[0].title}" if one else f"Queued {added} of {len(tracks)} songs from {playlist['display_name']}"

        log.info("Panel music action %s in guild %s by user %s", action, guild.id, user_id)
        return web.json_response({"ok": True, "detail": detail, "session": self._music_session(guild)})

    @staticmethod
    def _required_permissions(command) -> list[str]:
        """Member permissions a command demands, read from its has_permissions
        check (discord.py keeps them in the check's closure)."""
        required: list[str] = []
        for check in getattr(command, "checks", []):
            if "has_permissions" not in getattr(check, "__qualname__", "") or "bot_has" in check.__qualname__:
                continue
            for cell in check.__closure__ or ():
                if isinstance(cell.cell_contents, dict):
                    required += [name for name, wanted in cell.cell_contents.items() if wanted]
        return [name.replace("_", " ").replace("guild", "server").title() for name in dict.fromkeys(required)]

    def _command_categories(self) -> tuple[list[dict[str, str]], dict[str, str]]:
        """The same grouping /help uses (cogs/help.py CATEGORY_DEFS), so the
        website and the bot never disagree about where a command lives."""
        try:
            from cogs.help import CATEGORY_DEFS
        except Exception:  # noqa: BLE001 - help cog renamed/removed: fall back to one bucket
            return [], {}
        categories = [{"key": key, "label": label} for key, _emoji, label, _names in CATEGORY_DEFS]
        by_root = {name: key for key, _emoji, _label, names in CATEGORY_DEFS for name in names}
        return categories, by_root

    async def h_commands(self, request: web.Request) -> web.Response:
        categories, by_root = self._command_categories()
        result = []
        for command in self.bot.tree.walk_commands():
            if not isinstance(command, app_commands.Command):
                continue
            root = command.root_parent.name if command.root_parent else command.name
            result.append(
                {
                    "name": command.qualified_name,
                    "description": command.description,
                    "group": command.root_parent.name if command.root_parent else None,
                    "category": by_root.get(root, "owner" if root == "owner" else "other"),
                    "kind": "slash",
                    "requires": self._required_permissions(command),
                    "params": [
                        {"name": p.display_name, "description": p.description if p.description != "…" else "", "required": p.required}
                        for p in getattr(command, "parameters", [])
                    ],
                }
            )
        # Right-click ("Apps") commands live outside the slash tree.
        for kind in (discord.AppCommandType.message, discord.AppCommandType.user):
            for menu in self.bot.tree.get_commands(type=kind):
                result.append(
                    {"name": menu.name, "description": f"Right-click a {kind.name}, then Apps.", "group": None,
                     "category": "images" if "Image" in menu.name else "other", "kind": "menu", "requires": [], "params": []}
                )
        result.sort(key=lambda c: (c["kind"] != "slash", c["name"]))
        return web.json_response({"commands": result, "categories": categories})

    async def h_users_resolve(self, request: web.Request) -> web.Response:
        """Names for user IDs out of the bot's cache (no API calls)."""
        body = await self._json(request)
        users: dict[str, dict[str, Any]] = {}
        for raw in list(body.get("ids") or [])[:500]:
            if not str(raw).isdigit():
                continue
            user = self.bot.get_user(int(raw))
            if user is not None:
                users[str(raw)] = {"name": str(user), "avatar": _asset_url(user.display_avatar), "bot": user.bot}
        return web.json_response({"users": users})

    # ---------- guild actions ----------

    async def h_guild_leave(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        name = guild.name
        await guild.leave()
        log.info("Panel made the bot leave guild %s (%s)", name, guild.id)
        return web.json_response({"ok": True, "name": name})

    async def h_guild_invite(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        channel = next(
            (c for c in guild.text_channels if c.permissions_for(guild.me).create_instant_invite),
            None,
        )
        if channel is None:
            raise BridgeError(409, "No permission to create an invite in any channel there.")
        try:
            invite = await channel.create_invite(max_age=3600, max_uses=1, reason="Owner requested via web panel")
        except discord.HTTPException as exc:
            raise BridgeError(409, f"Discord refused to create an invite: {exc}") from exc
        return web.json_response({"url": invite.url, "channel": channel.name})

    # ---------- bot operations ----------

    async def h_reload(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        extension = str(body.get("extension", ""))
        if not EXTENSION_PATTERN.match(extension) or extension not in self.bot.extensions:
            raise BridgeError(400, "Unknown extension.")
        if extension == __name__:
            # Reloading ourselves tears down the server answering this request;
            # do it after the response has gone out.
            async def _later() -> None:
                await asyncio.sleep(0.3)
                try:
                    await self.bot.reload_extension(extension)
                except Exception:  # noqa: BLE001
                    log.exception("Panel bridge self-reload failed")

            asyncio.create_task(_later())
            return web.json_response({"ok": True, "deferred": True})
        try:
            await self.bot.reload_extension(extension)
        except commands.ExtensionError as exc:
            raise BridgeError(500, f"Reload failed: {exc}") from exc
        log.info("Panel reloaded extension %s", extension)
        return web.json_response({"ok": True})

    async def h_sync(self, request: web.Request) -> web.Response:
        try:
            count = await self.bot.sync_commands_if_changed(force=True)
        except discord.HTTPException as exc:
            raise BridgeError(502, f"Discord rejected the sync: {exc}") from exc
        return web.json_response({"ok": True, "count": count})

    async def h_loglevel(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        level = str(body.get("level", "")).upper()
        if level not in LOG_LEVELS:
            raise BridgeError(400, "Unknown log level.")
        root = logging.getLogger()
        previous = logging.getLevelName(root.getEffectiveLevel())
        root.setLevel(level)
        return web.json_response({"ok": True, "previous": previous, "level": level})

    async def h_presence(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        text = str(body.get("text", "")).strip()
        kind = str(body.get("type", "watching")).lower()
        if not text:
            await self.bot.change_presence(activity=None)
            self._presence = None
            return web.json_response({"ok": True, "presence": None})
        if len(text) > 128 or kind not in ACTIVITY_TYPES:
            raise BridgeError(400, "Status text must be 1-128 characters with a valid activity type.")
        await self.bot.change_presence(activity=discord.Activity(type=ACTIVITY_TYPES[kind], name=text))
        self._presence = {"type": kind, "text": text}
        return web.json_response({"ok": True, "presence": self._presence})

    async def h_avatar(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        try:
            image = base64.b64decode(str(body.get("image_base64", "")), validate=True)
        except Exception as exc:  # noqa: BLE001
            raise BridgeError(400, "image_base64 is not valid base64.") from exc
        if not image or len(image) > MAX_AVATAR_BYTES:
            raise BridgeError(400, "Avatar must be a non-empty image under 10 MB.")
        try:
            await self.bot.user.edit(avatar=image)
        except (discord.HTTPException, ValueError) as exc:
            raise BridgeError(409, f"Discord rejected that image: {exc}") from exc
        return web.json_response({"ok": True, "avatar": _asset_url(self.bot.user.display_avatar)})

    async def h_restart(self, request: web.Request) -> web.Response:
        log.info("Restart requested by owner via web panel")

        async def _shutdown() -> None:
            await asyncio.sleep(0.5)  # let the HTTP response go out first
            try:
                await self.bot.close()
            finally:
                # Same as /owner restart: the unit uses Restart=on-failure, so
                # exit non-zero to make systemd bring the bot back.
                os._exit(1)

        asyncio.create_task(_shutdown())
        return web.json_response({"ok": True})

    async def h_notify(self, request: web.Request) -> web.Response:
        """DM the owner an alert embed on the panel's behalf."""
        body = await self._json(request)
        title = str(body.get("title", "Panel alert"))[:200]
        message = str(body.get("message", ""))[:1800]
        level = str(body.get("level", "info"))
        color = {
            "critical": discord.Color.red(),
            "warning": discord.Color.orange(),
            "good": discord.Color.green(),
        }.get(level, discord.Color.blurple())
        embed = discord.Embed(title=title, description=message, color=color, timestamp=discord.utils.utcnow())
        embed.set_footer(text="neo-gigabot panel")
        try:
            owner = self.bot.get_user(self.bot.config.owner_id) or await self.bot.fetch_user(self.bot.config.owner_id)
            await owner.send(embed=embed)
        except discord.Forbidden as exc:
            raise BridgeError(409, _dm_refused(exc)) from exc
        except discord.HTTPException as exc:
            raise BridgeError(502, f"Could not DM the owner: {exc}") from exc
        return web.json_response({"ok": True})

    # ---------- stored-data edits ----------

    async def h_data_delete(self, request: web.Request) -> web.Response:
        """Delete one stored record. Routed through the bot's own storage
        helpers so their in-memory caches stay correct."""
        kind = request.match_info["kind"]
        body = await self._json(request)

        def need_int(name: str) -> int:
            value = str(body.get(name, ""))
            if not value.isdigit():
                raise BridgeError(400, f"{name} is required.")
            return int(value)

        before: Any
        if kind == "warnings":
            before = warning_store.remove_warning(self.storage, need_int("guild_id"), need_int("id"))
        elif kind == "tempbans":
            guild_id, user_id = need_int("guild_id"), need_int("user_id")
            before = tempban_store.get_tempban(self.storage, guild_id, user_id)
            if before is not None:
                if body.get("unban"):
                    guild = self.bot.get_guild(guild_id)
                    if guild is None:
                        raise BridgeError(409, "The bot is no longer in that server, so it can't unban.")
                    try:
                        await guild.unban(discord.Object(id=user_id), reason="Tempban lifted via web panel")
                    except discord.NotFound:
                        pass  # already unbanned by hand
                    except discord.HTTPException as exc:
                        raise BridgeError(409, f"Discord refused the unban: {exc}") from exc
                tempban_store.remove_tempban(self.storage, guild_id, user_id)
        elif kind == "reaction_roles":
            message_id, emoji = need_int("message_id"), str(body.get("emoji", ""))
            rows = reaction_role_store.list_for_message(self.storage, message_id)
            before = next((row for row in rows if row.get("emoji") == emoji), None)
            if before is not None:
                reaction_role_store.remove_mapping(self.storage, message_id, emoji)
        elif kind == "starboard":
            guild_id, message_id = need_int("guild_id"), need_int("message_id")
            before = starboard_store.get_starboard_post(self.storage, guild_id, message_id)
            if before is not None:
                starboard_store.delete_starboard_post(self.storage, guild_id, message_id)
        elif kind == "sticky_roles":
            guild_id, user_id = need_int("guild_id"), need_int("user_id")
            sticky_role_store.ensure_sticky_roles_table(self.storage)
            with connect(self.storage) as connection:
                row = connection.execute(
                    "SELECT guild_id, user_id, role_ids FROM sticky_roles WHERE guild_id = ? AND user_id = ?",
                    (guild_id, user_id),
                ).fetchone()
                before = dict(row) if row else None
                connection.execute(
                    "DELETE FROM sticky_roles WHERE guild_id = ? AND user_id = ?", (guild_id, user_id)
                )
                connection.commit()
        elif kind == "playlists":
            try:
                before = playlist_store.delete_playlist(self.storage, need_int("owner_id"), str(body.get("name", "")))
            except playlist_store.PlaylistError as exc:
                raise BridgeError(404, str(exc)) from exc
        else:
            raise BridgeError(404, "Unknown data kind.")

        if before is None:
            raise BridgeError(404, "That record no longer exists.")
        log.info("Panel deleted a %s record", kind)
        return web.json_response({"ok": True, "before": _stringify_ids(before)})

    async def h_playlist_update(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        owner_raw, name = str(body.get("owner_id", "")), str(body.get("name", ""))
        if not owner_raw.isdigit():
            raise BridgeError(400, "owner_id is required.")
        owner_id = int(owner_raw)
        try:
            if "status" in body:
                playlist_store.set_status(self.storage, owner_id, name, str(body["status"]))
            if "description" in body:
                playlist_store.set_description(self.storage, owner_id, name, str(body["description"]))
            if body.get("new_name"):
                playlist_store.rename_playlist(self.storage, owner_id, name, str(body["new_name"]))
        except playlist_store.PlaylistError as exc:
            raise BridgeError(400, str(exc)) from exc
        return web.json_response({"ok": True})


    # ---------- personal playlists (any signed-in user, from the website) ----------

    def _playlist_view(self, playlist: dict[str, Any], viewer_id: int, *, with_songs: bool = True) -> dict[str, Any]:
        owner = self.bot.get_user(playlist["owner_id"])
        view = {
            "owner_id": str(playlist["owner_id"]),
            "owner_name": str(owner) if owner else None,
            "name": playlist["name"],
            "display_name": playlist["display_name"],
            "description": playlist["description"],
            "status": playlist["status"],
            "likes": len(playlist["likes"]),
            "liked": str(viewer_id) in playlist["likes"],
            "mine": playlist["owner_id"] == viewer_id,
            "song_count": len(playlist["songs"]),
            "duration": sum(int(song.get("duration") or 0) for song in playlist["songs"]),
        }
        if with_songs:
            view["songs"] = [
                {"title": song.get("title"), "url": song.get("webpage_url"), "uploader": song.get("uploader"),
                 "duration": song.get("duration"), "thumbnail": song.get("thumbnail")}
                for song in playlist["songs"]
            ]
        return view

    async def h_playlists(self, request: web.Request) -> web.Response:
        user_id = self._user_id(request)
        own = playlist_store.list_playlists(self.storage, user_id)
        liked = [p for p in playlist_store.list_liked_playlists(self.storage, user_id) if p["owner_id"] != user_id]
        return web.json_response(
            {
                "own": [self._playlist_view(p, user_id, with_songs=False) for p in own],
                "liked": [self._playlist_view(p, user_id, with_songs=False) for p in liked],
                "limits": {
                    "playlists": playlist_store.MAX_PLAYLISTS_PER_USER, "songs": playlist_store.MAX_SONGS_PER_PLAYLIST,
                    "name": playlist_store.MAX_NAME_LENGTH, "description": playlist_store.MAX_DESCRIPTION_LENGTH,
                },
            }
        )

    def _shared_guilds(self, viewer_id: int) -> list[discord.Guild]:
        """Servers this user is in that let their members find each other's
        playlists (web_discovery_enabled)."""
        return [guild for guild in self.bot.guilds
                if guild.get_member(viewer_id) is not None and self._web_enabled(guild, "web_discovery_enabled")]

    def _public_playlists(self, viewer_id: int) -> list[dict[str, Any]]:
        """Every public playlist with songs that isn't the viewer's own, most
        liked first. Public means public: these are the ones anyone with the
        link could already open."""
        found = [p for p in playlist_store.list_public_playlists(self.storage) if p["owner_id"] != viewer_id and p["songs"]]
        found.sort(key=lambda p: (-len(p["likes"]), p["display_name"].lower()))
        return found

    async def h_playlists_search(self, request: web.Request) -> web.Response:
        """Public playlists across the bot whose name, description, owner or
        songs match every word of the query."""
        user_id = self._user_id(request)
        words = [word for word in str(request.query.get("q", "")).lower().split() if word][:8]
        if not words:
            return web.json_response({"playlists": []})
        results = []
        for playlist in self._public_playlists(user_id):
            view = self._playlist_view(playlist, user_id, with_songs=False)
            haystack = " ".join(
                [playlist["display_name"], playlist["description"], view["owner_name"] or ""]
                + [f"{song.get('title') or ''} {song.get('uploader') or ''}" for song in playlist["songs"]]
            ).lower()
            if all(word in haystack for word in words):
                results.append(view)
                if len(results) >= PLAYLIST_SEARCH_RESULTS:
                    break
        return web.json_response({"playlists": results})

    def _profile_view(self, owner_id: int, shared: list[discord.Guild]) -> dict[str, Any]:
        """Who a playlist owner is, as someone who shares `shared` with them
        may see it. Only servers the viewer is in too are named."""
        user = self.bot.get_user(owner_id)
        member = next((m for m in (guild.get_member(owner_id) for guild in shared) if m is not None), None)
        return {
            "user_id": str(owner_id),
            "name": getattr(user, "global_name", None) or getattr(user, "name", None) or (str(user) if user else None)
            or getattr(member, "display_name", None),
            "avatar": _asset_url(getattr(user or member, "display_avatar", None)),
            "servers": [guild.name for guild in shared if guild.get_member(owner_id) is not None],
        }

    async def h_playlists_discover(self, request: web.Request) -> web.Response:
        """Public playlists (with songs in them) by other people who share a
        server with this user, most liked first, with their owners' profiles."""
        user_id = self._user_id(request)
        shared = self._shared_guilds(user_id)
        profiles: dict[int, dict[str, Any] | None] = {}
        found = []
        for playlist in playlist_store.list_public_playlists(self.storage):
            owner_id = playlist["owner_id"]
            if owner_id == user_id or not playlist["songs"]:
                continue
            if owner_id not in profiles:
                profile = self._profile_view(owner_id, shared)
                profiles[owner_id] = profile if profile["servers"] else None
            profile = profiles[owner_id]
            if profile is None:
                continue
            profile["playlist_count"] = profile.get("playlist_count", 0) + 1
            view = self._playlist_view(playlist, user_id, with_songs=False)
            view["owner_name"] = profile["name"] or view["owner_name"]
            found.append(view)
        found.sort(key=lambda p: (-p["likes"], p["display_name"].lower()))
        people = sorted((p for p in profiles.values() if p), key=lambda p: (-p["playlist_count"], (p["name"] or "").lower()))
        popular = [self._playlist_view(p, user_id, with_songs=False) for p in self._public_playlists(user_id) if p["likes"]]
        return web.json_response({"profiles": people, "playlists": found, "popular": popular[:POPULAR_PLAYLISTS]})

    async def h_profile(self, request: web.Request) -> web.Response:
        """One person's profile and public playlists, for themselves and for
        people who share a server with them. Anyone else gets the same 404 as
        for a user who doesn't exist."""
        user_id = self._user_id(request)
        owner_raw = request.match_info["owner_id"]
        if not owner_raw.isdigit():
            raise BridgeError(404, "Profile not found.")
        owner_id = int(owner_raw)
        profile = self._profile_view(owner_id, self._shared_guilds(user_id))
        mine = owner_id == user_id
        if not mine and not profile["servers"]:
            raise BridgeError(404, "Profile not found.")
        playlists = [
            self._playlist_view(p, user_id, with_songs=False)
            for p in playlist_store.list_playlists(self.storage, owner_id)
            if p["status"] == "public" and (mine or p["songs"])
        ]
        profile.update(mine=mine, playlist_count=len(playlists))
        return web.json_response({"profile": profile, "playlists": playlists})

    async def h_playlist_view(self, request: web.Request) -> web.Response:
        """One playlist as this user may see it: their own, or someone's public one."""
        user_id = self._user_id(request)
        owner_raw = request.match_info["owner_id"]
        if not owner_raw.isdigit():
            raise BridgeError(404, "Playlist not found.")
        try:
            playlist = playlist_store.get_visible_playlist(self.storage, int(owner_raw), request.match_info["name"], viewer_id=user_id)
        except playlist_store.PlaylistError:
            # Missing and private look the same from outside.
            raise BridgeError(404, "Playlist not found.") from None
        return web.json_response({"playlist": self._playlist_view(playlist, user_id), "limits": {"songs": playlist_store.MAX_SONGS_PER_PLAYLIST}})

    def _add_many(self, user_id: int, name: str, songs: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
        """add_songs takes a few songs per call (the Discord command's limit);
        feed it a longer list in pieces. Stops when the playlist is full."""
        store, step = playlist_store, playlist_store.MAX_SONGS_ADDED_PER_CALL
        result, total = store.get_playlist(self.storage, user_id, name), 0
        for start in range(0, len(songs), step):
            try:
                result, added = store.add_songs(self.storage, user_id, name, songs[start:start + step])
            except store.PlaylistError:
                if total:
                    break  # full part-way through: keep what went in
                raise
            total += added
            if added < len(songs[start:start + step]):
                break
        return result, total

    async def h_playlist_action(self, request: web.Request) -> web.Response:
        """Every change a user can make to playlists. The owner is always the
        acting user (from the URL the panel builds out of the session), so
        nobody can edit a playlist that isn't theirs; liking is the one action
        that names another owner, and the store refuses private playlists."""
        user_id = self._user_id(request)
        body = await self._json(request)
        action, name = str(body.get("action", "")), str(body.get("name", ""))
        store, path = playlist_store, self.storage

        def position(key: str) -> int:
            raw = str(body.get(key, ""))
            if not raw.isdigit():
                raise BridgeError(400, "Invalid position.")
            return int(raw)

        detail, result = "", None
        if action == "create":
            result = store.create_playlist(path, user_id, name)
            detail = f"Created {result['display_name']}"
        elif action == "rename":
            result = store.rename_playlist(path, user_id, name, str(body.get("new_name", "")))
            detail = f"Renamed to {result['display_name']}"
        elif action == "details":
            store.set_status(path, user_id, name, str(body.get("status", "")))
            result = store.set_description(path, user_id, name, str(body.get("description", "")))
            detail = "Saved"
        elif action == "delete":
            result = store.delete_playlist(path, user_id, name)
            detail = f"Deleted {result['display_name']}"
        elif action == "remove_song":
            result, removed = store.remove_song(path, user_id, name, position("position"))
            detail = f"Removed {removed.get('title', 'the song')}"
        elif action == "move_song":
            result = store.move_song(path, user_id, name, position("position"), position("to"))
            detail = "Moved"
        elif action == "shuffle":
            result = store.shuffle_playlist(path, user_id, name)
            detail = "Shuffled"
        elif action == "add_song":
            music = self.bot.get_cog("Music")
            if music is None:
                raise BridgeError(503, "Music is not available right now.")
            if store.get_playlist(path, user_id, name) is None:
                raise BridgeError(404, "Playlist not found.")  # before spending a search on it
            try:
                track = await music.extract_track(str(body.get("query", "")), requester=self.bot.get_user(user_id), guild_id=None)
            except RuntimeError as exc:
                raise BridgeError(409, str(exc)) from exc
            result, added = store.add_songs(path, user_id, name, [track.to_storage_dict()])
            detail = f"Added {track.title}" if added else "Nothing was added"
        elif action == "import":
            music = self.bot.get_cog("Music")
            if music is None:
                raise BridgeError(503, "Music is not available right now.")
            existing = store.get_playlist(path, user_id, name)
            if existing is None:
                raise BridgeError(404, "Playlist not found.")
            room = store.MAX_SONGS_PER_PLAYLIST - len(existing["songs"])
            if room <= 0:
                raise BridgeError(409, f"That playlist is already full ({store.MAX_SONGS_PER_PLAYLIST} songs max).")
            try:
                songs = await music.extract_playlist_songs(str(body.get("url", "")), room)
            except RuntimeError as exc:
                raise BridgeError(409, str(exc)) from exc
            result, added = self._add_many(user_id, name, songs)
            detail = f"Imported {added} song{'s' if added != 1 else ''}" + ("" if added == len(songs) else f" (the playlist is full at {store.MAX_SONGS_PER_PLAYLIST})")
        elif action in ("save_song", "save_queue"):
            # From the web player: copy what a server is playing into the
            # user's own playlist. Seeing the queue is all it takes.
            guild_raw = str(body.get("guild_id", ""))
            guild = self.bot.get_guild(int(guild_raw)) if guild_raw.isdigit() else None
            if guild is None or guild.get_member(user_id) is None:
                raise BridgeError(404, "Server not found.")
            if not self._web_enabled(guild, "web_player_enabled"):
                raise BridgeError(403, WEB_PLAYER_OFF)
            music = self.bot.get_cog("Music")
            state = getattr(music, "guild_states", {}).get(guild.id) if music else None
            playing = ([state.current] if state and state.current else []) + (list(state.queue) if state else [])
            if action == "save_song":
                raw = str(body.get("position", "0"))  # 0 is the current track, 1.. the queue
                offset = 0 if state and state.current else 1
                index = int(raw) - offset if raw.isdigit() else -1
                if not 0 <= index < len(playing) or str(body.get("expect_title", playing[index].title)) != playing[index].title:
                    raise BridgeError(409, "The queue changed. Try again.")
                result, added = store.add_songs(path, user_id, name, [playing[index].to_storage_dict()])
                detail = f"Saved {playing[index].title} to {result['display_name']}" if added else "Nothing was added"
            else:
                if not playing:
                    raise BridgeError(409, "Nothing is playing or queued.")
                store.create_playlist(path, user_id, name)
                result, added = self._add_many(user_id, name, [track.to_storage_dict() for track in playing])
                detail = f"Saved {added} song{'s' if added != 1 else ''} as {result['display_name']}"
        elif action in ("like", "unlike"):
            owner_raw = str(body.get("owner_id", ""))
            if not owner_raw.isdigit():
                raise BridgeError(400, "Unknown playlist.")
            result, now_liked = store.toggle_like(path, int(owner_raw), name, user_id, desired=action == "like")
            detail = "Liked" if now_liked else "Removed from your liked playlists"
        else:
            raise BridgeError(400, "Unknown action.")
        return web.json_response({"ok": True, "detail": detail, "playlist": self._playlist_view(result, user_id, with_songs=False)})

    # ---------- moderation (server admins, from the website) ----------

    async def h_moderation(self, request: web.Request) -> web.Response:
        """What only Discord knows about a server's moderation state: who is
        timed out right now, and whether the bot could lift it. Warnings and
        temp bans are in the bot's database, which the panel reads itself."""
        guild = self._guild(request)
        self._rr_admin(guild, self._user_id(request, request.query.get("user_id", "")))
        muted = [
            {"user_id": str(member.id), "name": member.display_name, "avatar": _asset_url(getattr(member, "display_avatar", None)),
             "until": _iso(member.timed_out_until)}
            for member in guild.members if member.is_timed_out()
        ]
        muted.sort(key=lambda m: m["until"] or "")
        permissions = guild.me.guild_permissions if guild.me else None
        return web.json_response({
            "muted": muted,
            "bot_can_unmute": bool(permissions and permissions.moderate_members),
            "bot_can_unban": bool(permissions and permissions.ban_members),
        })

    async def h_tags_action(self, request: web.Request) -> web.Response:
        """Create, replace or delete one of a server's tags, as one of its admins."""
        guild = self._guild(request)
        body = await self._json(request)
        admin = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        action, name = str(body.get("action", "")), str(body.get("name", ""))
        if action == "set":
            try:
                saved, created = tag_store.set_tag(self.storage, guild.id, name, str(body.get("content", "")), admin.id)
            except tag_store.TagError as exc:
                raise BridgeError(400, str(exc)) from exc
            return web.json_response({"ok": True, "detail": f"{'Created' if created else 'Updated'} the tag {saved['name']}", "tag": saved["name"]})
        if action == "delete":
            removed = tag_store.delete_tag(self.storage, guild.id, name)
            if removed is None:
                raise BridgeError(404, "That tag no longer exists.")
            return web.json_response({"ok": True, "detail": f"Deleted the tag {removed['name']}", "before": removed})
        raise BridgeError(400, "Unknown action.")

    async def h_schedules_action(self, request: web.Request) -> web.Response:
        """Add or remove one of a server's scheduled messages, as one of its admins."""
        guild = self._guild(request)
        body = await self._json(request)
        admin = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        action = str(body.get("action", ""))
        if action == "add":
            channel_raw = str(body.get("channel_id", ""))
            channel = guild.get_channel(int(channel_raw)) if channel_raw.isdigit() else None
            if channel is None or not hasattr(channel, "send"):
                raise BridgeError(400, "Choose a channel to post in.")
            permissions = channel.permissions_for(guild.me) if guild.me else None
            if permissions is None or not (permissions.view_channel and permissions.send_messages):
                raise BridgeError(409, f"The bot can't send messages in #{channel.name}.")
            run_at = schedule_store.parse_utc(str(body.get("run_at", "")))
            interval = str(body.get("interval_minutes", "0"))
            if run_at is None or not interval.isdigit():
                raise BridgeError(400, "Choose when to post it.")
            try:
                entry = schedule_store.add(self.storage, guild.id, channel.id, str(body.get("content", "")), run_at, int(interval), admin.id)
            except schedule_store.ScheduleError as exc:
                raise BridgeError(400, str(exc)) from exc
            return web.json_response({"ok": True, "detail": f"Scheduled a message in #{channel.name}", "id": entry["id"]})
        if action == "remove":
            raw = str(body.get("id", ""))
            removed = schedule_store.remove(self.storage, guild.id, int(raw)) if raw.isdigit() else None
            if removed is None:
                raise BridgeError(404, "That scheduled message no longer exists.")
            return web.json_response({"ok": True, "detail": "Cancelled the scheduled message", "before": removed})
        raise BridgeError(400, "Unknown action.")

    def _postable_channel(self, guild: discord.Guild, raw: str) -> Any:
        """A channel of this server the bot can post in, or a BridgeError saying why not."""
        channel = guild.get_channel(int(raw)) if str(raw).isdigit() else None
        if channel is None or not hasattr(channel, "send"):
            raise BridgeError(400, "Choose a channel.")
        permissions = channel.permissions_for(guild.me) if guild.me else None
        if permissions is None or not (permissions.view_channel and permissions.send_messages):
            raise BridgeError(409, f"The bot can't send messages in #{channel.name}.")
        return channel

    async def h_stickies_action(self, request: web.Request) -> web.Response:
        """Set, stop, start or remove a channel's sticky message, as a server
        admin. The same steps as the /stick commands, including posting it."""
        guild = self._guild(request)
        body = await self._json(request)
        admin = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        action = str(body.get("action", ""))
        cog = self.bot.get_cog("Stickies")
        if cog is None:
            raise BridgeError(503, "Sticky messages are not available right now.")
        channel_raw = str(body.get("channel_id", ""))
        if action == "set":
            channel = self._postable_channel(guild, channel_raw)
            try:
                sticky = sticky_store.set_sticky(self.storage, guild.id, channel.id, str(body.get("content", "")), admin.id,
                                                 embed=bool(body.get("embed")), slow=bool(body.get("slow")))
            except sticky_store.StickyError as exc:
                raise BridgeError(400, str(exc)) from exc
            await cog.post(channel, sticky)
            return web.json_response({"ok": True, "detail": f"Stuck the message in #{channel.name}"})
        # The rest act on an existing sticky, which must belong to this server.
        existing = sticky_store.get(self.storage, int(channel_raw)) if channel_raw.isdigit() else None
        if existing is None or existing["guild_id"] != guild.id:
            raise BridgeError(404, "That sticky no longer exists.")
        channel = guild.get_channel(existing["channel_id"])
        name = f"#{channel.name}" if channel is not None else "a deleted channel"
        if action in ("stop", "start"):
            sticky = sticky_store.set_active(self.storage, existing["channel_id"], action == "start")
            if action == "start" and channel is not None:
                await cog.post(channel, sticky)
            return web.json_response({"ok": True, "detail": f"{'Restarted' if action == 'start' else 'Stopped'} the sticky in {name}"})
        if action == "remove":
            sticky_store.remove(self.storage, existing["channel_id"])
            cog.pacer.forget(existing["channel_id"])
            if channel is not None and existing.get("last_message_id"):
                try:
                    await channel.get_partial_message(existing["last_message_id"]).delete()
                except discord.HTTPException:
                    pass
            return web.json_response({"ok": True, "detail": f"Removed the sticky in {name}", "before": existing})
        raise BridgeError(400, "Unknown action.")

    async def h_rolemenus_action(self, request: web.Request) -> web.Response:
        """Post or delete a role menu, as a server admin. Creating one goes
        through the same role checks as /rolemenu create."""
        guild = self._guild(request)
        body = await self._json(request)
        admin = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        action = str(body.get("action", ""))
        cog = self.bot.get_cog("RoleMenus")
        if cog is None:
            raise BridgeError(503, "Role menus are not available right now.")
        if action == "create":
            from cogs.rolemenus import build_view  # the cog's own components, so presses are handled the same way

            channel = self._postable_channel(guild, str(body.get("channel_id", "")))
            style, mode = str(body.get("style", "buttons")), str(body.get("mode", "multiple"))
            title = str(body.get("title", "")).strip()[:200] or "Pick your roles"
            description = str(body.get("description", "")).strip()[:1000]
            raw_roles = body.get("role_ids")
            if style not in role_menu_store.STYLES or mode not in role_menu_store.MODES or not isinstance(raw_roles, list):
                raise BridgeError(400, "Choose a style, a mode and the roles to offer.")
            role_ids = list(dict.fromkeys(int(r) for r in raw_roles if str(r).isdigit()))
            try:
                if role_menu_store.count_menus(self.storage, guild.id) >= role_menu_store.MAX_MENUS_PER_GUILD:
                    raise role_menu_store.RoleMenuError(f"This server already has {role_menu_store.MAX_MENUS_PER_GUILD} role menus. Delete one first.")
                roles = cog._check_roles(guild, admin, role_ids)
            except role_menu_store.RoleMenuError as exc:
                raise BridgeError(400, str(exc).replace("`", "")) from exc
            hint = "Press a button to get a role; press it again to remove it." if style == "buttons" else "Open the menu and pick. Unpick a role to remove it."
            if mode == "single":
                hint += " You can have one of these at a time."
            embed = self.bot.embeds.info_embed(f"🎭 {title}", (description + "\n\n" if description else "") + hint)
            try:
                message = await channel.send(embed=embed, view=build_view(roles, style, mode))
            except discord.HTTPException as exc:
                raise BridgeError(409, f"Discord refused the message: {exc}") from exc
            menu_id = role_menu_store.add_menu(self.storage, guild.id, channel.id, message.id, title, style, mode, [r.id for r in roles], admin.id)
            return web.json_response({"ok": True, "detail": f"Posted a role menu with {len(roles)} role{'s' if len(roles) != 1 else ''} in #{channel.name}", "id": menu_id})
        if action == "delete":
            raw = str(body.get("id", ""))
            menu = role_menu_store.delete_menu(self.storage, guild.id, int(raw)) if raw.isdigit() else None
            if menu is None:
                raise BridgeError(404, "That role menu no longer exists.")
            channel = guild.get_channel(menu["channel_id"])
            if channel is not None:
                try:
                    await channel.get_partial_message(menu["message_id"]).delete()
                except discord.HTTPException:
                    pass  # the message is already gone
            return web.json_response({"ok": True, "detail": "Deleted the role menu", "before": menu})
        raise BridgeError(400, "Unknown action.")

    MEMBER_SEARCH_RESULTS = 25

    async def h_member_search(self, request: web.Request) -> web.Response:
        """Members of a server matching a name or ID, for its admins: who they
        are, their roles, and how much moderation history they have. An exact
        ID also finds someone who has left, as far as the bot still knows them."""
        guild = self._guild(request)
        self._rr_admin(guild, self._user_id(request, request.query.get("user_id", "")))
        query = str(request.query.get("q", "")).strip().lower()[:60]
        if not query:
            return web.json_response({"members": [], "more": False})

        def rank(member: discord.Member) -> int | None:
            names = [member.name.lower(), member.display_name.lower(), (getattr(member, "global_name", None) or "").lower()]
            if query == str(member.id) or query in names:
                return 0
            if any(name.startswith(query) for name in names):
                return 1
            return 2 if any(query in name for name in names) else None

        ranked = sorted(((rank(m), m.display_name.lower(), m) for m in guild.members if rank(m) is not None), key=lambda item: item[:2])
        found = [self._member_card(guild, member) for _, _, member in ranked[: self.MEMBER_SEARCH_RESULTS]]
        if not found and query.isdigit():
            user = self.bot.get_user(int(query))
            found = [{"user_id": query, "name": str(user) if user else None, "display_name": str(user) if user else None,
                      "avatar": _asset_url(getattr(user, "display_avatar", None)), "bot": bool(getattr(user, "bot", False)),
                      "in_server": False, "joined_at": None, "created_at": _iso(getattr(user, "created_at", None)), "roles": [],
                      "muted_until": None, **self._history_counts(guild.id, int(query))}]
        return web.json_response({"members": found, "more": len(ranked) > self.MEMBER_SEARCH_RESULTS})

    def _history_counts(self, guild_id: int, user_id: int) -> dict[str, int]:
        return {"warnings": warning_store.count_warnings(self.storage, guild_id, user_id),
                "cases": case_store.count_cases(self.storage, guild_id, user_id)}

    def _member_card(self, guild: discord.Guild, member: discord.Member) -> dict[str, Any]:
        roles = [{"id": str(role.id), "name": role.name, "color": f"#{role.color.value:06x}" if role.color.value else None}
                 for role in sorted(member.roles, key=lambda r: r.position, reverse=True) if not role.is_default()]
        muted = member.timed_out_until if member.is_timed_out() else None
        return {
            "user_id": str(member.id), "name": str(member), "display_name": member.display_name,
            "avatar": _asset_url(getattr(member, "display_avatar", None)), "bot": member.bot, "in_server": True,
            "owner": guild.owner_id == member.id, "admin": bool(guild.owner_id == member.id or member.guild_permissions.administrator),
            "joined_at": _iso(member.joined_at), "created_at": _iso(member.created_at), "roles": roles[:30],
            "muted_until": _iso(muted), **self._history_counts(guild.id, member.id),
        }

    # ---------- ban appeals ----------

    async def _appeal_context(self, request: web.Request, user_id: int) -> tuple[discord.Guild, dict[str, Any]]:
        """The server and its settings, for a user who is banned there. Anyone
        else gets the same 404 as for a server that doesn't exist or doesn't
        take appeals, so the form can't be used to look servers up."""
        raw = request.match_info["guild_id"]
        guild = self.bot.get_guild(int(raw)) if raw.isdigit() else None
        settings = get_guild_settings(self.storage, guild.id) if guild else {}
        if guild is None or not settings.get("appeals_enabled"):
            raise BridgeError(404, "This server isn't taking appeals.")
        try:
            await guild.fetch_ban(discord.Object(id=user_id))
        except discord.NotFound:
            raise BridgeError(404, "This server isn't taking appeals.") from None
        except discord.HTTPException as exc:
            raise BridgeError(409, "The bot can't read this server's ban list right now.") from exc
        return guild, settings

    async def h_appeal_form(self, request: web.Request) -> web.Response:
        user_id = self._user_id(request, request.query.get("user_id", ""))
        guild, settings = await self._appeal_context(request, user_id)
        latest = appeal_store.latest_appeal(self.storage, guild.id, user_id)
        allowed, why = appeal_store.can_appeal(latest)
        return web.json_response({
            "guild": {"id": str(guild.id), "name": guild.name, "icon": _asset_url(guild.icon)},
            "questions": appeal_store.questions_for(settings),
            "can_appeal": allowed, "reason": why,
            "latest": {"status": latest["status"], "created_at": latest["created_at"], "decision_note": latest["decision_note"]} if latest else None,
            "limits": {"answer": appeal_store.MAX_ANSWER_LENGTH},
        })

    async def h_appeal_submit(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        user_id = self._user_id(request, str(body.get("user_id", "")))
        guild, settings = await self._appeal_context(request, user_id)
        answers = body.get("answers")
        if not isinstance(answers, list):
            raise BridgeError(400, "Answers are required.")
        try:
            appeal_id = appeal_store.submit(self.storage, guild.id, user_id, str(body.get("user_name", "")),
                                            appeal_store.questions_for(settings), [str(a) for a in answers])
        except appeal_store.AppealError as exc:
            raise BridgeError(409, str(exc)) from exc
        # Let the moderators know there is something to read.
        channel_id = get_mod_log_channel_id(self.storage, guild.id)
        channel = guild.get_channel(channel_id) if channel_id else None
        if channel is not None:
            where = site_link(self.bot, "Review it on the website", f"/app/{guild.id}/moderation#appeals") or "Review it on the website's Moderation tab"
            try:
                await channel.send(embed=self.bot.embeds.info_embed("📨 New Ban Appeal", f"<@{user_id}> (`{user_id}`) appealed their ban. {where}."))
            except discord.HTTPException:
                pass
        return web.json_response({"ok": True, "appeal_id": appeal_id})

    async def _decide_appeal(self, guild: discord.Guild, admin: discord.Member, approved: bool, appeal_raw: str, note: str, mod_log) -> web.Response:
        if not appeal_raw.isdigit():
            raise BridgeError(400, "An appeal ID is required.")
        appeal_store.ensure_table(self.storage)
        if approved:
            # Unban first: if Discord refuses, the appeal stays open instead of
            # reading "approved" while the user is still banned.
            with connect(self.storage) as connection:
                row = connection.execute("SELECT user_id FROM ban_appeals WHERE id = ? AND guild_id = ? AND status = 'pending'",
                                         (int(appeal_raw), guild.id)).fetchone()
            if row is None:
                raise BridgeError(404, "That appeal was already decided.")
            try:
                await guild.unban(discord.Object(id=int(row["user_id"])), reason=f"Ban appeal approved by {admin} ({admin.id})")
            except discord.NotFound:
                pass  # already unbanned by hand
            except discord.HTTPException as exc:
                raise BridgeError(409, f"Discord refused the unban: {exc}") from exc
            tempban_store.remove_tempban(self.storage, guild.id, int(row["user_id"]))
        decided = appeal_store.decide(self.storage, guild.id, int(appeal_raw), approved, admin.id, note)
        if decided is None:
            raise BridgeError(404, "That appeal was already decided.")
        target = self.bot.get_user(decided["user_id"])
        if target is None:
            try:
                target = await self.bot.fetch_user(decided["user_id"])
            except discord.HTTPException:
                target = None
        await mod_log("ban", "🔓 Ban Appeal Approved" if approved else "📪 Ban Appeal Denied", target)
        if target is not None:
            text = (f"Your ban appeal for **{guild.name}** was approved. You can rejoin the server." if approved
                    else f"Your ban appeal for **{guild.name}** was denied.") + (f"\nNote from the moderators: {decided['decision_note']}" if decided["decision_note"] else "")
            try:
                await target.send(embed=self.bot.embeds.info_embed("Ban Appeal", text))
            except discord.HTTPException:
                pass  # they share no server with the bot, or have DMs closed
        return web.json_response({"ok": True, "detail": "Approved the appeal and unbanned the user" if approved else "Denied the appeal", "before": decided})

    async def h_moderation_action(self, request: web.Request) -> web.Response:
        """unmute (lift a timeout) or clear_warnings (all of one member's), by a
        server admin. Both are reported to the mod log like the commands."""
        guild = self._guild(request)
        body = await self._json(request)
        admin = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        action, target_raw = str(body.get("action", "")), str(body.get("target_id", ""))
        moderation = self.bot.get_cog("Moderation")

        async def mod_log(event: str, title: str, target: Any) -> None:
            if moderation is None or target is None:
                return
            try:
                await moderation._log_mod_action(
                    guild, event=event, title=title, color=self.bot.embeds.success_color,
                    target=target, moderator=admin, reason=str(body.get("note") or "From the web panel"),
                )
            except Exception:  # noqa: BLE001 - the action already happened; a failed log entry mustn't undo that
                log.exception("Could not write a mod-log entry for a panel action")

        if action in ("approve_appeal", "deny_appeal"):
            return await self._decide_appeal(guild, admin, action == "approve_appeal", str(body.get("appeal_id", "")), str(body.get("note", "")), mod_log)
        if action == "remove_note":
            note_raw = str(body.get("note_id", ""))
            removed = case_store.remove_note(self.storage, guild.id, int(note_raw)) if note_raw.isdigit() else None
            if removed is None:
                raise BridgeError(404, "That note no longer exists.")
            return web.json_response({"ok": True, "detail": "Removed the note", "before": removed})
        if not target_raw.isdigit():
            raise BridgeError(400, "A member ID is required.")
        target_id = int(target_raw)
        if action == "add_note":
            try:
                note_id = case_store.add_note(self.storage, guild.id, target_id, admin.id, str(body.get("note", "")))
            except case_store.CaseError as exc:
                raise BridgeError(400, str(exc)) from exc
            return web.json_response({"ok": True, "detail": "Saved the note", "note_id": note_id})
        if action == "unmute":
            member = guild.get_member(target_id)
            if member is None:
                raise BridgeError(404, "That member is no longer in the server.")
            if not member.is_timed_out():
                return web.json_response({"ok": True, "detail": f"{member.display_name} isn't muted"})
            try:
                await member.timeout(None, reason=f"By {admin} ({admin.id}) from the web panel")
            except discord.Forbidden:
                raise BridgeError(409, "The bot doesn't have permission to unmute that member.") from None
            except discord.HTTPException as exc:
                raise BridgeError(409, f"Discord refused the unmute: {exc}") from exc
            await mod_log("mute", "🔊 Member Unmuted", member)
            detail = f"Unmuted {member.display_name}"
        elif action == "clear_warnings":
            removed = warning_store.clear_warnings(self.storage, guild.id, target_id)
            if removed:
                await mod_log("warn", "🧹 Warnings Cleared", guild.get_member(target_id) or self.bot.get_user(target_id))
            detail = f"Cleared {removed} warning{'s' if removed != 1 else ''}"
            return web.json_response({"ok": True, "detail": detail, "removed": removed})
        else:
            raise BridgeError(400, "Unknown action.")
        log.info("Panel moderation action %s in guild %s by %s on %s", action, guild.id, admin.id, target_id)
        return web.json_response({"ok": True, "detail": detail})

    # ---------- reaction roles (server admins, from the website) ----------

    RR_MAX_PAIRS = 10
    RR_BOT_PERMISSIONS = ("view_channel", "send_messages", "embed_links", "add_reactions", "read_message_history")

    def _rr_admin(self, guild: discord.Guild, user_id: int) -> discord.Member:
        """The acting member, who must hold Administrator (or own the server).
        The panel checks this too; it is repeated here so the bot never relies
        on its caller for it."""
        member = guild.get_member(user_id)
        if member is None or not (guild.owner_id == member.id or member.guild_permissions.administrator):
            raise BridgeError(403, "You need the Administrator permission in this server.")
        return member

    @staticmethod
    def _rr_role_problem(guild: discord.Guild, role: discord.Role, member: discord.Member) -> str | None:
        """Why this member may not hand out this role via a reaction, or None.
        Same rules as /reactionrole add (cogs/reactionroles.py _add_one)."""
        if role.is_default():
            return "@everyone can't be a reaction role."
        if role.managed:
            return f"{role.name} is managed by an integration and can't be assigned."
        if not role < guild.me.top_role:
            return f"{role.name} is above the bot's highest role, so the bot can't assign it."
        # Discord's own rule: nobody hands out roles at or above their own rank.
        if member.id != guild.owner_id and role >= member.top_role:
            return f"{role.name} is at or above your own highest role."
        return None

    def _rr_channel(self, guild: discord.Guild, raw: Any) -> discord.abc.GuildChannel:
        channel = (guild.get_channel(int(raw)) or guild.get_thread(int(raw))) if str(raw).isdigit() else None
        if channel is None or not hasattr(channel, "fetch_message"):
            raise BridgeError(404, "That channel isn't in this server.")
        permissions = channel.permissions_for(guild.me)
        missing = [name.replace("_", " ") for name in self.RR_BOT_PERMISSIONS if not getattr(permissions, name)]
        if missing:
            raise BridgeError(409, f"The bot is missing permissions in #{channel.name}: {', '.join(missing)}.")
        return channel

    @staticmethod
    def _rr_emoji_view(text: str) -> dict[str, Any]:
        emoji = discord.PartialEmoji.from_str(text)
        if emoji.id:
            extension = "gif" if emoji.animated else "png"
            return {"text": text, "name": emoji.name, "url": f"https://cdn.discordapp.com/emojis/{emoji.id}.{extension}?size=48"}
        return {"text": text, "name": text, "url": None}

    async def _rr_add_one(self, guild, message, emoji_text: str, role_raw: Any, member) -> str:
        """Map one emoji on `message` to a role. Returns the stored emoji text."""
        role = guild.get_role(int(role_raw)) if str(role_raw).isdigit() else None
        if role is None:
            raise BridgeError(404, "That role no longer exists.")
        problem = self._rr_role_problem(guild, role, member)
        if problem:
            raise BridgeError(403, problem)
        emoji_text = (emoji_text or "").strip()
        if not emoji_text or len(emoji_text) > 80:
            raise BridgeError(400, "Enter an emoji.")
        emoji = discord.PartialEmoji.from_str(emoji_text)
        try:
            await message.add_reaction(emoji)
        except discord.HTTPException as exc:
            raise BridgeError(400, f"\"{emoji_text}\" isn't an emoji the bot can use. Use a standard emoji or one from this server.") from exc
        try:
            reaction_role_store.add_mapping(self.storage, guild.id, message.id, message.channel.id, str(emoji), role.id)
        except reaction_role_store.ReactionRoleError:
            try:
                await message.remove_reaction(emoji, self.bot.user)
            except discord.HTTPException:
                pass
            raise
        return str(emoji)

    async def h_rr_overview(self, request: web.Request) -> web.Response:
        guild = self._guild(request)
        member = self._rr_admin(guild, self._user_id(request, request.query.get("user_id", "")))
        me = guild.me
        groups: dict[int, dict[str, Any]] = {}
        for row in reaction_role_store.list_for_guild(self.storage, guild.id):
            channel = guild.get_channel(row["channel_id"])
            role = guild.get_role(row["role_id"])
            group = groups.setdefault(
                row["message_id"],
                {
                    "message_id": str(row["message_id"]),
                    "channel_id": str(row["channel_id"]),
                    "channel_name": channel.name if channel else None,
                    "url": f"https://discord.com/channels/{guild.id}/{row['channel_id']}/{row['message_id']}",
                    "items": [],
                },
            )
            group["items"].append(
                {
                    "emoji": self._rr_emoji_view(row["emoji"]),
                    "role_id": str(row["role_id"]),
                    "role_name": role.name if role else None,
                    "role_color": f"#{role.color.value:06x}" if role and role.color.value else None,
                }
            )
        roles = [
            {"id": str(role.id), "name": role.name, "color": f"#{role.color.value:06x}" if role.color.value else None}
            for role in sorted(guild.roles, key=lambda r: r.position, reverse=True)
            if self._rr_role_problem(guild, role, member) is None
        ]
        channels = []
        for channel in sorted(guild.text_channels, key=lambda c: (c.position, c.id)):
            permissions = channel.permissions_for(me)
            if all(getattr(permissions, name) for name in self.RR_BOT_PERMISSIONS):
                channels.append({"id": str(channel.id), "name": channel.name, "category": channel.category.name if channel.category else ""})
        used = reaction_role_store.count_for_guild(self.storage, guild.id)
        return web.json_response(
            {
                "messages": list(groups.values()),
                "roles": roles,
                "channels": channels,
                "emojis": [self._rr_emoji_view(str(emoji)) for emoji in guild.emojis if emoji.available][:200],
                "used": used,
                "limit": reaction_role_store.MAX_MAPPINGS_PER_GUILD,
                "max_pairs": self.RR_MAX_PAIRS,
                "bot_can_manage_roles": bool(me.guild_permissions.manage_roles),
            }
        )

    async def h_rr_add(self, request: web.Request) -> web.Response:
        """Add one emoji -> role to a message that already exists."""
        guild = self._guild(request)
        body = await self._json(request)
        member = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        try:
            link_guild, channel_id, message_id = parse_message_link(str(body.get("message_link", "")))
        except ValueError as exc:
            raise BridgeError(400, "That isn't a message link. In Discord, right-click the message and choose Copy Message Link.") from exc
        if link_guild != str(guild.id):
            raise BridgeError(400, "That message is in a different server.")
        channel = self._rr_channel(guild, channel_id)
        try:
            message = await channel.fetch_message(message_id)
        except discord.HTTPException as exc:
            raise BridgeError(404, "The bot couldn't find that message. It may have been deleted.") from exc
        emoji = await self._rr_add_one(guild, message, str(body.get("emoji", "")), body.get("role_id"), member)
        log.info("Panel added reaction role in guild %s by user %s", guild.id, member.id)
        return web.json_response({"ok": True, "emoji": emoji, "message_id": str(message.id)})

    async def h_rr_create(self, request: web.Request) -> web.Response:
        """Post a new embed in a channel and attach emoji -> role pairs to it."""
        guild = self._guild(request)
        body = await self._json(request)
        member = self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        title, description = str(body.get("title", "")).strip(), str(body.get("description", "")).strip()
        if not 1 <= len(title) <= 256 or not 1 <= len(description) <= 2000:
            raise BridgeError(400, "Give the message a title (up to 256 characters) and a description (up to 2000).")
        color = None
        if str(body.get("color") or "").strip():
            value = parse_hex_color(str(body["color"]))
            if value is None:
                raise BridgeError(400, "Use a hex color like #5865F2, or leave it empty.")
            color = discord.Color(value)
        pairs = [p for p in (body.get("pairs") or []) if isinstance(p, dict) and str(p.get("emoji", "")).strip()]
        if not pairs or len(pairs) > self.RR_MAX_PAIRS:
            raise BridgeError(400, f"Add between 1 and {self.RR_MAX_PAIRS} emoji and role pairs.")
        if len({str(p["emoji"]).strip() for p in pairs}) != len(pairs):
            raise BridgeError(400, "Each emoji can only be used once per message.")
        # Check every role before posting anything, so a bad pair can't leave a
        # half-configured message in the channel.
        for pair in pairs:
            role = guild.get_role(int(pair["role_id"])) if str(pair.get("role_id", "")).isdigit() else None
            if role is None:
                raise BridgeError(400, f"Choose a role for {pair['emoji']}.")
            problem = self._rr_role_problem(guild, role, member)
            if problem:
                raise BridgeError(403, problem)
        used = reaction_role_store.count_for_guild(self.storage, guild.id)
        if used + len(pairs) > reaction_role_store.MAX_MAPPINGS_PER_GUILD:
            raise BridgeError(409, f"This server can have {reaction_role_store.MAX_MAPPINGS_PER_GUILD} reaction roles; it has {used}.")
        channel = self._rr_channel(guild, body.get("channel_id"))

        embeds = getattr(self.bot, "embeds", None)
        embed = embeds.create(title=title, description=description, color=color) if embeds else discord.Embed(title=title, description=description, color=color)
        try:
            message = await channel.send(embed=embed)
        except discord.HTTPException as exc:
            raise BridgeError(409, f"The bot couldn't post in #{channel.name}: {exc}") from exc
        added, failed = [], []
        for pair in pairs:
            try:
                added.append(await self._rr_add_one(guild, message, str(pair["emoji"]), pair["role_id"], member))
            except (BridgeError, reaction_role_store.ReactionRoleError) as exc:
                failed.append({"emoji": str(pair["emoji"]), "error": getattr(exc, "message", str(exc))})
        if not added:
            # Nothing usable: don't leave an inert message behind.
            try:
                await message.delete()
            except discord.HTTPException:
                pass
            raise BridgeError(400, failed[0]["error"] if failed else "No emoji could be added.")
        log.info("Panel created a reaction-role message in guild %s by user %s", guild.id, member.id)
        return web.json_response(
            {"ok": True, "message_id": str(message.id), "channel": channel.name, "added": added, "failed": failed,
             "url": f"https://discord.com/channels/{guild.id}/{channel.id}/{message.id}"}
        )

    async def h_rr_remove(self, request: web.Request) -> web.Response:
        """Remove one mapping, or every mapping on a message (emoji omitted).
        Also takes the bot's own reaction off, as /reactionrole remove does."""
        guild = self._guild(request)
        body = await self._json(request)
        self._rr_admin(guild, self._user_id(request, str(body.get("user_id", ""))))
        raw_message = str(body.get("message_id", ""))
        if not raw_message.isdigit():
            raise BridgeError(400, "message_id is required.")
        message_id, emoji_text = int(raw_message), str(body.get("emoji") or "")
        # Only this server's mappings are ever touched.
        rows = [r for r in reaction_role_store.list_for_message(self.storage, message_id) if r["guild_id"] == guild.id]
        if emoji_text:
            rows = [r for r in rows if r["emoji"] == emoji_text]
        if not rows:
            raise BridgeError(404, "That reaction role no longer exists.")
        for row in rows:
            reaction_role_store.remove_mapping(self.storage, message_id, row["emoji"])
        channel = guild.get_channel(rows[0]["channel_id"])
        if channel is not None and hasattr(channel, "fetch_message"):
            try:
                message = await channel.fetch_message(message_id)
                for row in rows:
                    await message.clear_reaction(discord.PartialEmoji.from_str(row["emoji"]))
            except discord.HTTPException:
                pass  # message gone or no permission: the mapping is removed either way
        log.info("Panel removed %s reaction role(s) in guild %s", len(rows), guild.id)
        return web.json_response({"ok": True, "removed": _stringify_ids(rows)})

    # ---------- newsletter (owner announcements by DM) ----------

    def _newsletter_audiences(self) -> dict[str, dict[str, Any]]:
        opted_out = newsletter_store.opted_out_ids(self.storage)
        result = {}
        for audience in newsletter_store.AUDIENCES:
            found = collect_recipients(self.bot.guilds, audience, opted_out)
            result[audience] = {"recipients": len(found["user_ids"]), "opted_out": found["opted_out"], "guilds": found["guilds"]}
        return result

    async def h_newsletter_overview(self, request: web.Request) -> web.Response:
        sends = newsletter_store.list_sends(self.storage)
        for send in sends:
            send["failures"] = newsletter_store.failure_summary(self.storage, send["id"]) if send["counts"]["failed"] else []
        return web.json_response(
            {
                "audiences": self._newsletter_audiences(),
                "opted_out_total": len(newsletter_store.opted_out_ids(self.storage)),
                "sends": sends,
                "running_id": self.newsletter.running_id,
                "note": self.newsletter.note,
                "limits": {"title": newsletter_store.MAX_TITLE_LENGTH, "body": newsletter_store.MAX_BODY_LENGTH},
            }
        )

    async def h_newsletter_test(self, request: web.Request) -> web.Response:
        """Send the draft to the bot owner only, exactly as recipients will see it."""
        body = await self._json(request)
        title, text, _ = newsletter_store.validate(str(body.get("title", "")), str(body.get("body", "")), "owners")
        try:
            await self.newsletter.send_test(title, text, self.bot.config.owner_id)
        except discord.Forbidden as exc:
            raise BridgeError(409, _dm_refused(exc)) from exc
        except discord.HTTPException as exc:
            raise BridgeError(502, f"Could not DM you the test: {exc}") from exc
        return web.json_response({"ok": True})

    async def h_newsletter_send(self, request: web.Request) -> web.Response:
        body = await self._json(request)
        title, text, audience = newsletter_store.validate(
            str(body.get("title", "")), str(body.get("body", "")), str(body.get("audience", ""))
        )
        if self.newsletter.running_id is not None:
            raise BridgeError(409, "A newsletter is already being sent. Wait for it to finish or cancel it.")
        found = collect_recipients(self.bot.guilds, audience, newsletter_store.opted_out_ids(self.storage))
        if not found["user_ids"]:
            raise BridgeError(409, "There is nobody to send to.")
        # The panel shows a count before sending; refuse if it changed a lot
        # underneath (e.g. the member cache was still filling), so the owner
        # never sends to far more people than they confirmed.
        expected = body.get("expected")
        if isinstance(expected, int) and len(found["user_ids"]) > expected * 1.2 + 5:
            raise BridgeError(409, f"The audience grew to {len(found['user_ids'])} since you looked. Reload and confirm again.")
        send_id = newsletter_store.create_send(self.storage, title, text, audience, found["user_ids"])
        self.newsletter.start(send_id)
        log.info("Panel started newsletter %s to %s recipients (%s)", send_id, len(found["user_ids"]), audience)
        return web.json_response({"ok": True, "id": send_id, "recipients": len(found["user_ids"])})

    async def h_newsletter_resume(self, request: web.Request) -> web.Response:
        raw = request.match_info["send_id"]
        send = newsletter_store.get_send(self.storage, int(raw)) if raw.isdigit() else None
        if send is None:
            raise BridgeError(404, "No such newsletter.")
        if send["state"] not in newsletter_store.RESUMABLE_STATES or not send["counts"]["pending"]:
            raise BridgeError(409, "That newsletter has nothing left to send.")
        self.newsletter.start(send["id"])  # only still-pending recipients are messaged
        return web.json_response({"ok": True, "remaining": send["counts"]["pending"]})

    async def h_newsletter_cancel(self, request: web.Request) -> web.Response:
        if not self.newsletter.cancel():
            raise BridgeError(409, "No newsletter is being sent right now.")
        return web.json_response({"ok": True})


def _dm_refused(exc: discord.Forbidden) -> str:
    """Explain a refused DM to the owner. Discord does not say which privacy
    rule applied, so this names the usual one and how to change it."""
    return (
        f"Discord refused the DM to your account (error {exc.code}). The bot is working; this is decided by your "
        "Discord privacy settings. Turn on direct messages from server members for at least one server you share "
        "with the bot (right-click the server, Privacy Settings, Direct Messages), then try again."
    )


def _stringify_ids(value: Any) -> Any:
    """Make a stored record JSON-safe for any consumer: big ints become strings."""
    if isinstance(value, dict):
        return {key: _stringify_ids(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_stringify_ids(item) for item in value]
    if isinstance(value, int) and not isinstance(value, bool) and abs(value) > 2**53:
        return str(value)
    return value


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(PanelBridge(bot))
