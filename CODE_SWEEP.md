# neo-gigabot Code Sweep — 2026-07-06

Full read of all 28 project files (~11,600 lines): `main.py`, `utils/*`, `cogs/*`, `scripts/*`, plus configs.
Overall: this is a well-built codebase. Storage layer is careful (BEGIN IMMEDIATE transactions, caps with
migration bypasses, good docstrings explaining *why*), views consistently gate by author and disable on
timeout, and the heavy image/yt-dlp work is mostly pushed off the event loop. The findings below are
ordered by how much they matter.

---

## 🔴 High-impact bugs

### 1. `raise error` in cog error handlers silently kills the global handler (fun, music, images)

`cogs/info.py:126-131` documents — verified empirically, per the comment — that a local app-command error
handler which *raises* prevents `main.py`'s global handler from ever running. Yet three cogs do exactly
that:

- `cogs/fun.py:1521` (attached to 26 commands)
- `cogs/music.py:1451` (attached to 17 commands)
- `cogs/images.py:2208` (attached to 30 commands)

Consequences for ~73 commands:

- **Any unexpected exception produces no user-facing "Command Failed" message.** For deferred commands
  (`/play`, `/image effects swirl`, ...) the user stares at "thinking..." forever.
- **Nothing is written to the command log** — `record_command(..., status="ERROR")` only happens in
  `main.py:128`, which never runs. Cooldown hits handled locally also never log `COOLDOWN`.
- The traceback goes to discord.py's default logger instead of your structured
  `"Unhandled app command error"` path.

**Fix:** delete these three local handlers entirely. The global handler in `main.py` already sends the
identical cooldown/permission messages. (The only thing you'd lose is the `TransformerError` branch —
move that one case into the global handler.) Note `PlaylistGroup.on_error` (`cogs/music.py:1857`) already
does it correctly: it handles and falls through to its own catch-all *without* re-raising — but it also
never records to the command log, so centralizing is still better.

### 2. Most image commands process before deferring — 3-second timeout + event-loop blocking

`cogs/images.py`: `extract`, `jpegify`, `rotate`, `stretch`, `pull`, `squeeze`, `squash`, `convert`,
`toptext`/`middletext`/`bottomtext` (via `_text_command`), `caption`, `motivate`, `flip`, `flop`, and the
8 template commands all follow this order:

```python
image_bytes, filename = await get_source_image(...)   # network: history scan (50 msgs) + download
output, out_name = process_image_bytes(...)           # CPU: full PIL pipeline, every GIF frame
await self.bot.image_tools.defer(interaction)         # <-- only NOW
```

Two problems:

- **Interaction token expiry.** Discord gives you 3 seconds to respond/defer. A channel-history scan plus
  attachment download plus a 100-frame GIF re-encode blows through that easily; `defer()` then raises and
  the user sees "The application did not respond".
- **Event-loop blocking.** All of that PIL work runs inline on the event loop. Only the five "heavy"
  commands (`swirl`, `shake`, `deepfry`, faces, `contentaware`) go through `_run_cpu_bound`. A large GIF
  through `/image transform rotate` freezes the *entire bot* (music playback stutters, every event stalls)
  for the duration.

**Fix:** do what `swirl`/`shake`/`deepfry` already do — `defer()` first, then run `get_source_image`, and
wrap the `process_image_bytes` call in `_run_cpu_bound(...)`. This is mechanical; the helpers already
exist. Same ordering issue (milder) in `cogs/reactionroles.py` `add`/`batch`: `_resolve_target_message`
does a network fetch before the defer at `cogs/reactionroles.py:259`/`285`, and in `/image text quote`
(`cogs/images.py:839`) where the message fetch precedes the defer.

### 3. The per-guild "prefix" setting is dead — but the dashboard still edits it

`main.py:46` sets `command_prefix=commands.when_mentioned`. `get_guild_prefix`
(`utils/settings.py:252`) is **never called anywhere**. Yet the settings dashboard prominently offers
"Edit Prefix" (`cogs/settings.py:245,447-465`) with validation, and `Config.default_prefix` /
`DEFAULT_PREFIX` env plumbing exists (`utils/config.py`). An admin who sets a prefix gets... nothing.

**Fix:** either (a) remove the prefix from the dashboard, the default schema, and `Config`, or (b) actually
honor it by making `command_prefix` a callable that reads the guild setting. Given the bot is fully
slash-command based (the only text command is `help`), (a) is the honest option.

### 4. No moderator-vs-target hierarchy check on kick/ban/mute

`cogs/moderation.py` `kick` (:263), `ban` (:305), `mute` (:377) check the target against **the bot's** top
role but never against **the invoker's**. Anyone with kick/ban/mute permission can act on members above
their own rank (including higher-ranked mods), as long as the target sits below the bot. Discord's own
built-ins block this; users expect bots to as well.

**Fix:** add alongside the existing check:

```python
if isinstance(interaction.user, discord.Member) and user.top_role >= interaction.user.top_role \
        and interaction.guild.owner_id != interaction.user.id:
    ...error "That member's highest role is above or equal to yours."
```

Also worth blocking `warn` on self/bots, and note `_maybe_escalate_warning` counts **lifetime** warnings —
see feature gap #1 below.

---

## 🟠 Bugs / correctness issues

### 5. `_mass_apply` progress spam when actions fail

`cogs/moderation.py:566`: `if done % MASS_ACTION_PROGRESS_INTERVAL == 0` — `done` only increments on
success, and `0 % 10 == 0`. If the first target fails (or any stretch of failures keeps `done` parked on a
multiple of 10), the bot edits the progress message **on every single iteration** — an API call every
0.5s for up to 300 members. Track the loop index instead: `for i, member in enumerate(capped, 1): ... if i % INTERVAL == 0`.

### 6. Stale stream URLs for long-queued tracks

`cogs/music.py:661`: `create_source` uses the cached `track.stream_url` and only extracts a fresh one when
it's `None`. YouTube stream URLs expire (~6h). A 100-track queue of 3h-max tracks means tracks deep in the
queue will fail at play time with a generic "Playback Error / Skipping it". **Fix:** on FFmpeg/source
failure in `player_loop`, retry once after `refresh_stream_url(track)`; or simply don't persist
`stream_url` from initial extraction for anything except the track that starts immediately.

### 7. `/lock`, `/unlock`, `/purge` assume a TextChannel

`cogs/moderation.py:516,534`: `target = channel or interaction.channel` — invoked from a thread, forum, or
voice-text, `overwrites_for`/`set_permissions` will blow up (and with finding #1 fixed, at least fail
cleanly). Guard with `isinstance(target, discord.TextChannel)`.

### 8. Blocking `/reddit` confusingly breaks `/meme` and `/cursed`

`cogs/fun.py:973`: `meme` checks the `"meme"` block, then calls `reddit_command.callback`, which re-checks
`"reddit"` — so blocking `reddit` blocks `meme`/`cursed` too, with an error message naming a command the
user didn't run. Pass an "already checked" flag or factor the fetch/send out of the command callback.

### 9. Welcome/leave messages don't sanitize mentions

`cogs/welcome.py:28-39` sends the templated string raw. A join message containing `@everyone` (or role
mentions) pings on every join. Only admins can set it, but it's still a footgun — send with
`allowed_mentions=discord.AllowedMentions(users=True, everyone=False, roles=False)`.

### 10. Blocked-command list stores case-sensitively

`cogs/settings.py:109-113`: toggling `Ping` then `ping` stores both (functionally OK since
`command_is_blocked` lowercases, but the toggle can't remove what it shows, and the dashboard displays
duplicates). Normalize with `.lower()` on insert.

---

## 🟡 Performance

### 11. Settings reads are heavily amplified — and fire on every message event

Every call to `get_guild_settings` (a) re-opens SQLite, (b) **re-reads and re-parses
`storage/settings/default.yml` from disk** (`get_default_settings` → `ensure_default_settings_file` +
`yaml.safe_load`), (c) runs the sync, and (d) may write back. Callers that hit this per event:

- `cogs/logging.py:_log` — every message edit/delete, member/role/channel/voice event, bot-wide
- `cogs/fun.py:_ensure_fun_command_allowed` — up to **3 separate reads per command**
  (`command_is_blocked` + `get_guild_settings` + `is_bot_channel`, each a full round trip)
- `cogs/welcome.py`, `cogs/moderation.py` join/leave listeners

**Fix:** a small in-process cache: `dict[guild_id, settings]` populated on read, invalidated in
`_upsert_guild_settings`, plus caching the parsed defaults at startup (they only change on deploy). One
mutex-free dict gets you ~99% of the win since everything runs on one event loop. Also fold fun.py's
triple-read into one read.

### 12. Reaction-role lookups hit SQLite on every reaction bot-wide

`utils/reactionroles.py:133` is called from `on_raw_reaction_add`/`remove` for **every reaction anywhere**.
The comment says "keep this cheap", but it's still a connection + query per emoji tap. Preload the set of
`message_id`s that have any mapping into memory (invalidate on add/remove/clear); bail out on the 99.9% of
reactions whose message isn't in the set before touching the DB.

### 13. One `aiohttp.ClientSession` per request

`cogs/fun.py:_fetch_json*`, `utils/imaging.py:fetch_bytes_from_url`, `cogs/owner.py:avatar` each build a
fresh `ClientSession` (and TCP/TLS handshake) per call. aiohttp's docs explicitly recommend one long-lived
session. Create `bot.http_session` in `setup_hook`, close it in `close()`.

### 14. `tree.sync()` on every startup

`main.py:66` syncs all commands globally each boot. Discord rate-limits command registration aggressively
(200 creates/day/guild-scope); a crash-loop under systemd's `Restart=` could exhaust it and get you
temporarily blocked. Standard pattern: sync only via an owner command (`/owner sync`) or when a stored
command-tree hash changes.

---

## 🔵 Code quality / dead code

- **Dead:** `cogs/info.py:34` `_feedback_cooldowns` (never used — the decorator cooldown handles it);
  `utils/warnings.py:46` `count_warnings` (warn uses `len(list_warnings(...))` — either use it or drop it);
  `utils/imaging.py:61-69` `ImageSourceStore.clear` vs `forget` are the same method with different returns.
- **Unbounded dicts:** `cogs/help.py:_mention_cooldowns`, `utils/imaging.py:_heavy_command_uses` (values
  are pruned but keys never), `utils/command_log.py:_write_locks` (one per day, forever), `cogs/fun.py:_snipes`
  (one per channel). None dangerous, all trivial to cap.
- `cogs/fun.py:619` uses `print()` for Reddit failures — everything else uses loggers.
- **`apraw==0.6.9a0`** is an abandoned alpha (last release 2020). It's the one dependency likely to break
  on a Python/aiohttp upgrade. `asyncpraw` is the maintained equivalent with a near-identical API; or drop
  the credential requirement entirely by using Reddit's public JSON endpoints (`/r/x/hot.json`) through
  your existing `_fetch_json` — you fetch hot posts read-only, no auth needed.
- `cogs/fun.py` duplicates `ImageToolkit.ensure_command_allowed` as `_ensure_fun_command_allowed` (with the
  redundant `bot_channels and` pre-check — `is_bot_channel` already returns True when unset). One shared
  helper on the bot would serve both.
- **No tests.** `utils/` is nearly all pure functions over a `storage_path` (settings sync, playlists,
  durations, link parsing, migration mapping) — very cheap to pytest with a tmp dir, and the migration
  logic especially deserves it.
- **No README.** `PORTING_CHECKLIST.md` is great internal doc, but setup (env vars, ffmpeg, model files,
  systemd unit) lives only in scattered comments.

---

## 💡 Feature suggestions

Ordered by how naturally they fit what's already built:

1. **Warning management: `/warnings remove <id>` and `/clearwarnings`, plus optional expiry.** Right now
   warnings are permanent and unremovable, and auto-escalation counts lifetime totals — a member warned
   3× over two years gets auto-kicked on warning #3 even if the first two are ancient. Add a delete
   command (the `id` is already shown in `/warnings`) and an optional "only count warnings from the last
   N days" setting. The schema needs nothing new.

2. **`/userinfo <user>`** — you have `/server` and `/image misc pfp` but no user card (join date, account
   age, roles, warning count for mods). Cheap, high-use.

3. **Music: queue `loop` mode and `/music move <from> <to>`.** The `player_loop`/`GuildMusicState` design
   makes both trivial (re-append current track on loop; deque rotate for move). Loop is the most-requested
   music-bot feature that's missing.

4. **`/tempban <duration>`** — you already have `parse_duration` and the mod-log plumbing; store
   `(guild_id, user_id, unban_at)` in a table and check it in a background task loop. Complements the
   existing timeout-based mute nicely.

5. **Multi-option polls.** `PollView` is yes/no only; a `/poll` with up to ~5 choices is a small
   generalization of the same button pattern. Also note current poll tallies are in-memory only — a bot
   restart mid-poll freezes the buttons dead (non-persistent views). Consider `timeout=None` +
   persistent views with counts in the embed footer, or accept and document the limitation.

6. **Reaction-role self-healing.** Mappings outlive their roles/messages: hook `on_guild_role_delete` and
   `on_raw_message_delete` to purge dead mappings (you already return removed rows from `clear_message`
   for exactly this kind of cleanup).

7. **Starboard** — classic fit for this bot's shape: you already have raw-reaction listeners, an embeds
   helper, and per-guild settings storage. One table, one listener, one settings-dashboard row.

8. **`/remindme <duration> <text>`** — `parse_duration` exists; one table plus a background task. Pairs
   with the tempban scheduler infrastructure if you build that first.

9. **`/slowmode <seconds>`** — one-liner (`channel.edit(slowmode_delay=...)`), fits the moderation set.

10. **Log rotation for `storage/logs/`** — daily command logs accumulate forever. A startup sweep deleting
    files older than N days is ~5 lines in `ensure_storage_layout`.

---

## Checked and fine (so you don't re-audit)

- `.env` is properly gitignored and has never been committed (verified `git ls-files` + log).
- SQLite concurrency: `BEGIN IMMEDIATE` write transactions + busy timeout are correct for this scale.
- Music URL allowlist (`ALLOWED_URL_HOSTS`) correctly guards yt-dlp's generic extractor against SSRF.
- Playlist privacy is enforced at every read path (`get_visible_playlist`, likes, profile listing).
- Owner cog is DM-only *and* identity-checked; the visibility-vs-authorization distinction is documented.
- Seam-carving GIF strategy (record seams once, replay per frame) is a genuinely clever cost cap.
- Interaction `is_done()` branching in `EmbedManager.respond` / `ImageToolkit.send_error` is correct.
