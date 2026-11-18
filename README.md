# neo-gigabot

<div align="center">

<a href="https://ko-fi.com/bkrbnkr"><img alt="Support me on Ko-fi" src="https://img.shields.io/badge/Ko--fi-buy_me_a_coffee-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white"></a>

</div>

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
  `play`, `search`, `join`, `skip`, `seek`, `pause`, `resume`, `stop`, `leave`, `volume`, `nowplaying`
- Queue management: `queue`, `remove`, `move`, `shuffle`, `loop` (track / queue / off), `resetq`
- An interactive **remote** (`/music remote`) with buttons for pause/skip/volume — every press
  re-checks that the presser is actually in the voice channel
- **Playlists** (`/music playlist …`): create, add/remove songs, rename, public/private,
  like/unlike, browse other users' profiles, and `savenow` to capture the playing track
- Optional **autoplay** (`/music autoplay`): when the queue runs out, similar tracks keep playing
  until `/music stop` or everyone leaves
- Per-server DJ mode (role-gated playback control), DJ-admin override role, default volume,
  and queue/track-length caps — all from the dashboard
- Commands that alter live playback require you to be in the bot's voice channel

### 🖼️ Images — `/image …`, `/filter …`, `/animate …`, `/memegen …`, `/edit …`
- Grouped into `effects`, `transform`, `text`, `template`, and `misc`:
  deepfry, swirl, shake, jpegify, pixelate, blur, rotate/flip/flop/crop/stretch,
  caption/toptext/bottomtext, motivational posters, quote images, format conversion, and more
- **Face detection** (OpenCV DNN): `facebox`, `blurfaces`, `facecrop`, `faceswap`, `googlyeyes`
- **Content-aware scale** (seam carving), GIF-aware — effects apply to every frame
- **Color filters** (`/filter …`): invert, grayscale, sepia, hue, adjust (brightness, contrast, saturation),
  posterize, edges (pencil sketch or neon outlines), emboss, vaporwave
- **Animations** (`/animate …`): spin, rainbow, wave, zoom, pet, bonk and triggered (on an image or a member's avatar),
  plus GIF tools: speed, reverse, frames (pull one frame out), togif (join images into a GIF)
- **Meme formats** (`/memegen …`): speechbubble, wanted poster, drake, changemymind, jail, wasted, respects,
  whodidthis, wheeze.
  Formats built on a well-known picture use the real template, kept in `assets/images/`
- **Layout and utility** (`/edit …`): mirror, tile, collage, border (frames and rounded corners), circle,
  resize, palette (main colors with hex codes), removebg (cuts out the main subject with a small bundled model)
- **Text that survives real input**: every command that writes on an image draws emoji in color (flags,
  skin tones and joined emoji included), custom server emoji, accents, Cyrillic, Greek, Arabic, Hebrew,
  Chinese, Japanese, Korean, Thai and Hindi, turns fancy Unicode letters into the command's own font, and
  shows mentions as names. Fallback fonts live in `assets/fonts/`
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
- **Automod**: invite links, banned words, mass mentions and message spam, each with its own
  action (delete, warn or mute); automatic warnings feed the same escalation
- **Anti-raid**: a burst of joins starts a lockdown (`/lockdown on|off|status`), plus a minimum
  account age for new members
- **Verification gate** (`/verification …`): a Verify button that grants a role, with an optional
  minimum account age
- **Honeypot channel** (`/honeypot create|set|off`): anyone who posts in it is banned, softbanned
  or kicked. The bot can create the channel itself and keeps a notice in it that counts how many
  accounts it has caught; chosen users and roles can be exempted
- **Case history and notes**: every action is recorded (`/modhistory`), with private moderator
  notes (`/note add|remove`)
- **Ban appeals**: the ban DM links to an appeal form on the website; admins approve or deny there.
  The form is built on the website (written answers, multiple choice, checkboxes, required
  questions), with an intro, a choice of notification channel, a wait time after a denial, and
  test appeals for trying it out
- `/softban` and `/slowmode`
- **Sticky roles**: restores a returning member's roles (never roles with elevated permissions)
- **Mod log**: kick/ban/mute/warn actions posted to a channel, filterable per action type,
  with optional DM notices to the actioned member

### ✨ Community
- **Reaction roles** (`/reactionrole …`): map emoji on any message to roles, one at a time,
  in batch, or with a guided builder that creates the message for you
- **Starboard**: messages that collect enough ⭐ (emoji/threshold configurable) get reposted
- **Welcome / leave messages** as plain text, a custom embed or a generated image card, with
  `{mention}` / `{name}` / `{server}` / `{count}` templates, an optional DM to new members, and auto-delete
- **Role menus** (`/rolemenu …`): buttons or a dropdown members use to pick roles, with an
  only-one-of-these mode
- **Suggestions** (`/suggest`, `/suggestion approve|deny`): numbered suggestions with vote buttons
- **Tags** (`/tag`, `/tags set|delete|list`): custom canned replies
- **Sticky messages** (`/stick`, `/stickembed`, `/stickslow`, `/stickstop`, `/stickstart`,
  `/stickremove`, `/getstickies`): a message kept at the bottom of a channel
- **Scheduled messages** (`/schedule add|list|remove`): announcements at a set time, once or on repeat
- **Temporary voice channels**: joining a hub channel creates a voice channel of your own
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
| `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET` | | Let `/meme`, `/reddit` and `/cursed` read Reddit through its API. Without them the bot uses Reddit's public pages, which Reddit refuses from many servers. Create a "script" app at [reddit.com/prefs/apps](https://www.reddit.com/prefs/apps) |
| `REDDIT_USER_AGENT` | | How the bot names itself to Reddit, such as `discord:my-bot:v1 (by /u/yourname)` |
| `SUPPORT_SERVER_INVITE` | | Shown in `/about` |
| `WEBSITE_URL` | | Address of the bot's website (the public site of neogigabot-panel, if you run it). When set, `/about`, `/help`, `/settings`, the music and playlist embeds and the first-join greeting link to it |
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

**Keep yt-dlp fresh.** YouTube breaks stale yt-dlp releases every few weeks; the symptom
is ffmpeg logging `HTTP error 403 Forbidden` on every track. A weekly timer upgrades it
and restarts the bot only when the version changed:

```bash
sudo cp neo-gigabot-ytdlp-update.service neo-gigabot-ytdlp-update.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now neo-gigabot-ytdlp-update.timer
sudo systemctl start neo-gigabot-ytdlp-update.service   # run once now
```

The same script works by hand: `scripts/update_ytdlp.sh`.

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

---

## Support

<div align="center">

This bot is free and open source, and I work on it in my spare time.<br>
If it saved you some time, you can buy me a coffee. No pressure - the code stays free either way.

<a href="https://ko-fi.com/bkrbnkr"><img alt="Support me on Ko-fi" src="https://img.shields.io/badge/Ko--fi-bkrbnkr-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white"></a>

</div>

## License

GPL-3.0, see [LICENSE](LICENSE).
