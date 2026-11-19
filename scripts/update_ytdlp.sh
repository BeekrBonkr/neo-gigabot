#!/bin/bash
# Upgrade yt-dlp inside the bot's venv and restart the service only if the
# version actually changed. YouTube breaks stale yt-dlp releases every few
# weeks (symptom in the journal: ffmpeg "HTTP error 403 Forbidden" on every
# track), so this is meant to run from a systemd timer - see
# neo-gigabot-ytdlp-update.{service,timer}.
#
# Safe to run by hand as the repo owner:  scripts/update_ytdlp.sh
# (then it upgrades but can only restart the service if you have sudo rights).
set -euo pipefail

cd "$(dirname "$(readlink -f "$0")")/.."

PIP=.venv/bin/pip
# The live bot is a system unit. A test copy run as a systemd user unit sets
# BOT_SERVICE and BOT_SERVICE_SCOPE=user (see neo-gigabot-beta-ytdlp-update.service).
SERVICE=${BOT_SERVICE:-neo-gigabot}
SCOPE=${BOT_SERVICE_SCOPE:-system}
OWNER=$(stat -c %U .)

# When run as root (from the systemd timer) do the pip work as the repo owner
# so nothing in .venv ends up root-owned.
run_pip() {
    if [ "$(id -u)" -eq 0 ] && [ "$OWNER" != "root" ]; then
        runuser -u "$OWNER" -- "$PIP" "$@"
    else
        "$PIP" "$@"
    fi
}

version() { run_pip show yt-dlp 2>/dev/null | awk '/^Version:/ {print $2}'; }

before=$(version)
run_pip install --quiet --upgrade yt-dlp
after=$(version)

if [ "$before" = "$after" ]; then
    echo "yt-dlp already up to date ($after); not restarting $SERVICE"
    exit 0
fi

echo "yt-dlp upgraded $before -> $after"
if [ "$SCOPE" = "user" ]; then
    if systemctl --user is-active --quiet "$SERVICE"; then
        systemctl --user restart "$SERVICE"
    fi
    exit 0
fi
# Only restart when the service is actually running; a stopped bot stays stopped.
if systemctl is-active --quiet "$SERVICE"; then
    if [ "$(id -u)" -eq 0 ]; then
        systemctl restart "$SERVICE"
    else
        sudo -n systemctl restart "$SERVICE" || echo "could not restart $SERVICE (no sudo); restart it manually" >&2
    fi
fi
