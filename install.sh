#!/usr/bin/env bash
# Install claude-session-deck for the current user. Safe to run again to update.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
app_dir="$HOME/.local/share/claude-session-deck"
unit_dir="$HOME/.config/systemd/user"
ext_uuid="claude-session-deck-windows@local"
ext_dir="$HOME/.local/share/gnome-shell/extensions/$ext_uuid"
python=/usr/bin/python3  # the interpreter the service runs with, so the checks below hold for it

info() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mwarning:\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -ne 0 ] || die "run this as your normal user, not as root"

# --- helpers -----------------------------------------------------------------
assume_yes=false
check_only=false
for arg in "$@"; do
    case "$arg" in
        -y|--yes) assume_yes=true ;;
        -c|--check) check_only=true ;;
        -h|--help)
            echo "usage: ./install.sh [--yes | --check]"
            echo "  --yes    answer yes to every question (packages, extension, IDE plugin, udev rule)"
            echo "  --check  only tell what is missing; nothing is installed or changed"
            exit 0 ;;
        *) die "unknown option: $arg" ;;
    esac
done

# ask "question": true if the user agrees; without a terminal that needs --yes
ask() {
    $assume_yes && return 0
    [ -t 0 ] || return 1
    local answer
    read -r -p "$1 [Y/n] " answer
    [[ ! "$answer" =~ ^[Nn] ]]
}

pkg_manager=""
for candidate in apt-get dnf pacman zypper; do
    if command -v "$candidate" >/dev/null; then pkg_manager=$candidate; break; fi
done

# pkg_name <apt> <dnf> <pacman> <zypper>: the name for this system's manager
pkg_name() {
    case "$pkg_manager" in
        apt-get) echo "$1" ;; dnf) echo "$2" ;; pacman) echo "$3" ;; zypper) echo "$4" ;;
    esac
}

# pkg_install <what it is> <package>: offer to install a system package
pkg_install() {
    local what=$1 package=$2
    [ -n "$pkg_manager" ] && [ -n "$package" ] || return 1
    ask "Install $what (package '$package', uses sudo)?" || return 1
    case "$pkg_manager" in
        apt-get) sudo apt-get install -y "$package" ;;
        dnf)     sudo dnf install -y "$package" ;;
        pacman)  sudo pacman -S --needed --noconfirm "$package" ;;
        zypper)  sudo zypper --non-interactive install "$package" ;;
    esac
}

is_gnome() { command -v gnome-shell >/dev/null && [[ "${XDG_CURRENT_DESKTOP:-}" == *GNOME* ]]; }
is_kde_wayland() { [[ "${XDG_CURRENT_DESKTOP:-}" == *KDE* && "${XDG_SESSION_TYPE:-}" == wayland ]]; }
# Windows are focused through X11 where no other way applies.
uses_x11() { ! is_gnome && ! is_kde_wayland && [ -z "${SWAYSOCK:-}${HYPRLAND_INSTANCE_SIGNATURE:-}" ]; }

# --- dependencies ------------------------------------------------------------
font=$(sed -n 's/^FONT = "\(.*\)"$/\1/p' "$here/claude_session_deck.py")  # the font the daemon draws with

has_systemd() { command -v systemctl; }
has_python() { [ -x "$python" ] && "$python" -c 'import sys; sys.exit(sys.version_info < (3, 8))'; }
has_pillow() { "$python" -c 'from PIL import ImageFont; ImageFont.Layout'; }  # Layout: Pillow 9.1
has_gi() { "$python" -c 'import gi; gi.require_version("Gio", "2.0")'; }
has_atspi() { "$python" -c 'import gi; gi.require_version("Atspi", "2.0"); from gi.repository import Atspi'; }
has_x11() { "$python" -c 'import ctypes.util, sys; sys.exit(not ctypes.util.find_library("X11"))'; }
has_gdbus() { command -v gdbus; }
has_kdotool() { command -v kdotool; }
has_font() { [ -f "$font" ]; }
has_claude() { command -v claude || [ -d "$HOME/.claude" ]; }

# What is needed here, each as: required or optional | its test | what it is | what does not work without it | package
gi_loss="a locked screen and sleep are not noticed, so the keys keep showing the sessions then"
! is_gnome || gi_loss+="; keys cannot focus windows"
deps=(
    "required|has_systemd|systemd with user services (systemctl)|the daemon runs as a user service|"
    "required|has_python|Python 3.8 or newer, as $python|the daemon is written in it, and the service runs it with this interpreter|$(pkg_name python3 python3 python python3)"
    "required|has_pillow|Pillow 9.1 or newer, the Python imaging library|the keys cannot be drawn|$(pkg_name python3-pil python3-pillow python-pillow python3-Pillow)"
    "optional|has_gi|PyGObject|$gi_loss|$(pkg_name python3-gi python3-gobject python-gobject python3-gobject)"
)
! is_gnome || deps+=("optional|has_gdbus|gdbus|the Window Calls extension cannot be set up, so the bundled one is used, which needs a logout|$(pkg_name libglib2.0-bin glib2 glib2 glib2-tools)")
! is_kde_wayland || deps+=("optional|has_kdotool|kdotool|keys show sessions but cannot focus windows on KDE Wayland; without a package: cargo install kdotool|kdotool")
! uses_x11 || deps+=("optional|has_x11|libX11|keys show sessions but cannot focus windows|$(pkg_name libx11-6 libX11 libx11 libX11-6)")
! command -v ptyxis >/dev/null || deps+=("optional|has_atspi|the AT-SPI bindings for PyGObject|the session's tab is not selected in Ptyxis, only its window is raised|$(pkg_name gir1.2-atspi-2.0 at-spi2-core at-spi2-core typelib-1_0-Atspi-2_0)")
deps+=(
    "optional|has_font|the Ubuntu font ($font)|a wider font is used, so more text on the keys is shortened; FONT in claude_session_deck.py names the file|$(pkg_name fonts-ubuntu "" "" "")"
    "optional|has_claude|Claude Code|there are no sessions to show|"
)

# check_deps [quiet]: sets `missing` to the lines of `deps` whose test fails, and tells each one's state
check_deps() {
    local line kind test what loss package
    missing=()
    for line in "${deps[@]}"; do
        IFS='|' read -r kind test what loss package <<<"$line"
        if "$test" >/dev/null 2>&1; then
            [ "${1:-}" = quiet ] || printf '    \033[32mfound\033[0m    %s\n' "$what"
        else
            missing+=("$line")
            [ "${1:-}" = quiet ] || printf '    \033[1;33mmissing\033[0m  %s (%s)\n' "$what" "$kind"
        fi
    done
}

# tell_missing: what is still missing, what that costs and how to get it; sets `missing_required`
tell_missing() {
    local line kind test what loss package packages=()
    missing_required=false
    for line in "${missing[@]}"; do
        IFS='|' read -r kind test what loss package <<<"$line"
        printf '    \033[1;33m%s\033[0m, %s\n        without it: %s\n' "$what" "$kind" "$loss"
        if [ -n "$package" ]; then packages+=("$package"); else echo "        no package is known for it on this system"; fi
        [ "$kind" != required ] || missing_required=true
    done
    [ ${#packages[@]} -eq 0 ] || case "$pkg_manager" in
        apt-get) echo "    To install: sudo apt-get install ${packages[*]}" ;;
        dnf)     echo "    To install: sudo dnf install ${packages[*]}" ;;
        pacman)  echo "    To install: sudo pacman -S ${packages[*]}" ;;
        zypper)  echo "    To install: sudo zypper install ${packages[*]}" ;;
    esac
}

info "Checking what claude-session-deck needs here"
check_deps
if ! $check_only; then
    for line in "${missing[@]}"; do
        IFS='|' read -r kind test what loss package <<<"$line"
        pkg_install "$what (without it: $loss)" "$package" || true
    done
    [ ${#missing[@]} -eq 0 ] || check_deps quiet
fi
if [ ${#missing[@]} -eq 0 ]; then
    info "Nothing is missing"
else
    warn "missing:"
    tell_missing
    ! $missing_required || die "something required is missing: install it and run this again"
fi
if $check_only; then
    exit 0
fi

# --- files -------------------------------------------------------------------
info "Installing the daemon to $app_dir"
install -Dm755 "$here/claude_session_deck.py" "$app_dir/claude_session_deck.py"
install -Dm644 "$here/systemd/claude-session-deck.service" "$unit_dir/claude-session-deck.service"

# --- GNOME: an extension is needed to focus windows ---------------------------
# "Window Calls" is preferred: GNOME loads it right away, the bundled one only at login.
wc_uuid="window-calls@domandoman.xyz"
window_calls_loaded() {
    gdbus call --session --dest org.gnome.Shell \
        --object-path /org/gnome/Shell/Extensions/Windows \
        --method org.gnome.Shell.Extensions.Windows.List >/dev/null 2>&1
}

extension=none
if is_gnome; then
    if ! window_calls_loaded &&
        ask "Install the 'Window Calls' GNOME extension from extensions.gnome.org (needed to focus windows)?"; then
        info "Confirm the dialog GNOME shows now"
        gdbus call --session --timeout 180 --dest org.gnome.Shell.Extensions \
            --object-path /org/gnome/Shell/Extensions \
            --method org.gnome.Shell.Extensions.InstallRemoteExtension "$wc_uuid" >/dev/null 2>&1 || true
        sleep 1
    fi
    if window_calls_loaded; then
        extension=window-calls
        rm -rf "$ext_dir"  # the bundled extension from an earlier install is not needed
    else
        info "Installing the bundled GNOME Shell extension instead"
        install -Dm644 -t "$ext_dir" "$here/gnome-extension/$ext_uuid/"*
        if gnome-extensions enable "$ext_uuid" 2>/dev/null; then
            extension=enabled
        else
            extension=pending
        fi
    fi
fi

# --- account usage: Claude Code status line ----------------------------------
# statusline.sh saves the usage limits Claude Code passes to its status line command.
hook_dir="$HOME/.claude/claude-session-deck"
install -Dm755 "$here/statusline.sh" "$hook_dir/statusline.sh"
# status_line_setup check|apply: prints configured, add, replace or unreadable
status_line_setup() {
    "$python" - "$HOME/.claude/settings.json" "$hook_dir" "$1" <<'PY'
import json, os, sys
path, hook_dir, mode = sys.argv[1:4]
ours = "~/.claude/claude-session-deck/statusline.sh"
try:
    settings = json.load(open(path))
except FileNotFoundError:
    settings = {}
except ValueError:
    print("unreadable"); sys.exit()
current = (settings.get("statusLine") or {}).get("command")
if current == ours:
    print("configured"); sys.exit()
if mode == "check":
    print("replace" if current else "add"); sys.exit()
if os.path.exists(path):
    with open(os.path.join(hook_dir, "settings.json.before-claude-session-deck"), "w") as backup:
        backup.write(open(path).read())
original = os.path.join(hook_dir, "statusline-original")
if current:  # keep the existing status line working behind ours
    with open(original, "w") as out:
        out.write(current)
elif os.path.exists(original):  # left by an earlier installation
    os.remove(original)
settings["statusLine"] = {**(settings.get("statusLine") or {}), "type": "command", "command": ours}
os.makedirs(os.path.dirname(path), exist_ok=True)
with open(path + ".tmp", "w") as out:
    json.dump(settings, out, indent=2)
    out.write("\n")
os.replace(path + ".tmp", path)
print("configured")
PY
}

status_line=$(status_line_setup check)
case "$status_line" in
    add)     question="Show account usage on the bottom-right key? This adds a status line command to ~/.claude/settings.json" ;;
    replace) question="Show account usage on the bottom-right key? This wraps your existing Claude Code status line command in ~/.claude/settings.json" ;;
    *)       question="" ;;
esac
if [ -n "$question" ] && ask "$question"; then
    status_line=$(status_line_setup apply)
fi
case "$status_line" in
    configured) : ;;
    unreadable) warn "~/.claude/settings.json is not valid JSON; the usage key will show 'no data yet'" ;;
    *)          warn "status line not configured; the usage key will show 'no data yet'" ;;
esac

# --- JetBrains IDEs: select the session's terminal tab ------------------------
# The plugin is built against each IDE found, by Gradle running on the Java that IDE ships.
# build_plugin <IDE home> <plugins directory>
build_plugin() (
    cd "$here/jetbrains-plugin"
    [ ! -x "$1/jbr/bin/java" ] || export JAVA_HOME="$1/jbr"
    ./gradlew --quiet installPlugin -Pide="$1" -Pplugins="$2"
)

jetbrains_homes() {
    {
        for exe in /proc/[0-9]*/exe; do readlink "$exe" 2>/dev/null; done | sed -n 's|/bin/[^/]*$||p'
        ls -d "$HOME"/.local/share/JetBrains/Toolbox/apps/* /opt/* /snap/*/current 2>/dev/null
    } | sort -u | while read -r home; do
        if [ -f "$home/product-info.json" ] && [ -d "$home/plugins/terminal" ]; then echo "$home"; fi
    done || true
}

jetbrains_ides=""
while read -r ide_home <&3; do
    # Plugins live in <vendor>/<product and version>, e.g. JetBrains/IntelliJIdea2026.3
    read -r ide_data ide_name < <("$python" -c '
import json, sys
info = json.load(open(sys.argv[1]))
print(info["productVendor"] + "/" + info["dataDirectoryName"], info["name"])' "$ide_home/product-info.json") || continue
    plugins_dir="${XDG_DATA_HOME:-$HOME/.local/share}/$ide_data"
    if [ -e "$plugins_dir/claude-session-deck" ] ||
        ask "Select the session's terminal tab in $ide_name? This builds a plugin and installs it to $plugins_dir"; then
        if build_plugin "$ide_home" "$plugins_dir"; then
            jetbrains_ides+="${jetbrains_ides:+, }$ide_name"
        else
            warn "could not build the plugin for $ide_name: its terminal may be too old, or Gradle could not download what it needs"
        fi
    fi
done 3< <(jetbrains_homes)

# --- device access -----------------------------------------------------------
deck=""
for dev in /sys/class/hidraw/hidraw*; do
    if grep -qi 'HID_ID=[0-9A-F]*:00000FD9:' "$dev/device/uevent" 2>/dev/null; then
        deck="/dev/$(basename "$dev")"
        break
    fi
done
if [ -z "$deck" ]; then
    warn "no Stream Deck is plugged in right now; the service will pick it up when it appears"
    warn "if it then stays dark, install the udev rule: see 'Device access' in the README"
elif [ ! -w "$deck" ]; then
    warn "$deck is not writable by $USER"
    if ask "Install a udev rule to grant access (uses sudo)?"; then
        sudo install -m644 "$here/udev/70-claude-session-deck.rules" /etc/udev/rules.d/70-claude-session-deck.rules
        sudo udevadm control --reload
        sudo udevadm trigger --subsystem-match=hidraw
        info "udev rule installed"
    else
        warn "skipped; see 'Device access' in the README"
    fi
fi

# --- service -----------------------------------------------------------------
info "Starting the service"
systemctl --user daemon-reload
systemctl --user enable claude-session-deck.service >/dev/null
systemctl --user restart claude-session-deck.service

echo
info "Done. Logs: journalctl --user -u claude-session-deck -f"
if [ ${#missing[@]} -gt 0 ]; then
    warn "it runs, but this is still missing:"
    tell_missing
fi
case "$extension" in
    pending)
        echo "    Window Calls is not installed, so the bundled extension is used. One step"
        echo "    left: log out and back in (GNOME only loads it at login), then run:"
        echo "      gnome-extensions enable $ext_uuid"
        echo "    Until then the keys show your sessions but pressing them does nothing."
        echo "    Or skip the logout: install 'Window Calls' from extensions.gnome.org."
        ;;
    window-calls)
        echo "    Press a key to jump to that session's window (through Window Calls)."
        ;;
    enabled)
        echo "    Press a key to jump to that session's window."
        ;;
esac
if [ -n "$jetbrains_ides" ]; then
    echo "    The plugin that selects a session's terminal tab is loaded when the IDE"
    echo "    starts; restart if it was new or updated: $jetbrains_ides"
fi
