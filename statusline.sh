#!/bin/sh
# Claude Code status line command: saves the session JSON from stdin for the
# claude-session-deck daemon, then runs the status line that was configured before.
dir="$HOME/.claude/claude-session-deck"
input=$(cat)
# A session reports no usage limits until its first reply, so only input that
# has them may replace what an earlier session saved.
case $input in
    *'"five_hour"'* | *'"seven_day"'*)
        printf '%s' "$input" > "$dir/status.json.$$" && mv "$dir/status.json.$$" "$dir/status.json" ;;
esac
if [ -s "$dir/statusline-original" ]; then
    printf '%s' "$input" | sh -c "$(cat "$dir/statusline-original")"
fi
