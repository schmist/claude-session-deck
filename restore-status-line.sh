#!/usr/bin/env bash
# Earlier versions of claude-session-deck took the account usage from a Claude
# Code status line command of their own. Give Claude Code its previous status
# line back and remove that command's folder. The folder stays if that fails,
# as the settings may still refer to it.
[ -d "$HOME/.claude/claude-session-deck" ] || exit 0
if python3 - "$HOME/.claude/settings.json" "$HOME/.claude/claude-session-deck" <<'PY'
import json, os, sys
path, hook_dir = sys.argv[1:3]
try:
    settings = json.load(open(path))
except FileNotFoundError:
    sys.exit()
if (settings.get("statusLine") or {}).get("command") != "~/.claude/claude-session-deck/statusline.sh":
    sys.exit()
try:
    original = open(os.path.join(hook_dir, "statusline-original")).read().strip()
except OSError:
    original = ""
if original:
    settings["statusLine"]["command"] = original
else:
    del settings["statusLine"]
with open(path + ".tmp", "w") as out:
    json.dump(settings, out, indent=2)
    out.write("\n")
os.replace(path + ".tmp", path)
PY
then
    rm -rf "$HOME/.claude/claude-session-deck"
else
    echo "warning: could not read or change ~/.claude/settings.json; if its statusLine runs" >&2
    echo "  ~/.claude/claude-session-deck/statusline.sh, change that, then delete that folder." >&2
fi
