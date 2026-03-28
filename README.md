# neo-gigabot

A cleaner recode of the original Gigabot project.

The goal is to keep the useful features from the old bot while moving to a simpler, more maintainable slash-command based codebase.

## Current status

This repo is in active migration.

Implemented or usable now:
- slash-command bot framework
- per-server settings stored in SQLite with default schema syncing
- image manipulation commands
- fun commands
- music playback with yt-dlp + ffmpeg
- owner maintenance commands
- core moderation commands

Still worth improving later:
- playlist management
- broader moderation automation
- more logging and event listeners
- more migration cleanup from the legacy bot
- test coverage

## Requirements

- Python 3.12+
- ffmpeg installed and available in `PATH`
- Discord application with the needed intents enabled

For voice and music support, `PyNaCl` is included in `requirements.txt`.

## Setup

Create and activate a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Copy the example environment file:

```bash
cp .env.example .env
```

Then fill in at least:

```env
DISCORD_TOKEN=your_bot_token_here
OWNER_ID=your_discord_user_id
LOG_LEVEL=INFO
```

Run the bot:

```bash
python main.py
```

## ffmpeg note

Music playback depends on `ffmpeg`.

Ubuntu/Debian:

```bash
sudo apt update
sudo apt install ffmpeg
```

Fedora:

```bash
sudo dnf install ffmpeg
```

If `ffmpeg` is missing, music commands will fail with a clear error instead of crashing playback.

## Environment variables

Required:
- `DISCORD_TOKEN`
- `OWNER_ID`

Optional:
- `LOG_LEVEL`
- `REDDIT_CLIENT_ID`
- `REDDIT_CLIENT_SECRET`
- `REDDIT_USER_AGENT`
- `REDDIT_USERNAME`
- `REDDIT_PASSWORD`

Reddit values are only needed for Reddit-based commands.

## Storage layout

Runtime data is stored under `storage/`.

Important paths:
- `storage/settings.db` for per-guild settings
- `storage/settings/default.yml` for the default settings schema
- `storage/cache/` and `storage/temp/` for temporary runtime files

## Formatting

This repo now includes formatter settings in `pyproject.toml` for Black and Ruff.

Example:

```bash
black .
ruff check .
```

## Notes on settings

Server settings are stored per guild and automatically synced to the current default schema on startup.

If no bot channels are configured, commands are allowed in any channel unless blocked another way.

## Migration direction

The old Gigabot had a lot of useful functionality, but the code became hard to reason about.

This recode focuses on:
- slash commands first
- cleaner cogs
- reusable utility helpers
- safer per-server config management
- easier future additions like playlists and more admin tools
