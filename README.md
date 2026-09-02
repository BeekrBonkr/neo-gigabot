# neo-gigabot

<div align="center">

<a href="https://ko-fi.com/bkrbnkr"><img alt="Support me on Ko-fi" src="https://img.shields.io/badge/Ko--fi-buy_me_a_coffee-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white"></a>

</div>

A slash command recode of my original Gigabot, built on [discord.py](https://github.com/Rapptz/discord.py) 2.4+ and Python 3.12. The old bot grew into four prefix command scripts that were hard to reason about. This one keeps the parts people actually used (image manipulation, fun commands, music, core moderation) and moves them onto application commands, cogs, shared helpers and per-server settings in SQLite. Stored settings are synced to the default schema on every start, so adding a new setting never breaks a server that was configured before it existed.

## Table of contents

- [Features](#features)
- [Requirements](#requirements)
- [Setup](#setup)
- [Running](#running)
- [Commands](#commands)
- [Command permissions](#command-permissions)
- [Storage layout](#storage-layout)
- [Formatting](#formatting)
- [Status](#status)
- [Support](#support)
- [License](#license)

## Features

Everything is a slash command. The `message_content` intent is left off.

- **Fun** (`cogs/fun.py`): dice, coin flips, 8-ball, ASCII art, cat facts, useless facts, jokes, multiple-choice trivia with buttons, server info, and Reddit fetching (`/reddit`, `/meme`, `/cursed`) that turns itself off with a clear message when no Reddit credentials are configured.
- **Images** (`cogs/images.py`): the image and GIF commands from the old bot (jpegify, deepfry, swirl, shake, stretch/pull/squeeze/squash, rotate, flip/flop, convert, meme text) plus the overlay templates in `assets/images/` and `/quote`, which renders a Discord message link into a quote image. Commands find their source image in a fixed order: an attachment on the command, a message link, a message you picked with the **Set Image Source** context menu, then the most recent image in the channel. A picked source expires after 15 minutes so it cannot silently hijack a later command.
- **Moderation** (`cogs/moderation.py`): `/say`, `/purge`, `/timeout`, `/untimeout`, `/kick`, `/ban` and `/unban`, each gated on the matching Discord permission.
- **Music** (`cogs/music.py`): `/music` playback through `yt-dlp` and `ffmpeg`. Search with a pick-one dropdown, queue paging, move/remove/clear, shuffle, volume, pause/resume and buttons on the now-playing embed. Optional DJ mode restricts control to a named role (`music_dj_enabled`, `music_dj_role_name`). Playback can only be controlled from inside the bot's voice channel. The bot leaves on its own when the channel empties or the queue has been idle for 180 seconds, and the volume resets to the server default (`music_default_volume`) every time it joins.
- **Settings** (`cogs/settings.py`): per-server suggestion channels, blocked commands, bot channels, logging channel and music settings through `/settings`, stored in SQLite.
- **Help** (`cogs/help.py`): a category dropdown with paging that only lists commands the caller can actually run. Owner commands are hidden from everyone but the owner, blocked commands are hidden in that server, and commands needing permissions the caller lacks are filtered out.
- **Owner** (`cogs/owner.py`): reload cogs, resync application commands, inspect or reset any guild's settings, list guilds and extensions, health diagnostics and a graceful shutdown, all locked to `OWNER_ID`.

## Requirements

- Python 3.12+
- `ffmpeg` installed and available in `PATH` (music playback)
- A Discord application with the **Server Members** intent enabled (`message_content` is not needed)
- Optional: Reddit API credentials for `/reddit`, `/meme` and `/cursed`

`PyNaCl` (voice support) is included in `requirements.txt`.

## Setup

1. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   source .venv/bin/activate
   ```

2. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

3. Copy the example environment file and fill it in:

   ```bash
   cp .env.example .env
   ```

   At minimum:

   ```env
   DISCORD_TOKEN=your_bot_token_here
   OWNER_ID=your_discord_user_id
   LOG_LEVEL=INFO
   ```

4. Install `ffmpeg` if you want music:

   ```bash
   # Ubuntu/Debian
   sudo apt update && sudo apt install ffmpeg
   # Fedora
   sudo dnf install ffmpeg
   ```

   If `ffmpeg` is missing, music commands fail with a clear error instead of crashing playback.

### Environment variables

Loaded from `.env` by `utils/config.py`.

Required:

- `DISCORD_TOKEN`: the bot refuses to start without it.
- `OWNER_ID`: the Discord user ID allowed to use `/owner` and `/settings` (alongside each server's owner). Defaults to `0` if unset, which means nobody.

Optional:

- `LOG_LEVEL`: Python logging level, default `INFO`.
- `REDDIT_CLIENT_ID`, `REDDIT_CLIENT_SECRET`, `REDDIT_USER_AGENT`, `REDDIT_USERNAME`, `REDDIT_PASSWORD`: all five must be set for Reddit commands to work. `REDDIT_USER_AGENT` defaults to `neo-gigabot/1.0`.

## Running

```bash
python main.py
```

On start the bot creates the `storage/` layout, opens (or creates) the settings database, syncs every stored guild record against `storage/settings/default.yml`, loads the cogs and syncs the slash commands globally. Global sync can take up to an hour to show in Discord the first time.

## Commands

Everything below is subject to the per-server policy described in [Command permissions](#command-permissions).

### Fun

| Command | Description | Who can use it |
|---|---|---|
| `/ping` | Gateway latency. | Anyone |
| `/coinflip` | Flip a coin. | Anyone |
| `/roll [sides]` | Roll a die with a custom number of sides. | Anyone |
| `/ascii <text>` | Turn short text into ASCII art. | Anyone |
| `/8ball <question>` | Magic 8-ball. | Anyone |
| `/catfact`, `/fact`, `/joke` | Random cat fact, useless fact, joke. | Anyone |
| `/server` | Information about the current server. | Anyone, in a server |
| `/reddit <subreddit>` | Random hot post from a subreddit (NSFW posts only in NSFW channels). | Anyone; needs Reddit credentials |
| `/meme`, `/cursed` | Meme or cursed image from Reddit. | Anyone; needs Reddit credentials |
| `/insult`, `/hug`, `/kiss`, `/kill <user>` | Roast, hug, kiss or fictionally defeat someone. | Anyone |
| `/trivia` | Multiple-choice trivia with answer buttons. | Anyone |

### Images

| Command | Description | Who can use it |
|---|---|---|
| `/imageinfo` | Explains how image commands find their source image. | Anyone |
| `/clearimagesource` | Forget the message you picked as an image source. | Anyone |
| **Set Image Source** / **Clear Image Source** (message context menu) | Pick a message's image as your source for the next 15 minutes, or clear it. | Anyone |
| `/quote <message_link>` | Render a message into a quote image. | Anyone |
| `/extract` | Re-upload the selected image or GIF as a file. | Anyone |
| `/jpegify`, `/deepfry`, `/swirl`, `/shake` | Quality crush, deepfry, swirl effect, shaky GIF. | Anyone |
| `/rotate`, `/flip`, `/flop` | Rotate by an angle, flip vertically, flip horizontally. | Anyone |
| `/stretch`, `/pull`, `/squeeze`, `/squash` | Double the width or height, halve the width or height. | Anyone |
| `/convert <format>` | Convert an image to another format. | Anyone |
| `/toptext`, `/middletext`, `/bottomtext <text>` | Meme text at the top, middle or bottom. | Anyone |
| `/pfp <user>` | A user's avatar as a file. | Anyone |
| `/chimp`, `/cooked` | Append the chimp or cooked panel to the bottom. | Anyone |
| `/doom`, `/craftify`, `/halflife`, `/murica`, `/point`, `/northkorea` | Overlay the matching template from `assets/images/`. | Anyone |

### Moderation

Server only.

| Command | Description | Who can use it |
|---|---|---|
| `/say <message>` | Post your message as an embed. | Manage Messages |
| `/purge <amount>` | Delete a number of recent messages. | Manage Messages |
| `/timeout <member> <minutes>` | Time a member out. | Moderate Members |
| `/untimeout <member>` | Remove a timeout. | Moderate Members |
| `/kick <member>` | Kick a member. | Kick Members |
| `/ban <member>` | Ban a member. | Ban Members |
| `/unban <user_id>` | Unban a user by ID. | Ban Members |

### Music

Server only. Every command also respects DJ mode (see below) and, for anything that changes playback, requires you to be in the bot's voice channel.

| Command | Description | Who can use it |
|---|---|---|
| `/music help` | The music command guide. | Anyone |
| `/music join` | Join your voice channel. | Anyone |
| `/music play <query>` | Play a URL or search term. Searches show a dropdown of results to pick from. | Anyone |
| `/music queue` | Show the queue. | Anyone |
| `/music nowplaying` | Show the current track with control buttons. | Anyone |
| `/music skip`, `pause`, `resume` | Playback control. | Anyone |
| `/music stop` | Stop and clear the queue. | Anyone |
| `/music leave` | Disconnect and clear the queue. | Anyone |
| `/music shuffle` | Shuffle the queue. | Anyone |
| `/music remove <position>` | Remove a queued track. | Anyone |
| `/music move <from> <to>` | Move a queued track. | Anyone |
| `/music clearqueue` | Clear queued tracks, keep the current one playing. | Anyone |
| `/music volume <0-100>` | Set playback volume. | Anyone |

### Settings

Server only. The whole `/settings` group is locked to the server owner or the configured bot owner. Each command additionally requires the Discord permission listed.

| Command | Description | Who can use it |
|---|---|---|
| `/settings show` | Show this server's current settings. | Administrator |
| `/settings reset` | Reset this server to `default.yml`. | Administrator |
| `/settings suggestion <on/off> <channels>` | Configure suggestion channels. | Administrator |
| `/settings command <block/allow> <command>` | Block or allow a command in this server. | Administrator |
| `/settings botchannel <add/remove/clear> [channel]` | Manage the bot channels. | Administrator |
| `/settings logging <enabled> [channel]` | Turn logging on or off and set the log channel. | Administrator |
| `/settings music [dj_mode] [dj_role_name] [default_volume]` | DJ mode, DJ role name, default volume. | Manage Server |

### Help and owner

| Command | Description | Who can use it |
|---|---|---|
| `/help` | Browse commands by category. | Anyone |
| `/owner reload <cog>` / `/owner reloadall` | Reload one or all cogs. | Bot owner |
| `/owner sync` | Resync application commands. | Bot owner |
| `/owner guildsettings <guild_id>` | Inspect a guild's raw settings. | Bot owner |
| `/owner resyncguild <guild_id>` | Reset one guild to the default schema. | Bot owner |
| `/owner storage` | Show storage paths and resync the settings schema. | Bot owner |
| `/owner extensions`, `/owner guilds` | List loaded extensions or guilds. | Bot owner |
| `/owner health` | Simple health diagnostics. | Bot owner |
| `/owner shutdown` | Gracefully shut the bot down. | Bot owner |

## Command permissions

Three layers decide whether a command runs (`utils/command_policy.py`, `utils/settings.py`):

1. **Discord permissions**: moderation and settings commands declare `has_permissions` checks.
2. **Owner gates**: `/owner` accepts only `OWNER_ID`. `/settings` accepts the server owner or `OWNER_ID`.
3. **Per-server policy**: every fun, image, moderation and music command is checked against the server's `blocked_commands` list and its `bot_channels` list. If no bot channels are configured, commands are allowed in any channel. Otherwise they are refused outside those channels with a pointer to the right ones. Fun and image commands also work in DMs. Moderation and music are server only.

Server settings are created automatically the first time a server is seen and on every `on_guild_join`.

## Storage layout

Runtime data lives under `storage/` (created on start):

- `storage/settings.db`: SQLite, one `guild_settings` row per server holding the settings as JSON.
- `storage/settings/default.yml`: the default settings schema. Every stored server record is synced against it on start (new keys added, values preserved), and the built-in defaults fill in anything missing from the file itself.
- `storage/cache/`, `storage/temp/`: temporary runtime files.
- `storage/logs/`, `storage/playlists/`: created for future use.

## Formatting

`pyproject.toml` carries Black and Ruff settings (line length 100, Python 3.12 target, Ruff rules `E F I UP B` with `E501` ignored):

```bash
black .
ruff check .
```

## Status

The migration from the old Gigabot is mostly done. Working now: the slash command framework, per-server settings in SQLite with schema syncing, image commands, fun commands, music with `yt-dlp` and `ffmpeg`, owner commands and core moderation.

Still to do:

- playlist management (the `storage/playlists/` directory is created but nothing uses it yet)
- broader moderation automation
- more logging and event listeners
- test coverage

---

## Support

<div align="center">

This bot is free and open source, and I work on it in my spare time.<br>
If it saved you some time, you can buy me a coffee. No pressure - the code stays free either way.

<a href="https://ko-fi.com/bkrbnkr"><img alt="Support me on Ko-fi" src="https://img.shields.io/badge/Ko--fi-bkrbnkr-FF5E5B?style=for-the-badge&logo=kofi&logoColor=white"></a>

</div>

<!-- more ways to support go here -->
<!-- - [PayPal](...) -->
<!-- - [GitHub Sponsors](...) -->

## License

GPL-3.0, see [LICENSE](LICENSE).
