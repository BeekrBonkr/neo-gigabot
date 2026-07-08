# neo-gigabot

A general-purpose Discord bot: music with shareable playlists, image manipulation and memes,
moderation with escalating warnings, reaction roles, a starboard, per-server activity/mod
logging, and a pile of fun commands — all slash commands, configured through a single
interactive `/settings` dashboard instead of dozens of setting subcommands.

This is the modern rewrite ("neo") of the legacy gigabot, with a one-shot importer for the
old bot's data (see [Migrating from legacy gigabot](#migrating-from-legacy-gigabot)).

---

## Features

### 🎵 Music — `/music …`
- Playback from YouTube / SoundCloud / Bandcamp / Vimeo (URL-allowlisted) or search:
  `play`, `search`, `join`, `skip`, `pause`, `resume`, `stop`, `leave`, `volume`, `nowplaying`
- Queue management: `queue`, `remove`, `move`, `shuffle`, `loop` (track / queue / off), `resetq`
- An interactive **remote** (`/music remote`) with buttons for pause/skip/volume — every press
  re-checks that the presser is actually in the voice channel
- **Playlists** (`/music playlist …`): create, add/remove songs, rename, public/private,
  like/unlike, browse other users' profiles, and `savenow` to capture the playing track
- Per-server DJ mode (role-gated playback control), DJ-admin override role, default volume,
  and queue/track-length caps — all from the dashboard
- Commands that alter live playback require you to be in the bot's voice channel

### 🖼️ Images — `/image …`
- Grouped into `effects`, `transform`, `text`, `template`, and `misc`:
  deepfry, swirl, shake, jpegify, pixelate, blur, rotate/flip/flop/crop/stretch,
  caption/toptext/bottomtext, motivational posters, quote images, format conversion, and more
- **Face detection** (OpenCV DNN): `facebox`, `blurfaces`, `facecrop`, `faceswap`, `googlyeyes`
- **Content-aware scale** (seam carving), GIF-aware — effects apply to every frame
- Flexible source resolution: attachment → message link → a "Set Image Source" context menu →
  the most recent image in the channel
- Hardened: image fetches are restricted to Discord's CDN, downloads capped at 25 MB,
  GIFs capped at 120 processed frames, heavy work runs off the event loop with concurrency caps

### 🛡️ Moderation
- `kick`, `ban`, `tempban <duration>`, `unban`, `mute`/`unmute` (timeouts), `warn`
- **Warnings** with history (`/warnings view|remove|clear`) and optional auto-escalation
  (mute/kick/ban at N warnings, optionally only counting recent ones)
- Role hierarchy enforced everywhere: mods can't act on (or warn) members ranked at/above them
- Mass tools: `/role` (add/remove for everyone), `/autorole` (+ apply now), `/prune`
- Channel tools: `/purge`, `/clean`, `/lock`, `/unlock`, `/say`, `/nick`
- **Sticky roles**: restores a returning member's roles (never roles with elevated permissions)
- **Mod log**: kick/ban/mute/warn actions posted to a channel, filterable per action type,
  with optional DM notices to the actioned member

### ✨ Community
- **Reaction roles** (`/reactionrole …`): map emoji on any message to roles, one at a time,
  in batch, or with a guided builder that creates the message for you
- **Starboard**: messages that collect enough ⭐ (emoji/threshold configurable) get reposted
- **Welcome / leave messages** with `{mention}` / `{server}` templates and auto-delete
- **Activity log**: message edits/deletes, joins/leaves, role/channel/voice changes to a
  log channel, with per-channel and per-user ignores

### 🎉 Fun
`/meme`, `/reddit`, `/cursed`, `/trivia`, `/joke`, `/fact`, `/catfact`, `/wiki`, `/weather`,
`/gif`, `/cute`, `/beans`, games (`/simon`, `/tictactoe`, `/hotpotato`),
`/poll` (yes/no or up to 5 options), `/snipe`, `/ascii`, `/hug`, `/kiss`, `/kill`, `/insult`,
`/coinflip`, `/roll`, `/ping`, `/server`, `/userinfo`, `/about`, `/feedback`

### ⚙️ Per-server configuration — `/settings`
One admin-only interactive dashboard (no memorizing subcommands) covering:
bot channels, disabled commands, activity log, moderation (autoroles, sticky roles,
warning escalation), mod log, welcome/leave, starboard, and music. Settings live in SQLite
and survive the bot being kicked and re-invited.

### 🔧 Owner tooling — `/owner …` (DM-only, owner-only)
| Command | What it does |
| --- | --- |
| `system` | Host/process CPU, RAM, disk, uptime, latency |
| `servers list/info/channels/invite/leave` | Multi-server inspection and management |
| `commandlog [day]` | Download the raw command log for a date |
| `commandstats [day]` | Usage summary: totals, statuses, top commands/users/servers |
| `errors [day]` | Failed commands for a date, with exception types |
| `backup` | Snapshot `settings.db` (SQLite online backup) and upload it; keeps 14 rotated copies |
| `settings <server_id>` | Dump a server's stored settings as JSON |
| `sync` | Force-sync the slash-command tree with Discord |
| `reload <cog>` | Hot-reload an extension |
| `restart` | Graceful shutdown; systemd brings it back |
| `loglevel <level>` | Change log verbosity at runtime |
| `status` / `avatar` | Presence text and bot avatar |
| `migrate` | Import legacy gigabot data (dry-run by default) |

Every command execution (with options, status, and errors) is logged to a daily file under
`storage/logs/`, rotated after 30 days.

---

## Setup

### Prerequisites
- **Python 3.12+**
- **ffmpeg** on the PATH (music playback): `sudo apt install ffmpeg` / `sudo dnf install ffmpeg`
- A Discord application with a bot token, **Message Content** and **Server Members**
  privileged intents enabled (Developer Portal → Bot → Privileged Gateway Intents)

The face-detection model files are bundled in `assets/models/` — nothing to download.

### Install

```bash
git clone git@github.com:BeekrBonkr/neo-gigabot.git
cd neo-gigabot
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env   # then edit it
chmod 600 .env
```

### Configure `.env`

| Variable | Required | Purpose |
| --- | --- | --- |
| `DISCORD_TOKEN` | ✅ | Bot token |
| `OWNER_ID` | ✅ | Your Discord user ID — gates `/owner` and `/feedback` delivery |
| `LOG_LEVEL` | | `DEBUG` / `INFO` / `WARNING` / `ERROR` (default `INFO`) |
| `OPENWEATHERMAP_API_KEY` | | Enables `/weather` ([free key](https://openweathermap.org/api)) |
| `GIPHY_API_KEY` | | Enables `/gif`, `/cute`, `/beans` ([free key](https://developers.giphy.com/)) |
| `SUPPORT_SERVER_INVITE` | | Shown in `/about` |
| `LEGACY_DATA_PATH` | | Old gigabot install root, for `/owner migrate` |

### Run

```bash
./start.sh              # or: .venv/bin/python3 main.py
```

On boot the bot loads all cogs and registers slash commands with Discord **only when the
command tree actually changed** (a fingerprint is kept in `storage/`), so restarts don't
burn Discord's daily registration rate limit. Use `/owner sync` to force a re-sync.

### Run as a service (recommended)

A hardened systemd unit ships in the repo — see the comments at the top of
[`neo-gigabot.service`](neo-gigabot.service) for the full cutover checklist:

```bash
sudo cp neo-gigabot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now neo-gigabot
journalctl -u neo-gigabot -f
```

The unit uses `Restart=on-failure` with a crash-loop limit; `/owner restart` exits non-zero
on purpose so systemd restarts it. **Deploys are just `git pull` + restart** (the service
runs from the working tree).

---

## Storage layout

Everything lives under `storage/` (gitignored except the settings schema):

| Path | Contents |
| --- | --- |
| `settings.db` | SQLite (WAL mode): guild settings, playlists, warnings, tempbans, reaction roles, sticky roles, starboard entries |
| `settings/default.yml` | The default per-guild settings schema; guild rows are synced to it on read |
| `logs/commands-YYYY-MM-DD.log` | Daily command usage logs (30-day retention) |
| `backups/` | Rotated `settings.db` snapshots from `/owner backup` |

Treat `settings.db` as live production data. Prefer `/owner backup` and `/owner settings`
over opening it with the sqlite3 CLI while the bot is running.

---

## Migrating from legacy gigabot

`/owner migrate` (or `scripts/migrate_legacy_data.py` offline) imports the old bot's guild
settings, playlists, and reaction-role mappings. It defaults to **dry-run** — re-run with
`dry_run: False` to apply. Point it at the old install with `LEGACY_DATA_PATH`, the
`source_path` option, or keep the old install as a sibling `gigabot/` directory.
`PORTING_CHECKLIST.md` tracks command-level parity with the old bot.

---

## Development

```bash
.venv/bin/python -m pytest tests/    # unit tests for the utils layer
```

The `utils/` modules (settings sync, playlists, warnings, tempbans, reaction roles,
durations, link parsing, command-log parsing, migration mapping) are pure functions over a
`storage_path` and carry the test coverage. Cogs hold the Discord-facing glue.

Repo docs: `PORTING_CHECKLIST.md` (parity tracking), `CODE_SWEEP.md` and `CODE_AUDIT.md`
(past reviews and their resolutions), `ACTION_ITEMS.md`.
