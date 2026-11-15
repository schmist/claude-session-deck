#!/usr/bin/env bash
# Remove claude-session-deck for the current user.
set -uo pipefail

ext_uuid="claude-session-deck-windows@local"

systemctl --user disable --now claude-session-deck.service 2>/dev/null
rm -f "$HOME/.config/systemd/user/claude-session-deck.service"
systemctl --user daemon-reload

gnome-extensions disable "$ext_uuid" 2>/dev/null
rm -rf "$HOME/.local/share/gnome-shell/extensions/$ext_uuid"
rm -rf "$HOME/.local/share/claude-session-deck" "$HOME/.cache/claude-session-deck"
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}"/{JetBrains,Google}/*/claude-session-deck  # IDE plugins

"$(dirname "$(readlink -f "$0")")/restore-status-line.sh"

echo "claude-session-deck removed."
if gnome-extensions list 2>/dev/null | grep -qx "window-calls@domandoman.xyz"; then
    echo "The Window Calls GNOME extension was left in place. To remove it:"
    echo "  gnome-extensions uninstall window-calls@domandoman.xyz"
fi
if [ -e /etc/udev/rules.d/70-claude-session-deck.rules ]; then
    echo "The udev rule is still installed. To remove it:"
    echo "  sudo rm /etc/udev/rules.d/70-claude-session-deck.rules && sudo udevadm control --reload"
fi
