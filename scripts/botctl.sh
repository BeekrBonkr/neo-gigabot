#!/bin/bash
# Convenience wrapper around systemctl/journalctl for the neo-gigabot service.
# Usage: scripts/botctl.sh {start|stop|restart|status|logs|enable|disable}
set -euo pipefail

SERVICE=neo-gigabot

usage() {
    echo "Usage: $0 {start|stop|restart|status|logs|enable|disable}" >&2
    exit 1
}

[ $# -eq 1 ] || usage

case "$1" in
    start)   sudo systemctl start "$SERVICE" ;;
    stop)    sudo systemctl stop "$SERVICE" ;;
    restart) sudo systemctl restart "$SERVICE" ;;
    status)  systemctl status "$SERVICE" --no-pager ;;
    logs)    journalctl -u "$SERVICE" -f -n 100 ;;
    enable)  sudo systemctl enable "$SERVICE" ;;
    disable) sudo systemctl disable "$SERVICE" ;;
    *)       usage ;;
esac
