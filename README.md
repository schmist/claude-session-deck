# claude-session-deck

Shows every running [Claude Code](https://claude.com/claude-code) session on an
Elgato Stream Deck: one key per session, with its live state. Pressing a key
jumps to the window that session runs in and, in JetBrains IDEs, to its
terminal tab.

![Rendering of a Stream Deck with eight example sessions and the usage key](docs/preview.png)

*Rendering with example sessions, made by `docs/preview.py`. The keys are drawn
by the daemon's own code and shown pixel for pixel, at 72 × 72 px each.*

> **Work in progress.** Only the deck this was developed on, an Elgato Stream
> Deck MK.2 (15 keys, USB ID `0fd9:0080`), was tried for real. The
> [other models](#decks) were only tried on the [emulator](#emulated-decks).

Not affiliated with Anthropic or Elgato.

## What the keys show

| Part | Meaning |
|---|---|
| Top bar and colour | Session state (see below) |
| Name | Project folder, or the session name if you renamed it; a name too long for the key scrolls to its end each time it appears |
| Line under the name | While working: the running tool (`Bash`, `Edit`, …) or `thinking`. While waiting: the reason |
| Bar and number | Context used, in tokens, on a 1M scale; amber above 65%, red above 85% |
| Creature and time | How long the session has been in its current state |
| Model in the name's place, e.g. `Opus 5.5` | The session's model. It replaces the name, in a tinted colour, for 3 s every 9 s, on all keys at once |

| State | Colour | Animation |
|---|---|---|
| Working | Orange | A highlight sweeps along the top bar; every 25 s a large creature appears for 6 s |
| Needs you | Red | The key pulses and the creature waves |
| Done | Green | A slow glow and a jumping creature, until you press the key |
| Idle | Grey | None |

Keys fill left to right, top to bottom, oldest session first. The bottom-right
key is reserved, so a deck shows one session fewer than it has keys: 14 on an
MK.2. Every deck shows the same sessions, as many as it has room for. A deck
with a [screen next to its keys](#screens-dials-and-touch-keys) shows the
usage there and has all its keys for sessions.

**The usage key** (bottom right) shows the account's 5-hour and weekly usage
limits: percentage used, a bar and the time until reset. It turns amber above
65% and red above 85%. The figures come from Claude Code, so they only update
while a session is active; for accounts without such limits the key reads
`no data yet`.

**While the screen is locked** the keys show no sessions. All keys of a deck
together act as one large screen, on which the creature turns up now and then;
key presses do nothing.

**When the computer sleeps, or the daemon stops** (at logout and shutdown, too),
the deck goes dark: its keys are cleared and its light is switched off. A deck
often keeps its power then, and would go on showing what it showed last. It
lights up again when the computer wakes.

## Requirements

- Linux with systemd user services
- One or more Stream Decks of a [model it knows](#decks), or the [emulator](#emulated-decks)
- Python 3.8+ with Pillow
- To notice a locked screen or sleep: PyGObject and systemd's login manager
- To jump to windows: a supported desktop, see [Jumping to a window](#jumping-to-a-window)
- Optional: the Ubuntu font; the fallback font is wider, so more text gets shortened

Developed on Ubuntu with GNOME Shell 50 on Wayland and a Stream Deck MK.2;
other setups are untested.

Do not run other Stream Deck software at the same time: claude-session-deck takes over
all keys.

## Install

```sh
git clone https://github.com/schmist/claude-session-deck.git
cd claude-session-deck
./install.sh
```

The script asks before each step that changes the system, and uses `sudo` only
for packages and the udev rule:

- missing packages (Pillow, PyGObject, `kdotool` on KDE Wayland)
- the Window Calls extension on GNOME
- a status line command in `~/.claude/settings.json`, for the usage key
- a plugin for each JetBrains IDE found, to select the session's terminal tab
- a udev rule, if the deck is not accessible

`./install.sh --yes` agrees to all of them; without a terminal every question
counts as no. Everything else is installed for the current user only:

| What | Where |
|---|---|
| Daemon | `~/.local/share/claude-session-deck/claude_session_deck.py` |
| Service | `~/.config/systemd/user/claude-session-deck.service` |
| Status line script and its data | `~/.claude/claude-session-deck/` |
| Bundled GNOME extension, if Window Calls is not used | `~/.local/share/gnome-shell/extensions/claude-session-deck-windows@local/` |
| JetBrains plugin | `~/.local/share/JetBrains/<IDE and version>/claude-session-deck/` (Android Studio: under `Google/`) |

To update, pull and run `./install.sh` again. To remove, run `./uninstall.sh`.

**GNOME** needs a shell extension to focus windows. The script offers
[Window Calls](https://extensions.gnome.org/extension/4724/window-calls/), which
loads right away. If you decline, it installs a small bundled extension, which
GNOME only loads at login: log out and back in, then run
`gnome-extensions enable claude-session-deck-windows@local`. Until then, pressing a key
does nothing.

**Device access.** Your user must be allowed to write to the deck's
`/dev/hidraw*` device. Recent distributions grant this; otherwise the script
offers the udev rule. To install it by hand:

```sh
sudo cp udev/70-claude-session-deck.rules /etc/udev/rules.d/
sudo udevadm control --reload
sudo udevadm trigger --subsystem-match=hidraw
```

## How it works

**Session state.** Claude Code keeps one file per running session in
`~/.claude/sessions/<pid>.json` with its name, folder and status. The daemon
reads these four times a second and drops sessions whose process is gone. The
tool name and context size come from the end of the session's transcript in
`~/.claude/projects/`. These files are internal to Claude Code, so an update
can break claude-session-deck.

**Account usage.** Claude Code passes the account's usage limits to its status
line command. The installer registers `statusline.sh` as that command; it
saves the data for the daemon and then runs the status line you had before, if
any. The uninstaller restores the previous setting, and a copy of your earlier
settings is kept in `~/.claude/claude-session-deck/settings.json.before-claude-session-deck`.

**Sessions in sandboxes.** How a session is found depends on what the host
can see of its sandbox:

| Sandbox | What the host sees | How the session is found |
|---|---|---|
| Container (Docker, Podman, …) | The process, and its files under `/proc/<pid>/root` | The daemon reads the session file and transcript the container sees, whether it keeps its own `~/.claude` or mounts the host's |
| Virtual machine ([Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), `sbx`) | Nothing | A script run inside the sandbox reports its sessions |

A container needs no setup, whatever started it, as long as its `claude`
process runs as your user.

In a virtual machine, the daemon starts a small shell script through the
sandbox's own command (`sbx exec`) in each running sandbox. Once a second it
reports the session files of live sessions and the end of a transcript that
grew; nothing is stored on the host. The script ends when the sandbox has no
session left or the daemon stops, and needs only `sh`, `stat`, `wc` and `tail` inside.
A sandbox that is stopped is not started.

**The terminal of a sandboxed session.** Such a session has no parent process
on the host that leads to its window. What it reads and writes passes through
a client process in your terminal instead, such as `sbx run` or `docker run`.
The session's terminal inside the sandbox and the client's terminal on the
host were therefore last typed into, and last written to, at the same moments;
the daemon compares these times to tell which client, and so which window and
tab, belongs to which session. This also holds for several sessions in one
sandbox. If no client matches, it takes the latest one started for that
sandbox or project folder.

**Adding a kind of sandbox.** Anything that can run a command inside, such as
`ssh` to another machine, is a subclass of `Sandbox` in
`claude_session_deck.py`, listed in `SANDBOXES`: `running()` returns the names
of the running sandboxes and `command(name)` the start of a command line that
runs its remaining arguments inside one, as `["sbx", "exec", "-i", name]`
does.

**Sessions in the same folder** are labelled with the name Claude gave them.

**Locked screen and sleep.** The login manager (logind) tells the daemon over
D-Bus when a session of yours is locked and when the computer is about to
sleep. The daemon asks it to wait with sleeping until the decks are dark,
which takes a moment. Without PyGObject the keys keep showing the sessions.

**The deck.** The daemon writes images straight to the device and reads key
presses from it, without a Stream Deck library. See [Decks](#decks).

## Decks

The daemon uses every deck it finds, and looks for new ones every 2 seconds:
plug one in or unplug it at any time. Each deck is drawn for its own number of
keys and their size; a press on any of them jumps to the session.

| Model | Keys | Key size | Also has | Tried on |
|---|---|---|---|---|
| Stream Deck MK.2, Original V2 | 5 × 3 | 72 px | | A real MK.2 |
| Stream Deck XL | 8 × 4 | 96 px | | The emulator |
| Stream Deck + | 4 × 2 | 120 px | Touch screen of 800 × 100 px, 4 dials | The emulator |
| Stream Deck Neo | 4 × 2 | 96 px | Screen of 248 × 58 px, 2 touch keys | The emulator |
| Stream Deck Mini | 3 × 2 | 80 px | | The emulator |

The models that were only tried on the emulator are written from what other
projects document of their protocol, so a real one may not work yet. The
Original of 2017 (USB ID `0fd9:0060`) is not supported.

### Screens, dials and touch keys

The screen of the + and of the Neo is drawn like a key: the daemon sends it an
image, on the + in four parts so that only what changed is sent. It shows how
many sessions need you, are done and are working, and the account usage.

| Control | What it does |
|---|---|
| + screen | Four panels: needs you, done, working, usage |
| + dial, pushed | Jumps to the next session in the state of the panel above it; pushed again, to the one after |
| + screen, touched | The same as pushing the dial under that panel |
| + dial, turned | Brightness of that deck, 5% a step, until the daemon restarts |
| Neo screen | The three counts and the usage |
| Neo touch key | Jumps to the next session that needs you, or else to one that is done |

Not used: long touches and swipes on the + screen, and the lights of the Neo's
touch keys.

The drawing is laid out for a key of 72 px and scaled to other sizes, with
text drawn at the key's own size so that it stays sharp.

**Adding a model.** A model is an entry in `MODELS` in
`claude_session_deck.py`: its USB product IDs, columns, rows, key size and how
an image is turned before it is sent. One with another protocol, a screen or
other controls is a subclass of `Model`, as `MiniModel` and `PlusModel` are;
its counterpart for the emulator is a subclass of `Firmware` in `emulator.py`.
What a press, turn or touch does is decided in `act()`.

### Emulated decks

`emulator.py` stands in for decks that are not plugged in, to work on a model
without having it:

```sh
./emulator.py              # one deck of each model
./emulator.py xl mini mk2  # these three; a model may be given more than once
```

It prints the address of a page, `http://127.0.0.1:8317/` unless `--port` says
otherwise, that shows each deck to scale with what the daemon draws on it.
Click a key, dial or touch key to press it, scroll over a dial to turn it, and
click the screen of the + to touch it. The running service picks emulated
decks up like real ones, next to those that are plugged in, and drops them
when the emulator ends.

An emulated deck is a socket in `$XDG_RUNTIME_DIR/claude-session-deck/`, which
only your user can reach. The daemon sends it the same reports as the real
model, and the emulator decodes them as that model would: it checks the report
headers, the order of an image's parts, the image format and its size, and
turns the image back the way the model's screen is mounted. What a deck does
not understand shows as a protocol error on the page and in the emulator's
output. The emulator knows the protocols from the same sources as the daemon,
so it catches the daemon's mistakes, not wrong assumptions about a model.

Like a real deck, an emulated one serves a single program: while the service
uses it, a second daemon started from the source folder gets none. To try
changed code, stop the service first (`systemctl --user stop
claude-session-deck`) or install the change with `./install.sh`.

## Jumping to a window

Linux has no single way to focus another program's window, so claude-session-deck uses
the first of these mechanisms that sees any windows:

| Desktop | Mechanism | Needs |
|---|---|---|
| GNOME | [Window Calls](https://github.com/ickyicky/window-calls) extension, over D-Bus | GNOME Shell 45+, `python3-gi` |
| GNOME, without Window Calls | Bundled extension, over D-Bus | GNOME Shell 45+, `python3-gi`, one logout and login |
| Sway | `swaymsg` | `SWAYSOCK` visible to the service |
| Hyprland | `hyprctl` | `HYPRLAND_INSTANCE_SIGNATURE` visible to the service |
| KDE Plasma on Wayland | [`kdotool`](https://github.com/jinliu/kdotool) | `kdotool` installed |
| Any X11 window manager | EWMH `_NET_ACTIVE_WINDOW` via libX11 | Nothing extra |

Only GNOME 50 on Wayland with Window Calls and the X11 window listing have
been tested. On other Wayland compositors only XWayland windows can be focused.

If Sway or Hyprland does not pass its environment to the service, add this to
the compositor's startup:

```sh
systemctl --user import-environment SWAYSOCK HYPRLAND_INSTANCE_SIGNATURE WAYLAND_DISPLAY DISPLAY
```

Both GNOME extensions add D-Bus calls that any program running as your user
can use. The bundled one can only list and activate windows; Window Calls, a
third-party project, can also move, resize and close them.

**Which window is chosen:**

1. The window the session's tool reports, if the tool is
   [supported](#tools).
2. The window that had the focus when the session started or last got a
   prompt, if it belongs to the session's terminal or IDE. This is remembered
   in `~/.cache/claude-session-deck/windows.json`.
3. Otherwise a window owned by a parent process of the session, preferring one
   with the project folder name in its title.

Limits, where the session's tool is not supported:

- The window is raised, but the right tab inside it is not selected.
- Until a session gets a prompt while the daemon runs, several windows of one
  program can only be told apart by title, so the wrong one may be raised.
- A session that starts working on its own (a scheduled task, for example) is
  remembered for whichever window of its program has the focus, until your
  next prompt.

## Tools

Raising a window works for any terminal or IDE. For a tool the daemon knows,
it can do more:

| Tool | Extra | Needs |
|---|---|---|
| JetBrains IDEs (IntelliJ IDEA, PyCharm, Android Studio, …) | Selects the session's tab in the Terminal tool window and raises the right project window | The plugin from `jetbrains-plugin/`, the IDE's current terminal (not the Classic one) |

**The JetBrains plugin** is built by `./install.sh` against each IDE it finds
(running, or installed through Toolbox, in `/opt` or as a snap), using the
Java compiler the IDE ships, and is loaded when the IDE next starts. A new
major IDE version has its own plugin directory, so run `./install.sh` again
after such an update. To build it by hand:

```sh
jetbrains-plugin/build.sh <IDE home> <plugins directory>
```

The plugin listens on the socket
`$XDG_RUNTIME_DIR/claude-session-deck/jetbrains-<IDE process ID>.sock`, which
only your user can reach. The daemon sends it the session's parent processes;
the plugin selects the tab whose shell is one of them and replies with the
title of that project's window. It uses the terminal's programming interface,
which JetBrains may change between versions; the key then still raises the
window and the daemon's log says why the tab was not selected.

Developed against IntelliJ IDEA 2026.3; other IDEs are untested. Terminals opened as an editor tab
are not selected.

**Adding a tool.** A tool is a subclass of `Tool` in `claude_session_deck.py`,
listed in `TOOLS`. `runs(pid)` tells whether a process is that tool, and is
asked for each parent process of the session; `focus(pid, chain)` brings the
session to the front inside it and may return the title of its window.

## Configuration

The settings are constants at the top of `claude_session_deck.py`; change them and run
`./install.sh` again.

| Constant | Default | Meaning |
|---|---|---|
| `BRIGHTNESS` | `60` | Key brightness in percent |
| `FPS` | `12` | Frames per second while something is animating |
| `CONTEXT_WINDOW` | `1_000_000` | Token count at which the context bar is full |
| `SAFE_BOTTOM` | `9` | Bottom pixels left empty because the keycap rim hides them, of a key 72 px high; scaled for other models |
| `MASCOT_EVERY` | `25` | Seconds between appearances of the large creature |
| `MASCOT_SECONDS` | `6` | How long it stays |
| `LOCK_EVERY`, `LOCK_SECONDS` | `16`, `12` | While the screen is locked, the creature appears every 16 s for 12 s |
| `MODEL_EVERY`, `MODEL_SECONDS` | `9`, `3` | The model replaces the name for 3 s every 9 s |
| `MASCOT_ANIMATIONS` | walk 35, think 25, peek 20, wave 20 | Relative probability of each animation |
| `FONT` | Ubuntu variable font | Font file used for all text |

To see how much your keycaps hide, stop the service and run
`python3 claude_session_deck.py --calibrate`. Every key of every deck shows nested frames, each
3 pixels wide: white outermost, then red, yellow, green, blue. The outermost
colour visible on a side tells how much is hidden there.

## Troubleshooting

```sh
systemctl --user status claude-session-deck     # is it running?
journalctl --user -u claude-session-deck -f     # log
```

| Symptom | Cause and fix |
|---|---|
| Deck stays dark, log shows `Permission denied` | No access to the device: see Device access under [Install](#install) |
| Deck stays dark, nothing in the log | No supported deck found: check `lsusb` for vendor `0fd9`; it must be one of the [models](#decks) the daemon knows |
| Keys flicker or show other content | Another Stream Deck program is running; stop it |
| Pressing a key does nothing, log shows both extensions as `not loaded` | Install Window Calls, or log out and in and run `gnome-extensions enable claude-session-deck-windows@local` |
| Log shows `no window found for …` | The line lists each mechanism tried; none applies to your desktop, or no window matches the session |
| The wrong terminal tab stays selected in a JetBrains IDE | The plugin is not loaded: run `./install.sh`, then restart the IDE. If the log shows `JetBrains IDE: cannot focus …`, see the IDE's own log (Help → Show Log) |
| Usage key reads `no data yet` | The status line command is not set up (run `./install.sh` again), no session has been active since, or the account has no usage limits |
| A session is missing | It is not interactive, or it runs in a sandbox the daemon cannot look into: a container whose process is not yours, or a [kind of sandbox](#how-it-works) it does not know |
