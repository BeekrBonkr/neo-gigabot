# GigaBot → neo-GigaBot Porting Checklist

Generated from a full review of `gigabot/` (legacy, prefix-command, 4-process bot) against
`neo-gigabot/` (in-progress, single-process, slash-command rewrite) on 2026-07-04.

Legend: ✅ done · 🟡 partial/needs work · ⬜ not started

---

## 0. Architecture Changes Already Made (good — keep these)

- ✅ Collapsed 4 separate always-logged-in-with-the-same-token processes
  (`giga.py`, `gigamusic.py`, `levels.py`, `setman.py`) into **one** `discord.py` `commands.Bot`
  with cogs (`main.py` + `cogs/*`). The old 4-process setup logged in 4 times with one token,
  which is wasteful, risks duplicate event handling, and had zero coordination between processes
  writing to the same YAML files.
- ✅ Config moved from a plaintext `config.yml` (checked out in the working dir with a live bot
  token, OpenAI key, and Reddit password sitting in it) to `.env` + `.env.example`, and `.env` is
  properly git-ignored.
- ✅ Shared `EmbedManager` (`utils/embeds.py`) gives every command consistent embed styling
  instead of every command hand-rolling `discord.Embed(...)`.
- ✅ Per-guild settings moved from one-YAML-file-per-guild (`storage/settings/<id>.yml`, no locking,
  read/written by up to 4 concurrent processes) to a single SQLite DB (`storage/settings.db`) with
  a `default.yml` schema template and an automatic key-sync (`sync_with_default`) that adds new
  settings keys and drops stale ones. This is a **storage format change**, not a drop-in — see the
  Data Migration section below for what's needed to carry old settings over.

---

## 1. Fun Commands (`cogs/fun.py`)

| Command | Status | Notes |
|---|---|---|
| `ping` | ✅ | |
| `coinflip` | ✅ | |
| `roll` | ✅ | |
| `ascii` | ✅ | |
| `8ball` | ✅ | |
| `catfact` | ✅ | |
| `fact` | ✅ | |
| `joke` | ✅ | |
| `server` (server info) | ✅ | |
| `reddit <subreddit>` | ✅ | |
| `meme` | ✅ | now pulls from Reddit instead of a hardcoded meme API |
| `cursed` | ✅ | |
| `insult` | ✅ | |
| `hug` / `kiss` / `kill` | ✅ | |
| `trivia` | ✅ | upgraded to buttons instead of reaction-based answering |
| `wiki <query>` | ✅ | rewritten against Wikipedia's REST summary API via `aiohttp` instead of the old `wikipedia` package, which made **blocking** synchronous HTTP calls from inside an async command (would have frozen the whole bot's event loop while it ran). Falls back to "did you mean" suggestions via the opensearch API on a miss or disambiguation page. |
| `weather <city>` | ✅ | API key moved to `.env` (`OPENWEATHERMAP_API_KEY`, was hardcoded in `giga.py`). Command is cleanly disabled with a clear message if unset, rather than erroring. Distinguishes "city not found" (404) from "service misconfigured" (401, logged server-side) instead of a generic failure. |
| `gif <query>` | ✅ | Giphy search. API key moved to `.env` (`GIPHY_API_KEY`, not hardcoded); disabled with a clear message if unset. Results capped to `rating=pg-13` as a content-safety default, and pool of 15 results randomized client-side instead of always returning the top hit. |
| `beans` | ✅ | thin wrapper around the same Giphy helper as `/gif` |
| `cute` | ✅ | thin wrapper around the same Giphy helper as `/gif` |
| `quote <user>` | ✅ | ported into `cogs/images.py` instead, and **improved**: takes a message link rather than scanning the last 30 channel messages for the user's last non-command message |
| `snipe` | ✅ | in-memory per-channel cache (no more unlocked `storage/snipes.json` shared across 4 processes). **Added a 5-minute expiry** that the old version didn't have — a message deleted an hour ago shouldn't be resurrectable by anyone who wanders in and runs `/snipe`. Requires the `message_content` privileged intent, now enabled in `main.py` (must also be turned on in the Discord Developer Portal). |
| `suggest <text>` | 🚫 | **Descoped (2026-07-04)** — dropped intentionally, not porting. Removed the admin config half too (`/settings suggestion`, `suggestion_channel_ids`) since it's dead weight without the submission command. |
| `tictactoe` | ✅ | rebuilt as a 3x3 button grid instead of number-emoji reactions; bot move logic (random empty square) kept as-is for parity |
| `simon` | ✅ | rebuilt with 4 color buttons instead of reactions; buttons are now disabled while the pattern is flashing (the old version left them clickable during playback, letting a fast player guess ahead of the reveal) |
| `hotpotato` | ✅ | **fixed the original's global-state bug** — it used module-level variables (`is_game_running`, `potato_holder`, ...) so only one game could run at a time *bot-wide across every server*. Now scoped per-channel (`self._hotpotato_games: dict[channel_id, HotPotatoGame]`), so multiple channels/guilds can run simultaneous games. Also replaced the old per-second lobby countdown message edit (30 edits per game = needless rate-limit pressure) with a single Discord relative timestamp (`<t:...:R>`) that the client renders live. |
| `poll <prompt>` | ✅ | ported into `cogs/moderation.py` as planned, and modernized to a button-based view with live vote tallies (see §3) instead of the old 👍/👎 reaction poll |
| `nick <name>` | ✅ | ported into `cogs/moderation.py` as planned (see §3) |
| `/help` equivalent | ✅ | **New `cogs/help.py` (2026-07-04).** Curated, categorized help menu — a real upgrade over the old reaction-driven browser and over Discord's own flat slash-command list, which the user found unwieldy for a bot this size. Category grouping/emoji/order is hand-curated (`CATEGORY_DEFS`); the description shown per command is always pulled live from that command's own registered `description`, so it can't drift stale the way the old bot's hardcoded `commands_dict` did (which even had a hardcoded `'Total Commands': '108'` that was surely already wrong). Triggerable via `/help` (ephemeral - only the invoker sees it) **or** by pinging the bot with nothing else in the message (DMs the menu instead, since plain messages have no ephemeral concept - posts a self-deleting "sent you a DM" notice in-channel, or falls back to an auto-deleting in-channel message if the user's DMs are closed). A classic `@GigaBot help` text-command works too. Both a Home button and a category `Select` dropdown are locked to the original invoker via `interaction_check`. |

All Fun-category commands from the old bot are now ported. `poll` and `nick` ended up in
`cogs/moderation.py` rather than here, per the plan noted above; `suggest` is intentionally dropped.

---

## 2. Image Commands (`cogs/images.py`)

This cog is essentially **complete and arguably improved** over the original:

- ✅ All transforms ported: `rotate`, `stretch`, `pull`, `squeeze`, `squash`, `swirl`, `shake`,
  `flip`, `flop`, `jpegify`, `deepfry`, `convert`, `extract`
- ✅ All meme-text commands: `toptext`, `middletext`, `bottomtext`
- ✅ All template overlays: `chimp`, `cooked`, `doom`, `craftify`, `halflife`, `murica`, `point`, `northkorea`
- ✅ `pfp`
- ✅ **New/better than original**: a unified image-source resolution order (attachment →
  message-link → a per-user "selected source" set via right-click context menu → most recent
  image in channel), replacing the old bot's single "scan last few messages for an attachment"
  approach used inconsistently across commands
- ✅ Context menu commands: "Set Image Source" / "Clear Image Source"

- ✅ **Text handling finished (2026-07-04).** Fonts are in `assets/fonts/` (`impact.ttf`,
  `arial.ttf`) — moved there from two places they'd been dropped (`assets/` flat and, incorrectly,
  `storage/`, which is for runtime data, not bundled assets). Font selection now matches the old
  bot's actual design instead of a single shared lookup: `_get_meme_font()` (Impact, bold-stroke
  toptext/middletext/bottomtext) and `_get_quote_font()` (Arial, `/quote`) are separate, each with
  its own system-font fallback chain covering both Debian/Ubuntu and Fedora/RHEL DejaVu paths.
  Font objects are cached (`functools.lru_cache`) since Arial.ttf alone is ~4MB and the meme-text
  auto-shrink loop re-requests fonts at several sizes per call.
- ✅ **Fixed a critical infinite-loop bug in `/quote`.** The line-count shrink loop recomputed
  `shorter` from the original `content` and the original `len(content)` every iteration without
  ever reassigning `content` — so if one 10-character trim didn't get under 11 lines, the loop
  never terminated. Since `_build_quote_image` runs inline on the event loop (not in an executor),
  this would have **frozen the entire bot for every guild**, triggerable by anyone quoting a long
  enough message. Fixed with an upfront input cap (400 chars — nothing longer usefully fits on the
  400x400 canvas anyway), a shrink step proportional to what's left (converges in ~4 iterations
  even from Discord's 4000-char max instead of only ever cutting 10 chars off), and a hard
  iteration cap as a backstop that guarantees termination regardless.
- ✅ Hardened `_wrap_text` to hard-break a single token that's too wide to fit on its own line
  (e.g. a long URL with no spaces) character-by-character, instead of letting it render off the
  edge of the image un-wrapped.
- ✅ **Meme-text auto-sizing fixed (2026-07-04).** `toptext`/`middletext`/`bottomtext` used to only
  ever *shrink* from a fixed default (50px), so short captions rendered tiny instead of filling the
  image the way real meme text does. Replaced with `_fit_text_to_box()`, a binary search (the
  standard technique real meme generators use — confirmed via research into imgflip/Kapwing/canvas
  auto-fit implementations) that finds the largest font size that fits within a margin-based box,
  so text is always as large as possible without touching the edges, growing *or* shrinking as
  needed. `size` became an optional `max_size` cap instead of a fixed starting point. Also added
  auto-uppercasing, matching universal meme-generator convention (imgflip, Kapwing, etc. all do
  this regardless of input casing).
- ✅ **New: `/caption`** — the classic "white box caption" format (confirmed via research; this is
  literally what ezgif's "White Box Caption" tool and the r/whenthe style are named after): a solid
  white bar prepended above (or appended below, via a `position` choice) the image with black text,
  box height auto-fit to the wrapped text rather than a fixed size. Works on both static images and
  animated GIFs.
- ✅ **New: `/motivate`** — classic despair.com-style "demotivational poster": black background,
  thin white frame around the photo, bold title + optional smaller subtitle below. Fully
  procedural (no new template images needed), reuses the same auto-fit sizing helper.
- 🟡 `pfp` command changed semantics slightly — old command took an optional user and defaulted
  contextually; confirm the new required-`user` argument still covers "no argument" nicely, or add
  a default-to-self case if desired.

---

## 3. Moderation (`cogs/moderation.py`) — ✅ fully ported and modernized (2026-07-05)

- ✅ `kick <user> [reason]` / `ban <user> [reason, delete_message_days]` / `unban <user_id> [reason]`
  — all with role-hierarchy pre-checks (can't target someone with a role ≥ the bot's own top role,
  can't target yourself) and a best-effort DM to the target *before* the action, since a kicked/
  banned member may become un-DMable afterward. `unban` takes a raw user ID (banned users aren't
  guild members, so there's nothing for a `Member`-typed option to resolve against).
- ✅ `mute <user> <duration> [reason]` / `unmute <user> [reason]` — **rebuilt on Discord's native
  member timeout** (`Member.timeout()`) instead of the old bot's custom `muted` role. This directly
  fixes the bug this checklist entry used to flag here: a hand-rolled mute role needs a permission
  overwrite added to *every* channel, including ones created after the role existed, and the old
  bot never did that. Discord's native timeout has no such gap (enforced by Discord itself, not
  per-channel overwrites). Duration is a short string (`10m`, `2h`, `1d12h`, `1w`), parsed and
  capped at Discord's own 28-day timeout limit.
- ✅ **New: `warn <user> <reason>` / `warnings <user>`** — the old bot's `warn` had no persistence
  at all (DM + log line, then gone forever), which makes it nearly useless for actually tracking a
  problem member. Added a `warnings` SQLite table (`utils/warnings.py`) so warnings have an audit
  trail; `/warnings <user>` lists a member's history. Modest, deliberate scope addition — not
  building a full case-management system, just enough that "how many times have I warned this
  person" has an answer.
- ✅ `purge <count>` (bulk delete, capped at 100 — Discord's own per-call bulk-delete batch limit) /
  `clean <count>` (deletes only the bot's own messages; the old prefix-based filter is gone since
  there's no more text prefix to filter on)
- ✅ `lock <channel>` / `unlock <channel>` (toggles the `@everyone` `send_messages` overwrite)
- ✅ `prune default|specific|exclude [role]` — mass-kick tool. Replaced the old bot's "type `yes` to
  confirm" plain-message prompt (doesn't map to slash commands) with a button-based `ConfirmView`.
  Capped at 300 targets per run with rate-limit-friendly pacing (0.5s between kicks + periodic
  progress edits on the confirmation message) so one command can't either hammer Discord's rate
  limits or silently run past the ~15-minute webhook-token validity window on a huge server.
- ✅ `role add|remove <role>` (mass role add/remove for all non-bot members) and
  `autorole add|remove|apply <role>` share the same capped/paced mass-action helper as `prune`.
  **`autorole` now actually does something at runtime**, not just config: added an
  `on_member_join` listener in this cog that applies the configured autoroles to every new member —
  previously `autoroles` was a schema key nothing ever read at join time.
- ✅ `nick <name>` (bot's own nickname, invoker needs `manage_nicknames`)
- ✅ **New: `poll <prompt>`** — modernized from the old bot's 👍/👎 reaction poll to a button-based
  `PollView` with live vote tallies shown on the buttons themselves and one-vote-per-user (switchable
  between yes/no, not just "add another reaction").

Verified directly (not just a load smoke-test): `parse_duration` against valid/invalid/zero/
over-limit inputs, the 28-day timeout cap, `ConfirmView`'s wrong-user rejection plus confirm/cancel/
timeout paths, `PollView`'s vote-toggle-and-switch logic, the warnings table's guild/user isolation,
kick's self-target and role-hierarchy blocks (confirmed the actual `kick()` call is never reached,
not just that an error message is shown), mute's invalid/too-long duration rejection, and the full
`/prune` confirm→kick and cancel→no-op flows end to end against faked Discord objects.

---

## 4. Server Settings / Admin (`cogs/settings.py`)

- ✅ **Rewritten as a single interactive dashboard (2026-07-05).** The old ~10 separate
  `/settings <subcommand>` commands (which required admins to already know each command's name
  and syntax) were replaced with one `/settings` command that opens a categorized, button/select/
  modal-driven panel (`SettingsDashboardView` in `cogs/settings.py`): General, Activity Log,
  Moderation, Welcome, and Music categories, switchable via a dropdown. Channel/role/user pickers
  use Discord's native `ChannelSelect`/`RoleSelect`/`UserSelect` components (pre-populated with the
  current value via `default_values`) instead of requiring mentions or IDs typed by hand; text
  values (prefix, join/leave message, DJ role name, warning duration, etc.) open a pre-filled
  `Modal` instead of a bare string argument. Everything is ephemeral and locked to the admin who
  opened it (`interaction_check`), matching the existing `ConfirmView`/`PollView` pattern in
  `cogs/moderation.py`. All edits go through the same `update_guild_settings`/`save_guild_settings`
  helpers the old commands used — no new storage path.
- ✅ **`setup`/`resetyaml` equivalent added** — the General category has a confirm-guarded
  "Reset To Defaults" button (reuses `create_guild_settings` to overwrite with
  `DEFAULT_GUILD_SETTINGS`), resolving the 🟡 flagged gap from the previous pass.
- ✅ **New settings** (`utils/settings.py`): a dedicated **moderation log channel**
  (`mod_log_enabled`/`mod_log_channel_id`, separate from the general activity log — nothing
  previously recorded kick/ban/mute/warn actions anywhere persistent), **warning escalation**
  (`warn_threshold`/`warn_action`/`warn_mute_duration` — auto-mute/kick/ban a member once their
  warning count in `utils/warnings.py` crosses an admin-set threshold), and **sticky roles**
  (`sticky_roles` — persists a departing member's roles in a new `utils/sticky_roles.py` table and
  restores them if they rejoin, via `on_member_remove`/`on_member_join` in `cogs/moderation.py`).
  All wired into the dashboard's Moderation category; `storage/settings/default.yml` updated to
  match.
- ✅ Reaction roles: `/reactionrole add|batch|remove|clear|list` — unchanged, see below.
- ✅ **New: `/reactionrole create` guided builder (2026-07-05).** The manual flow above still
  requires an existing message link and hand-typed emoji syntax, which isn't very approachable.
  `create` instead posts the embed message itself, then walks the admin through a
  `ReactionRoleBuilderView`: press **Add Mapping**, react to the posted message with the emoji you
  want (captured via a scoped `bot.wait_for("raw_reaction_add", ...)`, 30s timeout), then pick the
  role from a `RoleSelect` dropdown — no message link, no emoji-format guessing. Reuses the cog's
  own `_add_one()` internally, so it shares the exact same role-hierarchy check, reaction-add
  validation, and 100-mappings-per-guild cap as the manual `add` command rather than duplicating
  that logic.
- ✅ The whole **logging system** — see below.

### Logging system (`cogs/logging.py`) — ✅ fully ported and expanded (2026-07-04)

All of the original event listeners are ported, plus several new ones, with noise-reduction the
old bot didn't have:

- ✅ `on_message_edit` / `on_message_delete` / **`on_bulk_message_delete` (new — purge detection)**
- ✅ `on_member_join` / `on_member_remove` — join/leave logging only (see the `welcome` row above
  for why welcome messages/autorole weren't folded in here)
- ✅ **`on_member_ban` / `on_member_unban` (new)** — these are native Discord gateway events, so
  they fire for *any* ban/unban regardless of how it happened (slash command once one exists,
  right-click UI, another bot) — a real capability upgrade over the old bot, which only logged bans
  performed through its own commands. Both are audit-log-enriched to show the moderator and reason.
- ✅ `on_voice_state_update` (join/leave/move) — separate from the music cog's own voice-state
  listener, which handles auto-disconnect, not logging
- ✅ `on_member_update` (nickname, role add/remove) **+ new: timeout (communication-disabled)
  applied/lifted, with remaining duration**
- ✅ `on_guild_role_create` / `on_guild_role_delete` / `on_guild_role_update`
- ✅ `on_guild_channel_create` / `on_guild_channel_delete` / `on_guild_channel_update`
- ✅ `on_guild_update`, `on_guild_emojis_update` **+ new: `on_guild_stickers_update`** (stickers
  didn't exist as a guild feature when the old bot was written)
- ✅ `on_invite_create` / `on_invite_delete`
- ✅ **New: `on_thread_create` / `on_thread_delete` / `on_thread_update`** (threads were barely
  used/didn't exist when the old bot was written)
- ✅ **New: kick detection.** `on_member_remove` alone can't tell a kick from a voluntary leave —
  added a best-effort audit-log lookup (matches the departed member against a recent `kick` entry
  within a 15s window) so the log correctly says "👢 Member Kicked" with moderator + reason instead
  of just "📤 Member Left" for every departure.
- 🚫 `on_guild_join` / `on_guild_remove` — not treated as part of *per-guild* logging (there's no
  log channel to send to for a guild that's brand new or already gone); `cogs/settings.py` already
  creates default settings on join. A bot-wide "joined/left a server" notification to an owner
  channel would be a different feature, not implemented.
- 🚫 The old `poll_config_changes` "config changed externally" poller — dropped intentionally, not
  relevant now that settings live in SQLite instead of hand-editable YAML files.

**Noise reduction added** (the old bot had none of this — every one of these was a real,
demonstrated-in-testing source of log spam):
- Message "edits" where the content is actually unchanged (Discord fires `on_message_edit` when a
  link embed loads, not just on real edits) are skipped.
- Role/channel updates where nothing user-relevant actually changed (most `role_update`/
  `channel_update` events are just position churn from a drag-reorder) are skipped — only real
  field changes (name, color, permissions, topic, NSFW, slowmode, etc.) trigger a log entry.
- Voice state changes where the channel didn't change (mute/deafen/camera toggles) are skipped —
  only join/leave/move are logged.
- Bot accounts are skipped in message and voice-state logging (a music bot connecting/disconnecting
  constantly isn't log-worthy); bot accounts are *not* skipped for member join/leave, since another
  bot being added to the server is a meaningful, security-relevant event worth knowing about.
- Every audit-log lookup and every log-channel send is wrapped defensively (missing "View Audit
  Log" or channel-send permission degrades to "unknown actor" / silently skips, rather than
  spamming errors or crashing a listener).

Verified directly (not just read-through): the enable/disable + per-channel/per-user ignore gating,
every noise-reduction filter above (unchanged-edit, position-only role update, same-channel voice
state, bot voice state), audit-log enrichment for bans (moderator + reason shown correctly) and
graceful degradation when no matching audit entry exists, and emoji-diff add/remove detection —
all via direct calls against the listener methods with fake Discord objects, not just a bot-load
smoke test.

---

## 5. Music (`cogs/music.py`) — ✅ fully ported and hardened (2026-07-04)

All gigamusic.py features are now ported, cleaned up, and hardened against abuse:

- ✅ `join`, `play`, `queue`, `nowplaying`, `skip`, `pause`, `resume`, `stop`, `leave`, `shuffle`, `volume`
- ✅ DJ mode (`musicdj`, `musicdjrole`) and default volume (`musicdefaultvolume`)
- ✅ Auto-disconnect when the voice channel empties or the queue drains
- ✅ `search` — dropdown (`SearchResultsView`) of 5 results instead of the old number-emoji reactions
- ✅ `savenow` — save the currently playing track to one of your playlists, with autocomplete
- ✅ `resetq` — `manage_guild`-gated force-clear + disconnect, for stuck states
- ✅ `remove <position>` — remove a specific queued track (replaces the old reaction-driven removal)
- ✅ **Playlists** — full `/playlist` group backed by a new SQLite `playlists` table
  (`utils/playlists.py`), not YAML: `create`, `delete`, `rename`, `add`, `removesong`, `shuffle`,
  `description`, `status`, `view` (merged with the old `viewother` via an optional `user` param),
  `play`, `profile`, `like`, `unlike`, `liked`. Browsing/liking/playing use button/select `View`s
  (`PlaylistView`, `PlaylistPickerView`) instead of the old reaction+`wait_for` flows.

**Hardening added while porting** (per explicit request to design for abusive users):

- Direct-URL playback is restricted to an allowlist (YouTube, SoundCloud, Bandcamp, Vimeo,
  including subdomains). Anything else typed as an absolute URL is rejected with a clear message
  instead of being handed to yt-dlp's generic extractor — closes an SSRF-ish vector where a user
  could point the bot at an arbitrary/internal host.
- Fixed a real **privacy bug**: the old `/playlist viewother` (and its like/unlike reaction) never
  checked the private/public flag, so anyone could view or like another user's "private" playlist
  by name. `get_visible_playlist()` now enforces it everywhere (view, play, like/unlike).
- Per-guild `asyncio.Lock` around voice connect/move, so two near-simultaneous `/play`/`/join`
  calls in the same guild can't race into a double-connect.
- All yt-dlp extraction (`extract_track`, `search_tracks`, `refresh_stream_url`) goes through a
  shared helper with a **bot-wide concurrency semaphore** (max 4 at once) and a **25s timeout**,
  so a burst of `/play`/`/search` calls (or a slow/hanging remote host) can't exhaust the executor
  thread pool or hang a guild's playback indefinitely.
- Cooldowns added to every music command (none existed before this pass) — e.g. 2 uses/5s on
  `/play`, 1/15s on `/search`, 1/30s on `/playlist add` (it can do up to 10 external lookups).
- Playlist writes go through `utils/db.py`'s `write_transaction()` (`BEGIN IMMEDIATE`), so two
  concurrent edits to the same playlist can't silently clobber each other the way plain
  read-modify-write YAML did in the old bot.
- Playlist name/description are validated and length-capped (name: `[A-Za-z0-9_-]{1,20}`,
  description: 200 chars); hard caps kept from the original (5 playlists/user, 50 songs/playlist,
  10 songs added per call) are now enforced at the storage layer, not just in command handlers.
- `/playlist play` and the "Queue This Playlist" button route through the same DJ-mode/bot-channel
  gate as `/play` — the old bot let non-DJs bypass DJ mode entirely via playlists.

**Storage note**: playlists are **not** in `storage/playlists.yml` anymore — they're in
`storage/settings.db`'s `playlists` table (shared connection helper: `utils/db.py`). The
`storage/playlists.yml` → SQLite migration in §8 still needs to be written before old playlist
data can be carried over.

---

## 6. Levels / XP System — 🚫 Descoped (2026-07-04)

**Not porting.** Dedicated leveling bots do this better, and chasing feature parity here (message
XP, sentiment/"personality" tracking, level-up role rewards, leaderboard) isn't a good use of time
for this project. Nothing was ever built in neo-gigabot for this (no cog, no schema keys), so
there's nothing to remove — this is purely a decision to stop tracking it as a gap.
`gigabot/level_data.yml` and `gigabot/levels.py` stay behind in the legacy bot with no migration
path, by design.

---

## 7. Owner / Dev Tools (`cogs/owner.py` + `cogs/info.py`) — ✅ done (2026-07-04)

Split across two cogs on purpose: `cogs/owner.py` stays owner-gated (its `interaction_check`
restricts every command, including nested `/owner servers *` subcommands — verified this
propagates correctly, since a bug there would've made destructive commands like
`servers leave`/`migrate` callable by anyone); `cogs/info.py` holds the genuinely public
commands, since the old bot lumping `feedback`/`invite`/`uptime`/`support` in with owner-only
`avatar`/`status`/`system` under one "❎ Beek Only" category looks like it was never actually
gated that way in spirit, just dumped in the same bucket.

**`cogs/owner.py`** (owner-only):
- ✅ `reload` (already existed) — now also catches `commands.ExtensionError` cleanly instead of
  leaking a raw traceback to the interaction
- ✅ `system` — CPU/RAM/disk/process-memory/uptime via `psutil` (added to `requirements.txt`), plus
  gateway latency, Python/discord.py versions
- ✅ `avatar` — takes an attachment **or** a URL (URL fetching is fine here since only the verified
  owner can invoke it, unlike the SSRF-conscious host-allowlist added earlier for `/play`, which
  is reachable by any user)
- ✅ `status` — change presence text + activity type (playing/watching/listening/competing). The
  old bot's auto-rotating canned status list was **not** carried over — a static, owner-chosen
  status seemed more useful than a cycling one nobody asked for; easy to add back if wanted
- ✅ `servers` group: `list`, `info`, `channels`, `invite`, `leave`
- 🚫 `servers spy <server> <channel>` — **deliberately not implemented**, replaced by command-usage
  logging instead (see below) after discussing the tradeoff directly: reading recent message
  history from arbitrary channels with no consent/audit trail is real surveillance, whereas logging
  what *commands* get run (with their arguments) surfaces most of the same abuse signal — someone
  using `/caption`/`/gif`/etc. for something bad — without reading conversations the bot has no
  reason to see.
- ✅ **New: `migrate`** — see §8, wraps the same migration logic as the standalone script
- ✅ **New: command-usage logging + `commandlog <day>`.** Every app command invocation, bot-wide
  across every guild (not a per-guild opt-in like `cogs/logging.py`'s activity log — this is for
  the bot *operator*, not individual server admins), is appended to a plain-text
  `storage/logs/commands-YYYY-MM-DD.log` file via `on_app_command_completion` (successes) and the
  existing `on_app_command_error` handler (failures, tagged by type: cooldown/missing-permissions/
  check-failed/other). Each line records timestamp, guild+channel (or "DM"), user, the full
  command path (`playlist add`, not just `add`), and the resolved option values (so e.g. the actual
  text passed to `/caption` shows up, not just that `/caption` was run) — capped at 200 chars per
  value and with attachments/users/channels/roles formatted readably rather than dumped as repr.
  `/owner commandlog <day>` (accepts `YYYY-MM-DD`, `today`, or `yesterday`) uploads that day's file
  as a Discord attachment. Concurrent writes across simultaneously-completing commands in different
  guilds are serialized per-file via an `asyncio.Lock` keyed by path, so they can't interleave into
  a corrupted line — verified with 250 concurrent writes producing exactly 250 well-formed lines.
  `storage/logs/` was **not previously gitignored** (unlike `storage/cache/`/`storage/temp/`) —
  fixed, since these files can contain real user-submitted text.
- ✅ **`/owner` is now invisible outside DMs.** Applied `@app_commands.allowed_contexts(guilds=False,
  dms=True, private_channels=False)` to the whole `Owner` group (verified in the actual sync
  payload: `"contexts": [1]`, Discord's BOT_DM-only value) — no server member will ever see `/owner`
  in their slash-command autocomplete in any guild the bot is in, since the command isn't
  registered there at all, not merely blocked from running. You now run `/owner ...` by DMing the
  bot directly rather than inline while looking at a guild. One honest caveat: this hides it from
  *guild* command lists, which is the realistic threat model, but someone who deliberately opens a
  DM with the bot themselves would still see `/owner` listed there (execution stays blocked by the
  existing owner-ID check either way) — Discord has no per-user command visibility, only
  per-context.

**`cogs/info.py`** (public):
- ✅ `about` — consolidates the old bot's separate `giga`, `uptime`, `invite`, and `support` into
  one richer embed (server/user counts, uptime, latency, an auto-generated OAuth invite link with
  a sensible permission set, and a support-server link if `SUPPORT_SERVER_INVITE` is set in `.env`)
- ✅ `feedback` — DMs the configured owner; 60s per-user cooldown so it can't be used to spam the
  owner's DMs. Bug caught in testing: the initial version didn't catch `discord.NotFound`/
  `HTTPException` from `fetch_user` (e.g. a misconfigured `OWNER_ID`), so it would've surfaced as
  a generic "Command Failed" instead of the intended "Feedback Unavailable" message — fixed.

Both new commands are wired into the curated `/help` menu under a new "ℹ️ Info & Support" category.

---

## 8. Data Migration (YAML/JSON → SQLite) — ✅ fully done (2026-07-05)

Implemented as `utils/migration.py` (the actual logic), used by **both** a standalone
`scripts/migrate_legacy_data.py` (run by hand, supports `--dry-run`, safe to preview before
touching anything) and `/owner migrate` (same logic, live in the bot, also defaults to `dry_run:
True`). Verified against the **real** legacy data sitting in `gigabot/storage/` — not just
synthetic fixtures:

- ✅ `storage/settings/<guild_id>.yml` (48 of 49 files - the 49th, `default.yml`, is correctly
  skipped as not a guild) → `settings.db`'s `guild_settings` table. `djmode` → `music_dj_enabled`
  is remapped explicitly (same boolean meaning, new name); everything else that matched the new
  schema by name carries over as-is, and `save_guild_settings`'s existing schema-sync does the
  rest for free — `level_roles`/`levelling`/`xp_multiplier` (leveling descoped, see §6),
  `suggestion_channel_ids` (suggestions descoped), `rr` (static help text, not data), and
  `join_messages` (a typo'd duplicate of `join_message`) all get dropped automatically with no
  special-casing needed, since they're simply absent from `DEFAULT_GUILD_SETTINGS`.
- ✅ `storage/playlists.yml` (6 playlists / 64 songs in the real file) → the `playlists` table.
  Added `import_playlist_raw()` to `utils/playlists.py` specifically for this — it bypasses the
  5-playlists-per-user / 50-songs-per-playlist caps that guard the *live* `/playlist` commands
  against abuse, because a migration shouldn't silently truncate someone's real historical data
  just to enforce a limit designed to stop *future* abuse. Playlist names that don't fit the new
  `[A-Za-z0-9_-]{1,20}` charset (e.g. old names with spaces or accented characters) are coerced
  rather than dropped, with numeric-suffix collision handling if two legacy names coerce to the
  same key.
- 🚫 `storage/level_data.yml` — not migrated; leveling is descoped (§6), nothing to bring over.
- ✅ **`storage/reactionroles.yml`** → the `reaction_roles` table (2026-07-05, once §4's reaction
  roles feature existed to migrate *into*). Legacy shape is
  `{guild_id: {channel_id: {message_id: {role_id: emoji}}}}` — note the innermost mapping is keyed
  by **role ID with the emoji as the value**, the reverse of what you'd guess; a first pass assumed
  the opposite and the dry-run immediately failed all 69 real mappings with "role id '✅' is not
  numeric", which is exactly what caught it before it touched real data. Uses a new
  `import_reaction_role_raw()` (mirrors `import_playlist_raw`'s reasoning: bypasses the live
  100-mappings-per-guild abuse cap, since a migration shouldn't truncate real history to enforce a
  limit aimed at future abuse). No live Discord connection is needed for this migration at all —
  Discord persists a message's reactions independently of which bot added them, so the old bot's
  reaction is still sitting on the message; populating the SQLite mapping is sufficient for the new
  listeners to pick up right where the old bot left off. Verified against the real file: 7 guilds,
  21 messages, 69 emoji→role mappings, 1 message with an empty mapping (correctly skipped rather
  than erroring), zero custom emoji (all unicode) — all 69 migrated with zero failures on the
  corrected parse, then spot-checked by reading them back out of the target database.
- 🚫 `storage/snipes.json` — not migrated by design; `/snipe` is an intentionally-ephemeral
  in-memory cache now (5-minute expiry), not persisted storage.
- 🚫 `storage/reminders.json` — still believed orphaned (no `remind` command exists anywhere in
  the legacy codebase); not migrated since there's nothing in neo-gigabot to migrate it *to*.

---

## 9. Cleanup / Bugs Worth Fixing (not just preserving) While Porting

- The old bot hardcoded a live Discord token, OpenAI key, and Reddit password in
  `gigabot/config.yml`, plus a real OpenWeatherMap key and a Giphy key directly in `giga.py`
  source. None of this should be copied into `neo-gigabot` as literal strings — route everything
  through `.env` / `utils/config.py` (already the pattern for Discord token + Reddit creds; extend
  `Config` with `openweathermap_api_key` / `giphy_api_key` if/when those commands get ported).
- `get_prefix()` in `giga.py`/`levels.py` opens `storage/settings/{id}.yml` directly with no
  try/except — a brand-new guild the bot just joined (before `setup` had been run) would crash
  every message handler. `setman.py`'s `on_guild_join` mitigates this somewhat but it's fragile;
  the new bot's auto-create-on-join + `get_default_settings` fallback already fixes this
  architecturally — just make sure every remaining ported command reads settings via
  `utils/settings.py` rather than opening files directly.
- `storage/modules/test.py` in the old bot is a dead scaffold (a second, unused bot instance) —
  no need to port it or anything from it.
- `helpold` in `giga.py` is dead/disabled code left in place — no need to preserve.
- `requirements.txt` in the old bot lists `PyYAML` twice — not that it matters now, but don't
  carry the habit forward.
- Four processes writing to the same JSON/YAML files with no file locking was a standing
  data-corruption risk (`snipes.json`, `reminders.json`, `level_data.yml` were all
  read-modify-write with no atomicity). Solved by design in the new single-process + SQLite setup —
  just don't reintroduce raw multi-writer file access if you add new features.

### Full code review findings (2026-07-05) — fixed

- **`cogs/info.py`'s `/feedback` silently swallowed real bugs.** Its local `@feedback.error`
  handler re-raised for any non-`CommandOnCooldown` error. Verified empirically against
  discord.py's actual `CommandTree._call`: the local handler is awaited *before* the global
  handler, with no try/except around that await — so a re-raise there propagates out of `_call`
  entirely and the global `on_app_command_error` in `main.py` never runs. Net effect: a genuine bug
  in `/feedback` showed the user nothing and never hit the command log, unlike every other command.
  Fixed by deleting the local handler — the global one already covers the cooldown case identically.
- **Command log lost the actual exception type for every real bug.** `main.py`'s fallback error
  branch logged `type(error).__name__` — but discord.py wraps every exception a command body
  raises in `app_commands.CommandInvokeError`, so this always logged literally "CommandInvokeError"
  instead of the useful underlying type (`ValueError`, `discord.Forbidden`, etc). Fixed by unwrapping
  `error.original` before logging/recording. Verified: a `ValueError` raised from a fake command now
  correctly shows `error=ValueError` in the command log, not `error=CommandInvokeError`.
- **Missing-token startup error pointed at the wrong directory.** Said `Create gigabot/.env from
  gigabot/.env.example` — a leftover from before the rewrite; the files actually live in
  `neo-gigabot/`. Fixed to build the message from the resolved paths it already computes, so it's
  correct regardless of what the project directory is actually named or where it's deployed —
  relevant right now given the planned move to a new host, where this is the exact message a fresh
  `.env` setup would hit if something's missing.
- **`utils/checks.py` was 100% dead code.** Defined its own `guild_only()`/`owner_only()`, but
  nothing in the codebase ever imported from it — every command uses `app_commands.guild_only()`
  directly, and owner-gating is `Owner.interaction_check()` in `cogs/owner.py`. Deleted.
- **Noise**: `on_app_command_error` was logging a full exception traceback for every cooldown hit,
  permission check, and check failure — not just real bugs. Moved the `.exception()` call to only
  the genuine-bug fallback branch, so routine control flow doesn't bury real errors once there's
  actual traffic.

### Full code review findings (2026-07-05) — flagged, not yet fixed

These are architectural, not one-line fixes, so they're recorded here for a deliberate decision
rather than applied unprompted:

- **Every database call blocks the event loop.** `utils/db.py`'s `connect()`/`write_transaction()`
  are plain synchronous `sqlite3` calls, used directly (no thread offload) by every guild-settings,
  playlist, warning, and reaction-role read/write. `write_transaction`'s `BEGIN IMMEDIATE` can block
  for up to its full 10s busy-timeout under write contention — and because nothing hands this off a
  thread, that block freezes the *entire* asyncio event loop (every guild, the gateway heartbeat,
  voice) for its duration, not just the calling command. `cogs/music.py` already established the
  right idiom for this exact problem for yt-dlp extraction (`loop.run_in_executor(...)`); the DB
  layer never got the same treatment. Worth doing before this bot is under real multi-guild load.
- **Settings updates aren't atomic.** `update_guild_settings()` does a separate read connection,
  modifies in Python, then a separate write connection — unlike `utils/playlists.py`,
  `utils/warnings.py`, and `utils/reactionroles.py`, which all correctly wrap read-modify-write in a
  single `write_transaction`. Two concurrent settings changes to the same guild race, and the
  loser's change is silently dropped (whole-blob last-write-wins, not a merge). Low practical odds
  (admin-only, infrequent), but a real correctness gap and inconsistent with this codebase's own
  established pattern.
- **No in-memory settings cache.** `get_guild_settings()` — called by nearly every settings-gated
  check (`is_bot_channel`, `command_is_blocked`, `is_logging_enabled`, etc.) — hits SQLite fresh
  every single time, with no caching, despite being on the hot path of most messages and commands.
  Combined with the point above, a busy multi-guild bot means a steady stream of blocking
  synchronous DB round-trips on every message.

---

## 10. Pre-Launch Checklist (swapping the token over)

- [ ] All feature gaps above resolved (or explicitly descoped) to your satisfaction
- [ ] Legacy data migration script written and run against a **copy** of `gigabot/storage`
- [ ] Slash commands tested in a private test server with the real token before flipping production
- [ ] `assets/fonts/` populated (Impact/Arial) so meme-text commands render correctly
- [ ] `gigabot.service` (systemd unit) updated to point at `neo-gigabot`'s entrypoint, or a new
  unit file created — confirm `Restart=on-failure` and working directory are correct for the new
  layout
- [ ] Old `gigabot` process stopped/disabled **before** starting `neo-gigabot` with the same token,
  to avoid two processes racing to register slash commands or handle the same events
- [ ] Decide what happens to `gigabot/config.yml`'s live secrets (rotate the Discord token when you
  cut over, since it's been sitting in plaintext in a non-git-ignored file)
