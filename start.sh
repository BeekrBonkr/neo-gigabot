#!/bin/bash
# Entrypoint used by neo-gigabot.service (and safe to run by hand for local
# testing). Resolves its own directory so it works regardless of cwd or how
# systemd invokes it, then execs the bot so the python process replaces this
# shell (PID 1 of the unit's cgroup stays the bot itself - systemd's
# Restart=on-failure and `systemctl stop` signal it directly instead of an
# intermediate shell).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")"

if [ ! -x ".venv/bin/python3" ]; then
    echo "error: .venv/bin/python3 not found. Set up the venv first:" >&2
    echo "  python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
    exit 1
fi

if [ ! -f ".env" ]; then
    echo "error: .env not found. Copy .env.example to .env and fill in DISCORD_TOKEN." >&2
    exit 1
fi

exec .venv/bin/python3 main.py
