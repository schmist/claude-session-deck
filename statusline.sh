#!/bin/sh
# Claude Code status line command: saves the session JSON from stdin for the
# claude-session-deck daemon, then runs the status line that was configured before.
dir="$HOME/.claude/claude-session-deck"
input=$(cat)
printf '%s' "$input" > "$dir/status.json.$$" && mv "$dir/status.json.$$" "$dir/status.json"
if [ -s "$dir/statusline-original" ]; then
    printf '%s' "$input" | sh -c "$(cat "$dir/statusline-original")"
fi
