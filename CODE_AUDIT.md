# Code Audit — 2026-07-08

> **Status (2026-07-08, later the same day):** all findings below fixed in the working
> tree; suite now at **114 passed**. Notes on the two non-literal fixes:
> #9 was addressed with SQLite WAL mode rather than `asyncio.to_thread` — all bot DB
> access is serial on one event loop, so the only real lock contention is an external
> process (sqlite3 CLI, backups) touching the live DB, which WAL defuses; threading
> ~50 write call sites was judged not worth the regression risk on a live bot.
> #6 included a startup migration that expands stored leaf names to every matching
> qualified name (behavior-preserving; unblock the unintended ones from the dashboard).
> Restart the service to deploy.

Full audit of the codebase (~14,700 lines: `main.py`, all 12 cogs, all utils, tests,
deployment files). Test suite ran clean at time of audit: **112 passed**.

**Overall:** well-built codebase. Parameterized SQL throughout, `BEGIN IMMEDIATE` write
transactions, a deliberate URL allowlist in music, hierarchy checks on mod actions, caps on
user-generated data, sanitized mentions in welcome templates, consistent error handling.
Findings below are ordered by severity.

**Priorities:** #1 (settings wipe on rejoin — silent data loss on live data) and
#2 (reaction-role privilege escalation — actual permission bypass).

---

## High — should fix

### 1. Bot rejoin wipes a server's entire settings
`cogs/settings.py:1109`

`on_guild_join` calls `create_guild_settings()` unconditionally, which upserts defaults over
whatever exists. If the bot is ever kicked and re-invited (or Discord replays the event after
an outage), the guild's config is silently reset. Guard with `guild_settings_exists()` first.
Since `settings.db` is live production data, this is the one to fix soonest.

### 2. Privilege escalation via reaction roles
`cogs/reactionroles.py:183`

`_role_usable()` only checks the role against the **bot's** top role, never the invoker's.
A moderator with Manage Roles can map an admin role that sits *above their own top role* to an
emoji, then react to grant it to themselves — bypassing Discord's own rule that you can't
assign roles above your rank. Add `role < invoker.top_role` (owner exempt) to `_add_one`,
mirroring `_invoker_outranks` in moderation.

### 3. `/role` crashes on servers with >300 matching members
`cogs/moderation.py:893`

When targets exceed the cap it sends a "Large Operation" warning via `warning_interaction()`
(which consumes the interaction response), then immediately calls
`interaction.response.send_message()` again at line 900 → `InteractionResponded` exception,
command dies before doing anything. Use a followup for the second message, or fold the warning
into the "Starting..." embed.

### 4. Unbounded/unrestricted downloads in the image pipeline
`utils/imaging.py:162`

`fetch_bytes_from_url()` fetches whatever URL is in a message embed with no host allowlist and
no size limit (`await response.read()`). Any user can post a link that unfurls to an arbitrary
URL and point an image command at it — the bot will fetch internal/arbitrary hosts (SSRF) and
buffer arbitrarily large responses into RAM. The music cog explicitly solved this exact problem
(`ALLOWED_URL_HOSTS`, with a comment calling it "an SSRF-ish abuse vector") but imaging didn't
get the same treatment. Relatedly, GIF processing expands every frame to RGBA in memory with no
frame-count or pixel cap — a large multi-frame GIF can consume gigabytes.

Suggested fix: restrict embed-image fetches to Discord CDN hosts (`cdn.discordapp.com`,
`media.discordapp.net`), cap download size (~25 MB), and cap GIF frames processed.

---

## Medium

### 5. `/warn` skips the hierarchy check
`cogs/moderation.py:636`

Kick/ban/tempban/mute all enforce `_invoker_outranks`; warn doesn't. A junior mod can warn a
senior mod, and with escalation configured, the auto mute/kick/ban then fires (it only checks
the *bot's* hierarchy) — effectively letting a lower-ranked mod ban a higher-ranked one through
the warning system.

### 6. Command blocking collides across groups
`cogs/settings.py:384`, `main.py:93`

Blocked commands are stored and matched by leaf name (`command.name`), so disabling
`/playlist add` also disables `/reactionrole add`, and `/music play` blocks `/playlist play`.
Store `qualified_name` instead (needs a small migration for existing blocked lists).

### 7. Sticky roles restore privileged roles
`cogs/moderation.py:1059`

On leave, *all* non-managed roles are saved — including mod/admin roles — and restored on
rejoin. A demoted-by-kick moderator gets their roles back by rejoining. Consider excluding
roles with elevated permissions, or at least documenting it in the dashboard blurb.

### 8. Anyone in a bot channel can control playback
`cogs/music.py:664`

`/music skip`, `stop`, `leave`, `shuffle`, `volume` only check bot-channel + DJ mode — not
whether the invoker is in (or even near) the voice channel. Without DJ mode on, someone not in
voice can kill everyone's music. The remote view already has the right check
(`check_remote_access`); apply the same voice-channel requirement to the destructive slash
commands.

### 9. Blocking SQLite/YAML on the event loop
`utils/db.py:12`, throughout

Every settings read/write is synchronous on the event loop. Reads are fine (cached), but writes
use `BEGIN IMMEDIATE` with a **10-second busy timeout** — under write contention a stuck lock
freezes the whole bot for up to 10s (heartbeats, all guilds). Low likelihood at current scale,
but worth `asyncio.to_thread` for the write paths eventually.

### 10. Shared `cv2.dnn.Net` across concurrent threads
`cogs/images.py:169`

`_face_net` is one instance, and `MAX_CONCURRENT_CPU_BOUND = 2` means two executor threads can
call `net.forward()` simultaneously. OpenCV DNN nets are not thread-safe for concurrent
inference — can produce garbage results or crash. Either lock around inference or use one net
per thread.

### 11. `.env` is world-readable

`chmod 600 .env` (and arguably `storage/settings.db`). The bot token is currently readable by
any local user.

---

## Low / polish

- **Timeout leaks executor threads** (`cogs/music.py:891`, `cogs/images.py:159`): on
  `asyncio.wait_for` timeout the semaphore is released but the worker thread keeps running, so
  real concurrency can exceed the caps under repeated timeouts.
- **`/playlist unlike` can like** (`cogs/music.py:2184`): it delegates to the toggle, so
  unliking something you never liked *adds* a like.
- **`/purge` on old messages**: `channel.purge` raises on messages >14 days old (bulk-delete
  limit) → generic "Command Failed" instead of a friendly message.
- **Raw exception text shown to users**: `_reddit_flow` embeds `` ```{exc}``` `` and image
  `send_error(str(exc))` — PIL/aiohttp internals leak into user-facing embeds. Meanwhile
  `catfact`/`fact`/`joke`/`trivia` have *no* network exception handling at all (aiohttp errors
  fall through to the generic global handler) — inconsistent both ways.
- **`/reactionrole create` color** accepts hex >`0xFFFFFF` (e.g. 8 digits) — fails later with
  an opaque HTTP 400 from Discord instead of the validation message.
- **`/unmute` on an expired timeout** (`timed_out_until` in the past but non-None) reports
  success for a no-op; use `member.is_timed_out()`.
- **Stale docstring** in `cogs/logging.py` references `/settings log`, `/settings logchannel`,
  `/settings userlog` — those commands no longer exist (replaced by the dashboard).
- **`warnings_view` timestamps** print raw SQLite strings (`2026-07-08 01:23:45`) — could use
  `format_dt` for consistency with the rest of the bot.
