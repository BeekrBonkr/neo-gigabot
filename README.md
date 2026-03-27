# gigabot

Clean starter skeleton for migrating the legacy GigaBot project into a single-process `discord.py` bot using cogs.

## Included

- `main.py` entrypoint
- Cog modules for fun, images, moderation, music, settings, and owner commands
- Shared utility modules for config, storage, settings, checks, and embeds
- YAML-backed per-guild settings scaffold
- `.env.example` for secrets and local configuration

## Not included

- Legacy levels system. This skeleton intentionally drops leveling.
- Legacy commands. These should be migrated piece by piece from the old project.

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python main.py
```

## Project layout

```text
gigabot/
├── main.py
├── cogs/
│   ├── fun.py
│   ├── images.py
│   ├── moderation.py
│   ├── music.py
│   ├── settings.py
│   └── owner.py
├── utils/
│   ├── config.py
│   ├── settings.py
│   ├── checks.py
│   ├── storage.py
│   └── embeds.py
└── storage/
```

## Migration suggestion

Start by migrating these areas in this order:

1. Settings and prefix logic
2. Owner/debug commands
3. Fun commands
4. Moderation commands
5. Image commands
6. Music commands

Keep the first migration passes simple. Do not try to fully modernize each feature while moving it.
