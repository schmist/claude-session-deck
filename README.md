# claude-session-deck

Shows every running [Claude Code](https://claude.com/claude-code) session on an
Elgato Stream Deck: one key per session, with its live state. Pressing a key
jumps to the window that session runs in and, in JetBrains IDEs and Ptyxis, to
its terminal tab.

![Rendering of a Stream Deck with eight example sessions and the usage key](docs/preview.png)

*Rendering with example sessions, made by `docs/preview.py`. The keys are drawn
by the daemon's own code and shown pixel for pixel, at 72 × 72 px each.*

> **Work in progress.** Only the deck this was developed on, an Elgato Stream
> Deck MK.2 (15 keys, USB ID `0fd9:0080`), was tried for real. The
> [other models](#decks) were only tried on the [emulator](#emulated-decks).

Not affiliated with Anthropic or Elgato.

## What it does, and what it does not

It does:

- Show each interactive Claude Code session of your user on a key: on this
  machine, in containers and in Docker Sandboxes. See
  [How sessions are found and read](#how-sessions-are-found-and-read).
- Show per session its state, name, running tool, context size, model and the
  time in that state, and for the account its usage limits.
- Jump to a session's window when its key is pressed, on the
  [desktops](#jumping-to-a-session) it knows, and to its terminal tab in
  JetBrains IDEs and in Ptyxis.
- Let you [move a session to another key](#moving-a-session): hold its key,
  then press the key it is to be on.
- Drive several decks of different [models](#decks) at once, real or emulated.
- Take [plugins](#extending) for other places to find sessions, other
  desktops, other terminals and IDEs, other sandboxes and other deck models.

It does not:

- **Control a session.** It only reads. It cannot answer a permission prompt,
  send a prompt, or start, stop or interrupt a session.
- **Use the network.** It makes no request to Anthropic or anyone else and
  needs no login or API key; everything comes from files and processes on your
  machine.
- **See other sessions than those above**: not those on other machines or in
  the cloud, not other users', and not non-interactive runs such as `claude -p`
  or subagents. On this machine it looks in `~/.claude` only; a session started
  with another `CLAUDE_CONFIG_DIR` needs a [one-line plugin](#extending).
- **Show more sessions than a deck has keys.** The oldest are shown, the rest
  are left out, and there are no pages.
- **Select the tab or pane** in anything but a JetBrains IDE or Ptyxis. Elsewhere it
  raises the window, and may pick the wrong one of several: see the
  [limits](#which-window-is-chosen). Inside `tmux` or `screen` a session's
  processes do not lead to a terminal window at all, so the window can only be
  found by its title.
- **Share a deck.** It takes all keys of every deck it finds; there are no
  other actions, profiles or folders, and other Stream Deck software must not
  run next to it.
- **Run on anything but Linux** with systemd user services.
- **Promise to keep working.** It reads files that are internal to Claude Code
  and may change with any update; and of the decks only the MK.2, of the
  desktops only GNOME on Wayland and of the tools only IntelliJ IDEA and
  Ptyxis 50 were tried for real.

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

Keys fill left to right, top to bottom, oldest session first, unless you
[move a session](#moving-a-session). The bottom-right key is reserved, so a
deck shows one session fewer than it has keys: 14 on an MK.2. Every deck shows the same sessions, as many as it has room for. A deck
with a [screen next to its keys](#screens-dials-and-touch-keys) shows the
usage there and has all its keys for sessions.

**The usage key** (bottom right) shows the account's 5-hour and weekly usage
limits: percentage used, a bar and the time until reset. It turns amber above
65% and red above 85%. The figures come from Claude Code, so they only update
while a session is active. Until a session has replied since the computer
started the key reads `no data yet`, as it does for accounts without such
limits; a limit whose window has reset is left out until a session reports it
again.

### Moving a session

1. **Hold a session's key** for 0.6 s. The session is picked up: its key
   shrinks, bobs up and down and gets a pulsing frame, and every free key
   shows a `+`.
2. **Press the key it is to be on.** It stays on that key from then on, also
   when other sessions end.

| The key you press | What happens |
|---|---|
| A free key | The session is put there. Its old key is filled by the sessions after it moving up |
| Another session's key | The session takes that key. Coming from further right or below, the session that was there and those after it move on by one key, to the right and into the next row at the end of one. Coming from the left or above, they move up by one instead, as its old key became free |
| The usage key, or the session's own key | The session is put down where it was |
| No key for 10 s | The same |

The usage key in the bottom-right corner never moves and nothing is moved onto
it. A session that would be pushed off the last key for sessions gives up the
key it was put on and takes a free one, if there is one.

Sessions that were never moved keep filling the free keys in order, oldest
first, around the ones that were put somewhere. The keys are numbered left to
right, top to bottom, and the arrangement is the same on every deck: a session
on the 12th key of an MK.2 is on the 12th key of an XL too, in another row.
It is kept in `~/.cache/claude-session-deck/places.json`, so it survives a
restart of the daemon, and is forgotten for a session when that session ends.

Because a key can be held, a jump happens when the key is let go, not when it
goes down. While a session is picked up, any press, short or long, puts it
down.

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
- To jump to windows: a supported desktop, see [Jumping to a session](#jumping-to-a-session)
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

The script first checks what claude-session-deck needs on this system and
lists each thing as found or missing. For what is missing it says what does
not work without it and which package provides it, and offers to install that
package. Something required that is still missing ends the installation;
something optional is named again at the end, with the command that installs
it. `./install.sh --check` only does this check and changes nothing.

It asks before each step that changes the system, and uses `sudo` only for
packages and the udev rule:

- missing packages (Pillow, PyGObject, the AT-SPI bindings if Ptyxis is
  installed, `kdotool` on KDE Wayland, the Ubuntu font)
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

To update, pull and run `./install.sh` again: it replaces the daemon, rebuilds
the plugin of each IDE that has it, restarts the service and asks only about
what is not set up yet. Sessions keep the keys you moved them to. To remove,
run `./uninstall.sh`.

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

## How sessions are found and read

The daemon runs as your user and reads what your user may read anyway. It
never writes to a session, its files or its terminal.

| What | Read from | How often |
|---|---|---|
| Which sessions run, and each one's name, folder, state and what it waits for | The file Claude Code keeps per running session, `~/.claude/sessions/<pid>.json` | 4 times a second |
| Whether a session is still alive | `/proc/<pid>/stat`: the process must exist and have started when the file says | 4 times a second |
| Running tool, context size, model | The last 400 kB of the session's transcript, `~/.claude/projects/<folder>/<session id>.jsonl`, when it changed | 4 times a second |
| Account usage | `~/.claude/claude-session-deck/status.json`, saved by the status line command (below) | Once a second |
| The processes leading to a session's window | `/proc`: the session's parent processes | On a key press, and when a session starts or gets a prompt |

Of a transcript the daemon keeps the name of the running tool, the token
counts of the last reply and the model; the conversation itself is not kept,
shown or logged. What it writes is the window it remembered for each session,
in `~/.cache/claude-session-deck/windows.json`, the keys you moved sessions
to, in `places.json` next to it, and its log.

These files are internal to Claude Code, so an update can break
claude-session-deck.

**Where sessions are found.** Each place is a *source*; the daemon asks all of
them and shows what they find together:

| Source | Finds sessions | How |
|---|---|---|
| `Local` | On this machine | The session files in `~/.claude/sessions/` |
| `Containers` | In containers (Docker, Podman, …) | Every process named `claude` in another process namespace is looked up in `/proc`; its session file and transcript are read through `/proc/<pid>/root`, whether the container keeps its own `~/.claude` or mounts yours |
| `Sandboxes` | In virtual machines ([Docker Sandboxes](https://docs.docker.com/ai/sandboxes/), `sbx`) | The host sees nothing of them, so a script run inside each sandbox reports its sessions |

A session counts if its file says it is interactive, names a session ID and
belongs to a live process. Another source is [a plugin](#extending).

A container needs no setup, whatever started it, as long as its `claude`
process runs as your user.

In a virtual machine, the daemon starts a small shell script through the
sandbox's own command (`sbx exec`) in each running sandbox. Once a second it
reports the session files of live sessions and the end of a transcript that
grew; nothing is stored on the host. The script ends when the sandbox has no
session left or the daemon stops, and needs only `sh`, `stat`, `wc` and `tail` inside.
A sandbox that is stopped is not started.

**Sessions in the same folder** are labelled with the name Claude gave them.

## How the rest works

**Account usage.** Claude Code passes the account's usage limits to its status
line command. The installer registers `statusline.sh` as that command; it
saves the data for the daemon and then runs the status line you had before, if
any. The uninstaller restores the previous setting, and a copy of your earlier
settings is kept in `~/.claude/claude-session-deck/settings.json.before-claude-session-deck`.

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

Another model is [a plugin, or an entry in `MODELS`](#extending).

### Emulated decks

`emulator.py` stands in for decks that are not plugged in, to work on a model
without having it:

```sh
./emulator.py              # one deck of each model
./emulator.py xl mini mk2  # these three; a model may be given more than once
```

It prints the address of a page, `http://127.0.0.1:8317/` unless `--port` says
otherwise, that shows each deck to scale with what the daemon draws on it.
Click a key, dial or touch key to press it, keep the mouse button down to hold it, scroll over a dial to turn it, and
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

## Jumping to a session

A key press is handled in three steps, each by a part that can be exchanged:

| Step | Part | What it does |
|---|---|---|
| 1 | The session's *source* | Names the processes on this machine that lead to the session's window: its parent processes, up to the terminal or IDE |
| 2 | A *tool*, if one of these processes is one | Brings the session to the front inside that program: selects its tab. See [Tools](#tools) |
| 3 | The desktop's *backend* | Lists the windows, and raises the one that belongs to one of these processes |

A session without a tool still gets its window raised, and one whose window is
not found is reported in the log; neither stops the keys from being drawn.

**A session in a container or sandbox** has no parent process on the host
that leads to its window. What it reads and writes passes through a client
process in your terminal instead, such as `sbx run` or `docker run`. The
session's terminal inside and the client's terminal on the host were therefore
last typed into, and last written to, at the same moments; the daemon compares
these times to tell which client, and so which window and tab, belongs to
which session. This also holds for several sessions in one sandbox. If no
client matches, it takes the latest one started for that sandbox or project
folder.

### Desktops

Linux has no single way to focus another program's window, so claude-session-deck uses
the first of these backends that sees any windows:

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

### Which window is chosen

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

### Tools

Raising a window works for any terminal or IDE. For a tool the daemon knows,
it can do more:

| Tool | Extra | Needs |
|---|---|---|
| JetBrains IDEs (IntelliJ IDEA, PyCharm, Android Studio, …) | Selects the session's tab in the Terminal tool window and raises the right project window | The plugin from `jetbrains-plugin/`, the IDE's current terminal (not the Classic one) |
| Ptyxis (the "Terminal" of Ubuntu and Fedora) | Selects the session's tab | Up to Ptyxis 50: PyGObject with the AT-SPI bindings (`gir1.2-atspi-2.0` on Ubuntu) |

**Ptyxis** up to version 50 has no way to ask for the tab of a shell, so the
daemon uses what Ptyxis tells screen readers: the titles of a window's tabs,
and a step to the next or previous tab. Titles need not differ, so the daemon
writes a title of its own to the session's terminal, sees which tab takes it,
sets the old title back and steps to that tab, taking all steps at once so
that the tabs in between do not show. This takes about 0.2 s. The title is
written into what the session prints; should that ever fall into the middle of
something the session is printing, the terminal can show stray characters
once. Tried with Ptyxis 50.

A newer Ptyxis tells the shell of each tab the ID of that tab, in the
environment variable `PTYXIS_TAB`; at the time of writing only its development
version does. The daemon reads it from the shell's `/proc/<pid>/environ` and
asks Ptyxis for that tab over D-Bus (`focus-tab-by-uuid`). That takes a few
milliseconds, writes nothing to the terminal and needs no AT-SPI bindings.
Ptyxis does not tell which of its windows holds the tab, so with several
Ptyxis windows the window is [chosen](#which-window-is-chosen) as for a
terminal the daemon does not know. If Ptyxis cannot be asked, the log says why
and the daemon steps to the tab as above. Asking for a tab was tried on Ptyxis
50 with an ID obtained in another way; a Ptyxis that sets `PTYXIS_TAB` was not
tried.

**The JetBrains plugin** is built by `./install.sh` against each IDE it finds
(running, or installed through Toolbox, in `/opt` or as a snap), and is loaded
when the IDE next starts. It is written in Kotlin and built by Gradle, which
runs on the Java the IDE ships and compiles against that IDE, so no IDE is
downloaded. The first build does download Gradle, its plugins and the Kotlin
compiler. A new major IDE version has its own plugin directory, so run
`./install.sh` again after such an update. To build it by hand:

```sh
cd jetbrains-plugin
JAVA_HOME=<IDE home>/jbr ./gradlew installPlugin -Pide=<IDE home> -Pplugins=<plugins directory>
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

Another tool is [a plugin](#extending).

## Extending

Five parts of the daemon are lists of objects, and each object is a small
class. To add to one, write a subclass and append it to the list:

| To support another | Subclass | Add it to | It has to say |
|---|---|---|---|
| Place where sessions are found | `Source` | `SOURCES` | `sessions()`: the live sessions there. Optionally `info(session)`: tool, tokens and model, if they do not come from a transcript on this machine; `chain(session)`: the processes that lead to its window, if they are not its parents |
| Desktop, to list and raise windows | `Backend` | `BACKENDS` | `windows()`: the open windows with their process and title, or `None` on another desktop; `activate(id)`: raise one |
| Terminal or IDE, to select the tab | `Tool` | `TOOLS` | `runs(pid)`: whether that process is this tool; `focus(pid, chain)`: bring the session to the front inside it, and return its window's title if known |
| Sandbox that can run a command inside | `Sandbox` | `SANDBOXES` | `running()`: the names of the running ones; `command(name)`: the start of a command line that runs its remaining arguments inside, as `["sbx", "exec", "-i", name]` does |
| Stream Deck model | `Model` | `MODELS` | Its USB product IDs, columns, rows and key size; a subclass for another protocol, a screen or other controls, as `MiniModel` and `PlusModel` are |

The docstrings of these classes in `claude_session_deck.py` say exactly what
each method gets and returns; the classes next to them (`Local`, `Sway`,
`JetBrains`, `Sbx`) are short examples.

**Plugins.** The daemon runs every `*.py` file in
`~/.config/claude-session-deck/plugins/` when it starts, in alphabetical
order, so nothing in the repository has to be changed and `./install.sh` does
not overwrite it. A plugin imports the daemon as `claude_session_deck` and
changes its lists:

```python
# ~/.config/claude-session-deck/plugins/mine.py
import os
import subprocess

import claude_session_deck as csd


# Sessions started with CLAUDE_CONFIG_DIR=~/.claude-work
csd.SOURCES.append(csd.Local("~/.claude-work"))


class MyTerminal(csd.Tool):
    """Selects the session's tab in a terminal that can be told to."""

    name = "my terminal"

    def runs(self, pid):
        # Asked for each parent process of the session, nearest first.
        return csd.read_proc(pid, "comm").strip() == "my-terminal"

    def focus(self, pid, chain):
        # `chain` holds the session's parent processes; one of them is the
        # shell in the tab to select. Raise an exception if it fails: the
        # window is raised all the same.
        ...


csd.TOOLS.append(MyTerminal())


class MyDesktop(csd.Backend):
    """Lists and raises windows on a compositor the daemon does not know."""

    name = "my desktop"

    def windows(self):
        if "MYDESKTOP" not in os.environ.get("XDG_CURRENT_DESKTOP", ""):
            return None  # not this desktop: the next backend is asked
        return [{"id": w["id"], "pid": w["pid"], "title": w["title"], "focus": w["focused"]}
                for w in csd.run_json("my-desktop-msg", "windows", "--json")]

    def activate(self, wid):
        subprocess.run(["my-desktop-msg", "focus", str(wid)], timeout=3)


csd.BACKENDS.insert(0, MyDesktop())  # asked before the built-in ones
```

After a change, restart the daemon: `systemctl --user restart
claude-session-deck`. Its log names each plugin it loaded, and why one could
not be loaded; a source that fails is reported once and the other sources
carry on. `./uninstall.sh` leaves the plugins folder alone.

A plugin is ordinary Python that runs with the daemon's rights, which are
yours: only put files there that you would run yourself. The names it uses
from the daemon are not a stable interface yet and can change with an update.

What is not a list, such as what a key shows or what a press, turn or touch
does (`render()`, `act()`), is changed in the source.

For a new model, the emulator needs its counterpart, a subclass of `Firmware`
in `emulator.py`.

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
| `HOLD_SECONDS` | `0.6` | How long a key is held to pick its session up |
| `MOVE_SECONDS` | `10` | How long a session that was picked up waits for its new key |
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
| Usage key reads `no data yet` | The status line command is not set up (run `./install.sh` again), no session has replied since the computer started, or the account has no usage limits |
| A session is missing | It is not interactive, it uses another `CLAUDE_CONFIG_DIR`, or it runs where no [source](#how-sessions-are-found-and-read) looks: a container whose process is not yours, a kind of sandbox the daemon does not know, another machine. The deck may also have fewer keys than there are sessions |
| Log shows `cannot load plugin …` or `…: cannot look for sessions: …` | A [plugin](#extending) failed; the line says why |
