#!/usr/bin/env python3
"""Show one Stream Deck key per running Claude Code session; a key press focuses its window."""
import collections
import ctypes
import ctypes.util
import fcntl
import glob
import io
import itertools
import json
import math
import os
import re
import random
import runpy
import select
import shutil
import signal
import socket
import stat
import struct
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

CLAUDE_DIR = os.path.expanduser("~/.claude")  # where Claude Code keeps its session files and transcripts
PLUGINS_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
                           "claude-session-deck", "plugins")           # see load_plugins()
STATUS_FILE = os.path.expanduser("~/.claude/claude-session-deck/status.json")   # written by statusline.sh
WINDOWS_FILE = os.path.expanduser("~/.cache/claude-session-deck/windows.json")  # see learn_window()
PLACES_FILE = os.path.expanduser("~/.cache/claude-session-deck/places.json")    # see Sessions.place()
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                           "claude-session-deck")                # sockets of TOOLS and of emulated decks
ELGATO_VENDOR = 0x0FD9
UNIT = 72                   # key size in pixels that the drawing is laid out for; other sizes are scaled from it
BRIGHTNESS = 60             # percent
FPS = 12                    # while something is animating
CONTEXT_WINDOW = 1_000_000  # tokens at which the context bar is full
SAFE_BOTTOM = 9             # bottom pixels hidden by the keycap rim, of a key UNIT pixels high
MASCOT_EVERY = 25           # seconds between mascot appearances while working
MASCOT_SECONDS = 6          # how long it stays
LOCK_EVERY = 16             # seconds between mascot appearances while the screen is locked
LOCK_SECONDS = 12           # how long it stays then
HOLD_SECONDS = 0.6          # how long a key is held to pick its session up, to put it on another key
MOVE_SECONDS = 10           # how long a session that was picked up waits for its new key
NAME_EVERY = 9              # seconds between the scrolls of a name too long for its key
FONT = "/usr/share/fonts/truetype/ubuntu/Ubuntu[wdth,wght].ttf"
FALLBACK_FONT = "sans:bold"  # fontconfig pattern, used if FONT is missing


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- device ---

class Model:
    """A kind of Stream Deck: its keys, and how it takes images and reports presses.

    This class speaks the protocol of the Original V2 and the models after it."""

    image_format = "JPEG"
    keys_at = 4        # where a report of the keys holds their states
    screen = None      # (width, height) of the screen next to the keys, if there is one
    screen_turns = ()  # how an image for it is turned before it is sent
    panels = 1         # parts of equal width that the screen is drawn and sent in

    def __init__(self, id, name, products, cols, rows, size,
                 turns=(Image.Transpose.ROTATE_180,), key_report=b"\x01"):
        self.id, self.name, self.products = id, name, products  # id: short name, for emulator.py
        self.cols, self.rows, self.keys = cols, rows, cols * rows
        self.size = size              # pixels along a key's side
        self.turns = turns            # how an image is turned before it is sent
        self.key_report = key_report  # what a report of the keys starts with; others are ignored

    def brightness(self, percent):
        """The feature report that sets the brightness."""
        return bytes([0x03, 0x08, percent]).ljust(32, b"\0")

    def image_reports(self, key, data):
        """The output reports that draw the encoded image `data` on `key`."""
        page = 0
        while data:
            chunk, data = data[:1016], data[1016:]
            header = bytes([0x02, 0x07, key, 0 if data else 1,
                            len(chunk) & 0xFF, len(chunk) >> 8, page & 0xFF, page >> 8])
            yield (header + chunk).ljust(1024, b"\0")
            page += 1

    def read(self, report):
        """What an input report tells, by control: {"key": the state of each key}.

        Models with more than keys also tell "push" (the state of each dial),
        "turn" (the steps each dial was turned by), "touch" (where the screen
        was touched) and "pad" (the state of each touch key)."""
        states = [bool(b) for b in report[self.keys_at:self.keys_at + self.keys]]
        if len(states) < self.keys or not report.startswith(self.key_report):
            return {}
        return {"key": states}


class PlusModel(Model):
    """The +, with four dials under a touch screen as wide as the deck."""

    screen, panels = (800, 100), 4

    def read(self, report):
        if report[:2] == b"\x01\x03" and len(report) >= 9:  # the dials
            if report[4] == 1:
                return {"turn": [b - 256 if b > 127 else b for b in report[5:9]]}
            return {"push": [bool(b) for b in report[5:9]]}
        if report[:2] == b"\x01\x02" and len(report) >= 10:  # a touch
            return {"touch": (report[6] | report[7] << 8, report[8] | report[9] << 8)}
        return super().read(report)

    def screen_reports(self, data, box):
        """The output reports that draw the encoded image `data` in the part `box` of the screen."""
        page = 0
        while data:
            chunk, data = data[:1008], data[1008:]
            header = struct.pack("<BBHHHHBHHB", 0x02, 0x0C, box[0], box[1], box[2] - box[0], box[3] - box[1],
                                 0 if data else 1, page, len(chunk), 0)
            yield (header + chunk).ljust(1024, b"\0")
            page += 1


class NeoModel(Model):
    """The Neo, with a small screen between two touch keys."""

    screen = (248, 58)
    screen_turns = (Image.Transpose.ROTATE_180,)

    def read(self, report):
        found = super().read(report)
        if found:
            found["pad"] = [bool(b) for b in report[self.keys_at + self.keys:self.keys_at + self.keys + 2]]
        return found

    def screen_reports(self, data, box):
        page = 0
        while data:
            chunk, data = data[:1016], data[1016:]
            header = bytes([0x02, 0x0B, 0, 0 if data else 1,
                            len(chunk) & 0xFF, len(chunk) >> 8, page & 0xFF, page >> 8])
            yield (header + chunk).ljust(1024, b"\0")
            page += 1


class MiniModel(Model):
    """The Mini, which takes uncompressed images and has reports of its own."""

    image_format = "BMP"
    keys_at = 1

    def brightness(self, percent):
        return bytes([0x05, 0x55, 0xAA, 0xD1, 0x01, percent]).ljust(17, b"\0")

    def image_reports(self, key, data):
        page = 0
        while data:
            chunk, data = data[:1008], data[1008:]
            header = bytes([0x02, 0x01, page, 0, 0 if data else 1, key + 1]).ljust(16, b"\0")
            yield (header + chunk).ljust(1024, b"\0")
            page += 1


# Only the MK.2 was tried on a real deck. The others are written from what
# other projects document of their protocol, and were only tried on
# emulator.py.
MODELS = [
    Model("mk2", "Stream Deck MK.2", {0x006D, 0x0080, 0x00A5}, 5, 3, 72),  # and the Original V2
    Model("xl", "Stream Deck XL", {0x006C, 0x008F}, 8, 4, 96),
    PlusModel("plus", "Stream Deck +", {0x0084}, 4, 2, 120, turns=(), key_report=b"\x01\x00"),
    NeoModel("neo", "Stream Deck Neo", {0x009A}, 4, 2, 96),
    MiniModel("mini", "Stream Deck Mini", {0x0063, 0x0090}, 3, 2, 80,
              turns=(Image.Transpose.ROTATE_90, Image.Transpose.FLIP_TOP_BOTTOM)),
]


def model_of(product):
    """The model with this USB product ID, None if no model has it."""
    return next((model for model in MODELS if product in model.products), None)


WRITE_TIMEOUT = 2  # seconds a deck may take to accept a report


class DeckBusy(OSError):
    """An emulated deck that another daemon uses."""


def find_decks():
    """Path -> USB product id of every deck there is.

    A deck that emulator.py stands in for is a socket, and its id is None
    here: it tells its id once connected."""
    found = {}
    for dev in glob.glob("/sys/class/hidraw/hidraw*"):
        try:
            with open(dev + "/device/uevent") as f:
                uevent = f.read()
        except OSError:
            continue
        m = re.search(r"HID_ID=\w+:(\w+):(\w+)", uevent)
        if m and int(m[1], 16) == ELGATO_VENDOR and model_of(int(m[2], 16)):
            found["/dev/" + os.path.basename(dev)] = int(m[2], 16)
    for path in glob.glob(os.path.join(RUNTIME_DIR, "deck-*.sock")):
        found[path] = None
    return found


class Deck:
    """One deck, connected or emulated.

    An emulated deck is sent the reports a real one is, each as one message
    that starts with "O" for an output report or "F" for a feature report, and
    sends the reports a real one does."""

    def __init__(self, path, product=None):
        self.path, self.emulated = path, product is None
        if self.emulated:
            with socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET) as conn:
                conn.settimeout(1)
                conn.connect(path)
                hello = conn.recv(64)  # its USB ids, as "0fd9:0080"
                if not hello:
                    raise DeckBusy("in use")
                try:
                    product = int(hello.split(b":")[-1], 16)
                except ValueError:
                    product = None
                self.fd = conn.detach()
            os.set_blocking(self.fd, False)
        else:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        try:
            if not model_of(product):
                raise OSError("not a model this program knows")
            self.model = model_of(product)
            self.pressed = {}                      # control -> the states it last reported
            self.held = {}                         # key -> when it went down, until it is let go or counts as held
            self.shown = [None] * self.model.keys
            self.screen_shown = [None] * self.model.panels
            self.slots = [None] * self.model.keys  # session shown on each key
            self.brightness = BRIGHTNESS
            self.dark = False                      # see darken()
            self._feature(self.model.brightness(BRIGHTNESS))
        except OSError:
            self.close()
            raise

    def _feature(self, report):
        if self.emulated:
            return self._write(report, b"F")
        buf = bytearray(report)
        hidiocsfeature = (3 << 30) | (len(buf) << 16) | (ord("H") << 8) | 0x06
        fcntl.ioctl(self.fd, hidiocsfeature, buf)

    def _write(self, report, kind=b"O"):
        data = kind + report if self.emulated else report
        while True:
            try:
                os.write(self.fd, data)
                return
            except BlockingIOError:
                if not select.select([], [self.fd], [], WRITE_TIMEOUT)[1]:
                    raise TimeoutError("it takes no data") from None

    def set_image(self, key, image):
        """Draw `image` on `key`; skipped if `image` is what is shown already."""
        pixels = image.tobytes()
        if self.shown[key] == pixels:
            return
        for turn in self.model.turns:
            image = image.transpose(turn)
        buf = io.BytesIO()
        image.save(buf, self.model.image_format, quality=90)
        for report in self.model.image_reports(key, buf.getvalue()):
            self._write(report)
        self.shown[key] = pixels

    def set_screen(self, image):
        """Draw `image` on the screen next to the keys; parts that show it already are skipped."""
        model = self.model
        for part in range(model.panels):
            box = (part * image.width // model.panels, 0, (part + 1) * image.width // model.panels, image.height)
            piece = image.crop(box)
            pixels = piece.tobytes()
            if self.screen_shown[part] == pixels:
                continue
            for turn in model.screen_turns:
                piece = piece.transpose(turn)
            buf = io.BytesIO()
            piece.save(buf, "JPEG", quality=90)
            for report in model.screen_reports(buf.getvalue(), box):
                self._write(report)
            self.screen_shown[part] = pixels

    def dim(self, steps):
        """Make the deck brighter by `steps`, or darker if they are negative."""
        self.brightness = min(100, max(5, self.brightness + 5 * steps))
        self._feature(self.model.brightness(self.brightness))

    def darken(self):
        """Show nothing and switch the light off: a deck may keep its power when the computer has none."""
        if not self.dark:
            self.dark = True
            self.clear()
            self._feature(self.model.brightness(0))

    def lighten(self):
        """Take back darken(). Nothing that was shown is taken to be shown still."""
        if self.dark:
            self.dark = False
            self.shown = [None] * self.model.keys
            self.screen_shown = [None] * self.model.panels
            self._feature(self.model.brightness(self.brightness))

    def clear(self):
        for key in range(self.model.keys):
            self.shown[key] = None
            self.set_image(key, blank(self.model.size))
        if self.model.screen:
            self.screen_shown = [None] * self.model.panels
            self.set_screen(Image.new("RGB", self.model.screen, (0, 0, 0)))

    def events(self):
        """What was done to the deck, from the reports that have arrived, as (control, value).

        ("key", n) tells that key n was pressed and let go, ("hold", n) that it
        is down for HOLD_SECONDS, after which letting it go tells nothing. So a
        deck with a key in `held` is to be asked again soon, report or not.
        ("push", n) and ("pad", n) tell that dial or touch key n was newly
        pressed; "turn" and "touch" are as Model.read() tells them."""
        events = []
        while True:
            try:
                report = os.read(self.fd, 1024)
            except BlockingIOError:
                break
            if not report:
                raise OSError("it is gone")
            for control, value in self.model.read(report).items():
                if control in ("turn", "touch"):
                    events.append((control, value))
                    continue
                was = self.pressed.get(control) or [False] * len(value)
                if control == "key":
                    for n, down in enumerate(value):
                        if down and not was[n]:
                            self.held[n] = time.monotonic()
                        elif was[n] and not down and self.held.pop(n, None) is not None:
                            events.append(("key", n))
                else:
                    events += [(control, n) for n, down in enumerate(value) if down and not was[n]]
                self.pressed[control] = value
        for n, since in list(self.held.items()):
            if time.monotonic() - since >= HOLD_SECONDS:
                del self.held[n]
                events.append(("hold", n))
        return events

    def close(self):
        os.close(self.fd)


# -------------------------------------------------------------- sessions ---

def read_proc(pid, name):
    """Text of /proc/<pid>/<name>."""
    with open(f"/proc/{pid}/{name}", errors="replace") as f:  # arguments need not be UTF-8
        return f.read()


def read_json(path):
    with open(path) as f:
        return json.load(f)


def save_json(path, data, what):
    """Keep `data` in the file at `path`; `what` names it in the log if that fails."""
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path + ".tmp", "w") as out:
            json.dump(data, out)
        os.replace(path + ".tmp", path)
    except OSError as e:
        log(f"cannot save {what}:", e)


def stat_fields(text):
    """(parent pid, terminal, start time) from the text of a /proc/<pid>/stat, None if it is not one."""
    fields = text.rsplit(")", 1)[-1].split()
    try:
        return int(fields[1]), fields[4], fields[19]
    except (IndexError, ValueError):
        return None


def proc_stat(pid):
    """(parent pid, terminal, start time) of a process, None if there is none."""
    try:
        return stat_fields(read_proc(pid, "stat"))
    except OSError:
        return None


def proc_start(pid):
    fields = proc_stat(pid)
    return fields and fields[2]


def is_session(s, start):
    """Whether a session file describes an interactive session of the process started at `start`."""
    return s.get("kind", "interactive") == "interactive" and bool(s.get("sessionId")) \
        and str(s.get("procStart")) == start


# Where sessions are found differs: on this host, in a container, in a virtual
# machine. Each such place is a Source, and SOURCES lists the ones in use.

class Source:
    """A place where sessions are found. A subclass describes one.

    A session is a dict as Claude Code writes it to its session file. The
    daemon uses "sessionId", "status" ("busy", "waiting" or "idle"), "cwd",
    "name", "nameSource", "waitingFor", and "startedAt" and "statusUpdatedAt"
    in milliseconds. A source adds "key", which names the session for as long
    as it lives and is unique among all sources."""

    name = "?"

    def sessions(self):
        """The live interactive sessions found there now, by their "key".

        Called four times a second, so it must not block; a source that has to
        wait for something keeps its sessions up to date in a thread."""
        return {}

    def info(self, session):
        """What the key shows beyond the session file: {"tokens", "model", "tool"}, each optional.

        Unless overridden, it is read from the transcript on this host, in the
        folder that the session's "projects" names."""
        return transcript_info(session)

    def chain(self, session):
        """Pids of the processes on this host that lead to the session's window, nearest first.

        A window or a tool belongs to the session if it is one of them. Unless
        overridden, these are the parents of the process "hostPid"."""
        return ancestors(session.get("hostPid"))


class Local(Source):
    """Sessions on this host, from the session files in a configuration folder of Claude Code."""

    def __init__(self, config=CLAUDE_DIR):
        self.config = os.path.expanduser(config)
        self.name = f"session files in {self.config}"

    def sessions(self):
        own_ns = os.readlink("/proc/self/ns/pid")
        found = {}
        for path in glob.glob(os.path.join(self.config, "sessions", "*.json")):
            try:
                s = read_json(path)
            except (OSError, ValueError):
                continue
            # A file from another pid namespace belongs to a container: see Containers.
            domain = re.search(r"pid:\[\d+\]", s.get("pidDomain", ""))
            start = proc_start(s.get("pid"))
            if (domain and domain[0] != own_ns) or not is_session(s, start):
                continue
            s.update(hostPid=s["pid"], key=f"{s['pid']}:{start}", projects=os.path.join(self.config, "projects"))
            found[s["key"]] = s
        return found


CLAUDE_PIDS_INTERVAL = 2  # seconds between searches of /proc for Claude processes
_claude_pids = (float("-inf"), [])  # (when /proc was searched, the pids found)


def claude_pids():
    """Pids of the Claude processes on this host, in a container or not."""
    global _claude_pids
    if time.monotonic() - _claude_pids[0] >= CLAUDE_PIDS_INTERVAL:
        pids = []
        for entry in os.listdir("/proc"):
            try:
                if entry.isdigit() and read_proc(entry, "comm").strip() == "claude":
                    pids.append(int(entry))
            except OSError:
                continue
        _claude_pids = (time.monotonic(), pids)
    return _claude_pids[1]


class Containers(Source):
    """Sessions of Claude processes in containers, from the session file each container sees.

    A container shares the host's kernel, so its processes and files show up in /proc."""

    name = "containers"

    def sessions(self):
        own_ns = os.readlink("/proc/self/ns/pid")
        found = {}
        for pid in claude_pids():
            try:
                parent, tty, start = proc_stat(pid) or (0, "0", "")
                if os.readlink(f"/proc/{pid}/ns/pid") == own_ns or tty == "0" \
                        or read_proc(parent, "comm").strip() == "claude":
                    continue
                env = dict(v.split("=", 1) for v in read_proc(pid, "environ").split("\0") if "=" in v)
                inner = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME", "/"), ".claude")
                config = f"/proc/{pid}/root/{inner.lstrip('/')}"  # the container's files, as the host sees them
                inner_pid = re.search(r"^NSpid:.*?(\d+)$", read_proc(pid, "status"), re.M)[1]
                s = read_json(os.path.join(config, "sessions", inner_pid + ".json"))
            except (OSError, ValueError, TypeError):
                continue
            if not is_session(s, start):
                continue
            s.update(hostPid=pid, key=f"{pid}:{start}", projects=os.path.join(config, "projects"))
            found[s["key"]] = s
        return found

    def chain(self, session):
        # The container's processes do not lead to a window; its client in a terminal does.
        return ancestors(session["hostPid"]) + ancestors(docker_client(session))


_source_errors = {}  # source -> the error it last failed with


def load_sessions():
    """Live interactive sessions of all SOURCES, by their key."""
    sessions = {}
    for source in SOURCES:
        try:
            found, error = source.sessions(), None
        except Exception as e:  # one source must not take the others' sessions along
            found, error = {}, str(e) or type(e).__name__
        if error != _source_errors.get(source):
            _source_errors[source] = error
            if error:
                log(f"{source.name}: cannot look for sessions: {error}")
        for key, s in found.items():
            s["source"] = source
            sessions[key] = s
    # Sessions sharing a folder are told apart by the name Claude gave them.
    counts = {}
    for s in sessions.values():
        counts[label(s)] = counts.get(label(s), 0) + 1
    for s in sessions.values():
        if counts[label(s)] > 1 and s.get("name"):
            s["label"] = s["name"]
    return sessions


def label(session):
    if session.get("label"):
        return session["label"]
    if session.get("nameSource") in ("user", "peer") and session.get("name"):
        return session["name"]
    return os.path.basename(session.get("cwd", "").rstrip("/")) or session.get("name", "?")


# -------------------------------------------------------------- metadata ---

_transcripts = {}  # session id -> (path, mtime, size, info)
TRANSCRIPT_TAIL = 400_000  # bytes read from the end
# What Claude Code records when /model switches the model, e.g. "Set model to `Opus 5.5`".
# Only a name with a version counts: "Default" says nothing until the next reply.
MODEL_SWITCH = re.compile(r'"text":\s*"<local-command-stdout>Set model to `([^`]*\d[^`]*)`')


def transcript_info(session):
    """Context size, model and current tool, read from the transcript tail."""
    sid = session["sessionId"]
    cached = _transcripts.get(sid)
    path = cached[0] if cached else None
    if not path:
        projects = session.get("projects") or os.path.join(CLAUDE_DIR, "projects")
        slug = re.sub(r"[^A-Za-z0-9]", "-", session.get("cwd", ""))
        path = os.path.join(projects, slug, sid + ".jsonl")
        if not os.path.exists(path):
            found = glob.glob(os.path.join(projects, "*", sid + ".jsonl"))
            if not found:
                return {}
            path = found[0]
    try:
        st = os.stat(path)
        if cached and cached[1:3] == (st.st_mtime, st.st_size):
            return cached[3]
        with open(path, "rb") as f:
            f.seek(max(0, st.st_size - TRANSCRIPT_TAIL))
            lines = f.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return {}
    info = parse_transcript(lines, cached[3] if cached else {})
    _transcripts[sid] = (path, st.st_mtime, st.st_size, info)
    return info


def parse_transcript(lines, previous):
    """Info from the last `lines` of a transcript; `previous` is what an earlier read gave."""
    info = dict(previous)  # keep values the tail no longer has
    info.pop("tool", None)
    seen_turn = seen_usage = switched = False
    for line in reversed(lines):
        if seen_usage:
            break
        switch = not switched and MODEL_SWITCH.search(line)
        if switch:  # a /model command newer than the last reply
            info["model"], switched = switch[1], True
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        if entry.get("isSidechain") or entry.get("isMeta") or kind not in ("assistant", "user"):
            continue
        msg = entry.get("message") or {}
        if not seen_turn:  # the last turn: a reply that is not finished names the running tool
            seen_turn = True
            content = msg.get("content")
            tools = [b.get("name") or "" for b in content if isinstance(b, dict) and b.get("type") == "tool_use"] \
                if isinstance(content, list) else []
            if kind == "assistant" and msg.get("stop_reason") in (None, "tool_use") and tools:
                info["tool"] = tools[-1].split("__")[-1]
        usage = msg.get("usage")
        if kind == "assistant" and usage:
            seen_usage = True
            if not str(msg.get("model") or "<").startswith("<") and not switched:  # "<synthetic>" is no model's reply
                info["model"] = msg["model"]
            info["tokens"] = sum(usage.get(k) or 0 for k in (
                "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"))
    return info


def fmt_duration(seconds):
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds >= 2 * 86400:
        return f"{seconds // 86400}d"
    return f"{seconds // 3600}h{seconds % 3600 // 60:02d}"


def fmt_model(model):
    """Readable name of a model: "claude-opus-5-5" or "Opus 5.5" -> "Opus 5.5"."""
    parts = re.split(r"[-\s.]", model or "")
    family = [p for p in parts if p.isalpha() and p != "claude"]
    version = [p for p in parts if p.isdigit() and len(p) < 3]  # not a date
    return f"{family[0].capitalize()} {'.'.join(version[:2])}".strip() if family else ""


def fmt_tokens(n):
    if not n:
        return ""
    return f"{n / 1e6:.1f}M" if n >= 1_000_000 else f"{round(n / 1000)}k"


# ------------------------------------------------------------- rendering ---

# The drawing is laid out for a key of UNIT pixels, and its measures are in
# such pixels. Graphics are drawn at that size, supersampled, and scaled to
# the key's; text is drawn on the result, its place and font size scaled.
SS = 4  # graphics are drawn supersampled and scaled down; text is drawn at the key's size
BASE = (16, 18, 22)
ACCENT = {
    "busy": (255, 160, 40),
    "waiting": (255, 70, 70),
    "done": (60, 220, 120),
    "idle": (105, 115, 130),
}
ANIMATED = {"busy", "waiting", "done"}
_fonts = {}
_fallback_file = None
PAD = 4                      # pixels left free at a key's left and right edge
TRACK = (48, 52, 60)         # the empty part of a bar
_gradient = Image.linear_gradient("L").resize((UNIT * SS, UNIT * SS)).point(lambda v: 255 - v)
_blanks = {}


def blank(size):
    """An empty key of `size` pixels."""
    if size not in _blanks:
        _blanks[size] = Image.new("RGB", (size, size), (0, 0, 0))
    return _blanks[size]


_targets = {}  # key size -> target() of it


def target(size):
    """A free key of `size` pixels while a session is picked up: it can be put there."""
    if size not in _targets:
        s = scaler(size)
        image = blank(size).copy()
        g = ImageDraw.Draw(image)
        top, bottom = s(6), size - s(SAFE_BOTTOM) - s(3)
        g.rounded_rectangle((s(8), top, size - 1 - s(8), bottom), radius=s(8), outline=TRACK, width=max(1, s(2)))
        x, y, arm = size // 2, (top + bottom) // 2, s(6)
        g.line((x - arm, y, x + arm, y), fill=TRACK, width=max(1, s(2)))
        g.line((x, y - arm, x, y + arm), fill=TRACK, width=max(1, s(2)))
        _targets[size] = image
    return _targets[size]


def lifted(image, t):
    """A session's key while the session is picked up: smaller, bobbing, in a frame that pulses."""
    size = image.width
    s = scaler(size)
    wave = math.sin(t * 2 * math.pi * 1.2)
    small = image.resize((s(54), s(54)), Image.LANCZOS)
    x, y = (size - small.width) // 2, s(6) + round(s(3) * wave)
    out = blank(size).copy()
    out.paste(small, (x, y))
    ImageDraw.Draw(out).rounded_rectangle(
        (x - s(3), y - s(3), x + small.width + s(2), y + small.height + s(2)), radius=s(6),
        outline=mix((110, 110, 110), (255, 255, 255), 0.5 + 0.5 * wave), width=max(1, s(2)))
    return out


def scaler(size):
    """A function that gives the pixels on a key of `size` for a measure of the layout."""
    return lambda v: round(v * size / UNIT)


def name_width(size):
    """Pixels of room for the name on a key of `size`."""
    return size - 2 * scaler(size)(PAD)


def background(accent, tint):
    """A key's supersampled background: `accent` at the top, fading out towards the bottom."""
    big = UNIT * SS
    return Image.composite(Image.new("RGB", (big, big), mix(BASE, accent, tint)),
                           Image.new("RGB", (big, big), BASE), _gradient)


def level_color(used, normal):
    """Colour of a bar that is `used` (0..1) full: amber and red warn that it fills up."""
    return (255, 80, 80) if used > 0.85 else (255, 190, 70) if used > 0.65 else normal


def fallback_font_file():
    global _fallback_file
    if _fallback_file is None:
        try:
            _fallback_file = subprocess.run(
                ["fc-match", "-f", "%{file}", FALLBACK_FONT],
                capture_output=True, text=True, timeout=5).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            _fallback_file = ""
    return _fallback_file


def font(size, style="Condensed Bold"):
    if (size, style) not in _fonts:
        try:
            f = ImageFont.truetype(FONT, size, layout_engine=ImageFont.Layout.BASIC)
            f.set_variation_by_name(style)
        except OSError:
            try:
                f = ImageFont.truetype(fallback_font_file(), size)
            except OSError:
                f = ImageFont.load_default()
        _fonts[size, style] = f
    return _fonts[size, style]


def mix(a, b, t):
    return tuple(round(x + (y - x) * t) for x, y in zip(a, b))


def fit(text, fnt, width):
    """Shorten `text` with an ellipsis so it is at most `width` pixels wide."""
    if fnt.getlength(text) <= width:
        return text
    while len(text) > 1 and fnt.getlength(text + "…") > width:
        text = text[:-1]
    return text.rstrip("-_ /.") + "…"


NAME_SIZE = 15  # font size of the name; the widest model name, "Sonnet 5.5", just fits
SCROLL_FPS = 6  # frames per second while only names scroll


def name_scroll(name, t, size=UNIT):
    """(pixels a name too wide for a key of `size` is moved left at time `t`, whether it is moving).

    Every NAME_EVERY seconds the name rests for a second, moves until its end shows and rests there."""
    px = scaler(size)
    over = font(px(NAME_SIZE)).getlength(name) - name_width(size)
    if over <= 0:
        return 0, False
    moves = min(over / px(16), NAME_EVERY - 3)     # at 16 px a second, faster if that takes too long
    done = (t % NAME_EVERY - 1) / moves
    return round(over * min(1, max(0, done))), 0 < done < 1


MASCOT_COLOR = (217, 119, 87)
MASCOT_ROWS, MASCOT_COLS = 7, 12
# animation -> relative probability
MASCOT_ANIMATIONS = {"walk": 35, "think": 25, "peek": 20, "wave": 20}


def draw_sprite(g, x, y, px, feet="stand", look=0, blink=False, arms=(0, 0), color=None, scale=SS):
    """Draw the creature at (x, y), `px` pixels per cell; `feet` is "stand" or step 0/1,
    `look` shifts the eyes by -1/0/1 and `arms` raises each side's arm. `g` draws
    `scale` times as large."""
    grid = [[False] * MASCOT_COLS for _ in range(MASCOT_ROWS)]
    for row in (0, 1, 2, 3, 4):
        for col in range(2, 10):
            grid[row][col] = True
    for side, cols in enumerate(((0, 1), (10, 11))):
        for col in cols:
            grid[1 if arms[side] else 2][col] = True
    for col in (2, 4, 7, 9):
        grid[5][col] = True
    for col in {"stand": (2, 4, 7, 9), 0: (2, 7), 1: (4, 9)}[feet]:
        grid[6][col] = True
    x, y = round(x), round(y)

    def cell(col, row, color):
        g.rectangle(((x + col * px) * scale, (y + row * px) * scale,
                     (x + (col + 1) * px) * scale - 1, (y + (row + 1) * px) * scale - 1), fill=color)

    for row in range(MASCOT_ROWS):
        for col in range(MASCOT_COLS):
            if grid[row][col]:
                cell(col, row, color or MASCOT_COLOR)
    if not blink:
        for col in (3 + look, 8 + look):
            cell(col, 1, BASE)


ICON_PX = 1.5  # screen pixels per cell of the small mascot next to the time


def draw_icon(g, state, x, y, accent, t):
    """State icon: a small mascot with its top-left corner at (x, y)."""
    if state == "busy":       # stays put: steps on the spot, glances around, blinks
        look = (0, 0, -1, 0, 0, 1)[int(t / 0.7) % 6]
        draw_sprite(g, x, y, ICON_PX, feet=int(t / 0.25) % 2, look=look, blink=t % 2.6 > 2.45)
    elif state == "done":     # jumps for joy, arms up while in the air
        hop = abs(math.sin(t * math.pi / 0.6)) * 3 * ICON_PX
        draw_sprite(g, x, y - hop, ICON_PX, arms=(1, 1) if hop > 1.2 * ICON_PX else (0, 0))
    elif state == "waiting":  # waves both arms and shakes to get attention
        beat = int(t / 0.14) % 2
        draw_sprite(g, x + beat, y, ICON_PX, arms=(beat, 1 - beat),
                    color=mix(accent, (255, 255, 255), 0.35))
    else:                     # idle: asleep, greyed out
        draw_sprite(g, x, y, ICON_PX, blink=True, color=mix(MASCOT_COLOR, (70, 74, 84), 0.6))


# Where a mascot animation plays, in pixels: `px` is the size of one cell of
# the creature, (`x`, `y`) where it stands, `width` and `bottom` the edges it
# walks off over and rises from, and `hop` how high it jumps. An appearance
# lasts `seconds`, of which it walks in, and out, for `walk`. The image drawn
# on is `scale` times as large.
Stage = collections.namedtuple("Stage", "px x y width bottom hop seconds walk scale",
                               defaults=(MASCOT_SECONDS, 1.6, SS))


def key_stage(kind, top, bottom):
    """The stage for the animation `kind` on a key, between `top` and `bottom`."""
    px = 4 if bottom - top >= 34 else 3
    h = MASCOT_ROWS * px
    floor = round(top + (bottom - top - h) / 2)  # stands centred in its area
    if kind == "think":  # leave room for the dots over its head
        floor = min(bottom - h, max(floor, top + 2 * px + 1))
    return Stage(px, (UNIT - MASCOT_COLS * px) / 2, floor, UNIT, UNIT, min(5, floor - top))


def draw_mascot(g, kind, phase, stage):
    """One of the mascot animations; `phase` is seconds since it started."""
    px, x, floor = stage.px, stage.x, stage.y
    w, h = MASCOT_COLS * px, MASCOT_ROWS * px
    bob = max(1, px / 4)  # how far a step or a wave lifts it
    left = stage.seconds - phase
    step = int(phase / 0.16) % 2

    def draw(x, y, **pose):
        draw_sprite(g, x, y, px, scale=stage.scale, **pose)

    if kind == "walk":  # walks in, hops and blinks, walks off
        walk = stage.walk
        if phase < walk:
            draw(-w + (x + w) * phase / walk, floor - step * bob, feet=step)
        elif left < walk:
            draw(x + (stage.width - x) * (1 - left / walk), floor - step * bob, feet=step)
        else:
            hop = abs(math.sin((phase - walk) * math.pi / 0.7)) * stage.hop
            draw(x, floor - hop, blink=(phase - walk) % 1.4 > 1.25)
        return

    # The others rise from the bottom edge and sink back at the end.
    rise = min(1.0, phase / 0.5, left / 0.5)
    y = floor + (stage.bottom - floor) * (1 - rise)
    inner = phase - 0.5
    if kind == "think":  # looks around while dots pop up over its head
        look = (0, -1, 0, 1)[int(inner / 0.9) % 4] if rise == 1 else 0
        draw(x, y, look=look, blink=inner % 2.2 > 2.05)
        if rise == 1:
            for i in range(int(inner / 0.45) % 4):
                dx = x + w / 2 - 2.5 * px + i * 2 * px
                g.rectangle((dx * stage.scale, (y - 2 * px) * stage.scale,
                             (dx + px) * stage.scale - 1, (y - px) * stage.scale - 1),
                            fill=mix(MASCOT_COLOR, (255, 255, 255), 0.5))
    elif kind == "peek":  # peeks over the edge, glances both ways, pops up
        if inner < 2.4:
            y = max(y, floor + h * 0.55)
            look = -1 if inner < 1.2 else 1
        else:
            y = min(y, floor + h * 0.55 * max(0.0, 1 - (inner - 2.4) / 0.25)) if rise == 1 else y
            look = 0
        draw(x, y, look=look if rise == 1 else 0, blink=3.6 < inner < 3.8)
    else:  # wave: swings its arms, with a little bounce
        beat = int(inner / 0.3) % 2 if rise == 1 else None
        arms = (0, 0) if beat is None else (1, 0) if beat else (0, 1)
        bounce = abs(math.sin(inner * math.pi / 0.6)) * 2 * bob if rise == 1 else 0
        draw(x, y - bounce, arms=arms, blink=inner % 1.8 > 1.65)


def render_locked(model, t):
    """The images for a deck's keys while the screen is locked.

    The keys together are one large screen, as if there were nothing between
    them, on which the mascot appears now and then."""
    size = model.size
    width, height = model.cols * size, model.rows * size
    # As large as leaves it room to walk in from the side, and for the dots over its head.
    # On the MK.2 that is a third of a key for each of its cells, so its eyes are not cut in two.
    px = int(min(width / (MASCOT_COLS + 3), height / (MASCOT_ROWS + 2)))
    screen = Image.new("RGB", (width, height), (0, 0, 0))
    appearance, phase = divmod(t, LOCK_EVERY)
    if phase < LOCK_SECONDS:
        kind = random.Random(appearance).choices(list(MASCOT_ANIMATIONS), weights=MASCOT_ANIMATIONS.values())[0]
        stage = Stage(px, (width - MASCOT_COLS * px) / 2, height - MASCOT_ROWS * px, width, height,
                      1.25 * px, LOCK_SECONDS, 3.0, 1)
        draw_mascot(ImageDraw.Draw(screen), kind, phase, stage)
    return [screen.crop((key % model.cols * size, key // model.cols * size,
                         (key % model.cols + 1) * size, (key // model.cols + 1) * size))
            for key in range(model.keys)]


def render(info, t, size=UNIT):
    """Image of `size` pixels for a session's key; `t` (seconds) drives the animations."""
    state = info["state"]
    name = info["name"]
    shift = name_scroll(name, t, size)[0]
    if state not in ANIMATED:  # then only these make one image differ from the next
        drawn = (tuple(info.items()), name, shift, size)
        if drawn not in _still:
            if len(_still) > STILL_KEYS:
                _still.clear()
            _still[drawn] = draw_key(info, t, name, shift, size)
        return _still[drawn]
    return draw_key(info, t, name, shift, size)


_still = {}  # what a key without animation was drawn from -> its image
STILL_KEYS = 200  # how many of these are kept


def draw_key(info, t, name, shift, size):
    px = scaler(size)
    state = info["state"]
    accent = ACCENT.get(state, ACCENT["idle"])
    pulse = 0.0
    if state == "waiting":
        pulse = 0.5 - 0.5 * math.cos(t * 2 * math.pi / 1.1)
        tint = 0.22 + 0.5 * pulse
    elif state == "done":
        pulse = 0.5 - 0.5 * math.cos(t * 2 * math.pi / 3.0)
        tint = 0.26 + 0.16 * pulse
    else:
        tint = 0.2 if state == "busy" else 0.07
    mascot = None
    if state == "busy":
        appearance, phase = divmod(t + info["offset"], MASCOT_EVERY)
        mascot = phase if phase < MASCOT_SECONDS else None
        # Pick the animation at random, but the same one for a whole appearance.
        rng = random.Random(f"{info['offset']}:{appearance}")
        kind = rng.choices(list(MASCOT_ANIMATIONS), weights=MASCOT_ANIMATIONS.values())[0]

    big = UNIT * SS
    img = background(accent, tint)
    g = ImageDraw.Draw(img)
    name_font = font(px(NAME_SIZE))
    name_bottom = 5 + NAME_SIZE
    small = font(px(10), "Condensed Medium")
    time_y = UNIT - SAFE_BOTTOM - 9   # centre of the time row
    bar_y = time_y - 14               # centre of the context bar

    # --- graphics, supersampled ---------------------------------------------
    bar = 3 * SS
    g.rectangle((0, 0, big, bar), fill=mix(BASE, accent, 0.55 + 0.45 * pulse))
    if state == "busy":  # highlight sweeping along the top bar
        seg = big * 0.45
        head = (t * 0.7 % 1) * (big + seg)
        for i in range(12):
            x1 = head - seg * (i + 1) / 12
            g.rectangle((x1, 0, x1 + seg / 12, bar),
                        fill=mix(mix(accent, (255, 255, 255), 0.45), mix(BASE, accent, 0.55), i / 12))
    if mascot is not None:
        draw_mascot(g, kind, mascot, key_stage(kind, name_bottom + 2, UNIT - SAFE_BOTTOM))
    else:
        draw_icon(g, state, PAD, time_y - round(MASCOT_ROWS * ICON_PX / 2), accent, t)
        if info["tokens"]:  # the bar ends where the number next to it starts
            x2 = (UNIT - PAD - small.getlength(info["tokens"]) * UNIT / size - 4) * SS
            box = (PAD * SS, (bar_y - 2) * SS, x2, (bar_y + 2) * SS)
            g.rounded_rectangle(box, 2 * SS, fill=TRACK)
            used = min(1.0, info["context"])
            g.rounded_rectangle((box[0], box[1], box[0] + max(4 * SS, (x2 - box[0]) * used), box[3]),
                                2 * SS, fill=level_color(used, (165, 172, 185)))
    img = img.resize((size, size), Image.LANCZOS)

    # --- text, at the key's size ----------------------------------------------
    d = ImageDraw.Draw(img)
    dim = (150, 156, 168) if state != "idle" else (110, 116, 128)
    soft = mix(accent, (255, 255, 255), 0.25)
    color = (255, 255, 255) if state != "idle" else (190, 195, 205)
    # The name is drawn on its own strip, which cuts off what is scrolled out of it.
    box = (px(PAD), 0, px(PAD) + name_width(size), px(name_bottom + 4))
    strip = img.crop(box)
    if mascot is not None and name_font.getlength(name) <= strip.width:  # centred like the mascot below it
        ImageDraw.Draw(strip).text((strip.width / 2, px(5)), name, font=name_font, fill=color, anchor="ma")
    else:
        ImageDraw.Draw(strip).text((-shift, px(5)), name, font=name_font, fill=color)
    img.paste(strip, box)
    if mascot is not None:
        return img
    big_font = font(px(18))
    time_x = PAD + round(MASCOT_COLS * ICON_PX) + 3
    d.text((px(time_x), px(time_y)), info["elapsed"], font=big_font,
           fill=mix(accent, (255, 255, 255), 0.3), anchor="lm")
    # Under the name: what the session is doing or waiting for, else its model.
    under, fill = (info["activity"], soft) if info["activity"] or state == "busy" else (info["model"], dim)
    if under:
        line = font(px(11), "Condensed Medium")
        d.text((px(PAD), px(name_bottom + 1)), fit(under, line, name_width(size)), font=line, fill=fill)
    d.text((size - px(PAD), px(bar_y)), info["tokens"], font=small, fill=dim, anchor="rm")
    return img


_usage = (None, None)  # (mtime of STATUS_FILE, its rate limits)


def account_usage():
    """The account's usage windows as [(name, used fraction, seconds to reset)].

    Only what is known to hold now: a reading saved before the computer started
    says nothing about what the account has used since, and neither does the
    figure of a window that has reset."""
    global _usage
    try:
        mtime = os.stat(STATUS_FILE).st_mtime
        if _usage[0] != mtime:
            _usage = (mtime, read_json(STATUS_FILE).get("rate_limits") or {})
    except (OSError, ValueError):
        return []
    now = time.time()
    if mtime < now - time.clock_gettime(time.CLOCK_BOOTTIME):
        return []
    windows = []
    for name, field in (("5h", "five_hour"), ("week", "seven_day")):
        window = _usage[1].get(field)
        left = (window.get("resets_at") or 0) - now if window else 0
        if left > 0:
            windows.append((name, (window.get("used_percentage") or 0) / 100, left))
    return windows


def fmt_reset(left):
    """Time until a usage window resets, in whole hours once minutes stop mattering."""
    return f"{int(left // 3600)}h" if 10 * 3600 <= left < 2 * 86400 else fmt_duration(left)


def render_usage(size=UNIT):
    """Image of `size` pixels for the key that shows how much of the account's usage limits is used."""
    px = scaler(size)
    windows = account_usage()
    big = UNIT * SS
    worst = max((used for _, used, _ in windows), default=0)
    accent = level_color(worst, MASCOT_COLOR)
    img = background(accent, 0.14)
    g = ImageDraw.Draw(img)
    g.rectangle((0, 0, big, 3 * SS), fill=mix(BASE, accent, 0.7))
    rows = [7 + i * 27 for i in range(len(windows))]  # top of each window's block
    for y, (_, used, _) in zip(rows, windows):
        box = (PAD * SS, (y + 16) * SS, (UNIT - PAD) * SS, (y + 21) * SS)
        g.rounded_rectangle(box, 2.5 * SS, fill=TRACK)
        g.rounded_rectangle((box[0], box[1], box[0] + max(5 * SS, (box[2] - box[0]) * min(1, used)),
                             box[3]), 2.5 * SS, fill=level_color(used, MASCOT_COLOR))
    img = img.resize((size, size), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    dim = (150, 156, 168)
    small = font(px(10), "Condensed Medium")
    if not windows:
        d.text((size / 2, px(24)), "usage", font=font(px(15)), fill=(255, 255, 255), anchor="mm")
        d.text((size / 2, px(42)), "no data yet", font=small, fill=dim, anchor="mm")
        return img
    for y, (name, used, left) in zip(rows, windows):
        d.text((px(PAD), px(y + 13)), name, font=font(px(11)), fill=(255, 255, 255), anchor="ls")
        x = px(PAD) + font(px(11)).getlength(name) + px(3)
        d.text((x, px(y + 13)), fmt_reset(left), font=small, fill=dim, anchor="ls")
        d.text((size - px(PAD), px(y + 13)), f"{round(used * 100)}%", font=font(px(15)),
               fill=(255, 255, 255), anchor="rs")
    return img


# The screen next to the keys of some models shows how many sessions are in
# each of these states, then the account usage. On the +, each has a panel of
# its own over a dial.
PANELS = ("waiting", "done", "busy")
PANEL_NAMES = {"waiting": "needs you", "done": "done", "busy": "working"}
_screens = {}  # what a screen was drawn from -> its image


def render_screen(model, counts):
    """Image for a model's screen; `counts` are the sessions in each of PANELS' states."""
    windows = account_usage()
    drawn = (model.id, tuple(counts), tuple((name, round(used * 100), fmt_reset(left))
                                            for name, used, left in windows))
    if drawn not in _screens:
        if len(_screens) > 20:
            _screens.clear()
        _screens[drawn] = (draw_panels if model.panels > 1 else draw_compact)(model.screen, counts, windows)
    return _screens[drawn]


def draw_screen(size, graphics):
    """An image of `size` with `graphics(g)` drawn supersampled, then the texts it returns.

    A text is (place, text, font, colour, anchor)."""
    img = Image.new("RGB", (size[0] * SS, size[1] * SS), BASE)
    texts = graphics(img, ImageDraw.Draw(img))
    img = img.resize(size, Image.LANCZOS)
    d = ImageDraw.Draw(img)
    for xy, text, fnt, fill, anchor in texts:
        d.text(xy, text, font=fnt, fill=fill, anchor=anchor)
    return img


def draw_panels(size, counts, windows):
    """A wide screen: a panel for each of PANELS and one for the account usage."""
    (w, h), parts = size, len(PANELS) + 1
    pw = w // parts - 2  # two pixels stay empty between panels
    white, dim, grey = (255, 255, 255), (150, 156, 168), (110, 116, 128)

    def graphics(img, g):
        texts = []

        def panel(part, accent, tint, bar):
            x = part * w // parts
            img.paste(background(accent, tint).resize((pw * SS, h * SS)), (x * SS, 0))
            g.rectangle((x * SS, 0, (x + pw) * SS - 1, 3 * SS), fill=mix(BASE, accent, bar))
            return x

        for part, (state, count) in enumerate(zip(PANELS, counts)):
            accent = ACCENT[state] if count else ACCENT["idle"]
            x = panel(part, accent, 0.24 if count else 0.07, 0.9 if count else 0.55)
            color = mix(MASCOT_COLOR, (70, 74, 84), 0.6) if not count \
                else mix(accent, white, 0.35) if state == "waiting" else None
            draw_sprite(g, x + pw - 12 - MASCOT_COLS * 5, 46, 5, blink=not count, color=color,
                        arms=(1, 1) if count and state != "busy" else (0, 0))
            texts.append(((x + 12, 12), PANEL_NAMES[state], font(17, "Condensed Medium"),
                          mix(accent, white, 0.25) if count else grey, "la"))
            texts.append(((x + 12, 64), str(count), font(50), white if count else grey, "lm"))

        worst = max((used for _, used, _ in windows), default=0)
        x = panel(parts - 1, level_color(worst, MASCOT_COLOR), 0.14, 0.7)
        if not windows:
            texts.append(((x + pw / 2, 40), "usage", font(22), white, "mm"))
            texts.append(((x + pw / 2, 64), "no data yet", font(15, "Condensed Medium"), dim, "mm"))
        for row, (name, used, left) in enumerate(windows):
            y = 14 + row * 40
            box = ((x + 12) * SS, (y + 24) * SS, (x + pw - 12) * SS, (y + 31) * SS)
            g.rounded_rectangle(box, 3.5 * SS, fill=TRACK)
            g.rounded_rectangle((box[0], box[1], box[0] + max(7 * SS, (box[2] - box[0]) * min(1, used)),
                                 box[3]), 3.5 * SS, fill=level_color(used, MASCOT_COLOR))
            texts.append(((x + 12, y + 18), name, font(17), white, "ls"))
            texts.append(((x + 12 + font(17).getlength(name) + 5, y + 18), fmt_reset(left),
                          font(15, "Condensed Medium"), dim, "ls"))
            texts.append(((x + pw - 12, y + 18), f"{round(used * 100)}%", font(22), white, "rs"))
        return texts

    return draw_screen(size, graphics)


def draw_compact(size, counts, windows):
    """A small screen: the counts of PANELS' states side by side, then the account usage."""
    w, h = size
    col = w // 6  # the counts take half the width
    white, dim, grey = (255, 255, 255), (150, 156, 168), (95, 100, 112)

    def graphics(img, g):
        texts = []
        for part, (state, count) in enumerate(zip(PANELS, counts)):
            x, accent = part * col, ACCENT[state]
            if count:
                g.rectangle(((x + 3) * SS, 0, (x + col - 3) * SS, 2 * SS), fill=accent)
            texts.append(((x + col / 2, h * 0.42), str(count), font(24), accent if count else grey, "mm"))
            texts.append(((x + col / 2, h * 0.8), fit(PANEL_NAMES[state], font(10, "Condensed Medium"), col - 2),
                          font(10, "Condensed Medium"), dim if count else grey, "mm"))
        left_edge, right_edge = 3 * col + 8, w - 4
        if not windows:
            texts.append((((left_edge + right_edge) / 2, h / 2), "no usage data",
                          font(11, "Condensed Medium"), dim, "mm"))
        for row, (name, used, _) in enumerate(windows):
            y = 5 + row * (h - 6) // 2
            box = (left_edge * SS, (y + 14) * SS, right_edge * SS, (y + 18) * SS)
            g.rounded_rectangle(box, 2 * SS, fill=TRACK)
            g.rounded_rectangle((box[0], box[1], box[0] + max(4 * SS, (box[2] - box[0]) * min(1, used)),
                                 box[3]), 2 * SS, fill=level_color(used, MASCOT_COLOR))
            texts.append(((left_edge, y + 11), name, font(11), white, "ls"))
            texts.append(((right_edge, y + 11), f"{round(used * 100)}%", font(12), white, "rs"))
        return texts

    return draw_screen(size, graphics)


_usage_keys = {}  # size -> (when it was drawn, the image)


def usage_key(size, now):
    """render_usage(), drawn anew once a second: it shows nothing finer than seconds."""
    if size not in _usage_keys or now - _usage_keys[size][0] >= 1:
        _usage_keys[size] = (now, render_usage(size))
    return _usage_keys[size][1]


# ------------------------------------------------------------- sandboxes ---
# A container shares the host's kernel, so its processes and files show up in
# /proc and Containers finds them. A virtual machine or another host shows
# neither; its sessions are reported by a script that runs inside it.

class Sandbox:
    """A kind of sandbox that can run a command inside. A subclass describes one."""

    name = "?"
    program = "?"  # what the user runs in a terminal to work in such a sandbox

    def running(self):
        """Names of the sandboxes that are running now."""
        return []

    def command(self, sandbox):
        """Start of a command line that runs its remaining arguments inside `sandbox`.

        The command's input and output must be passed through."""
        raise NotImplementedError


class Sbx(Sandbox):
    """Docker Sandboxes, each a virtual machine."""

    name = program = "sbx"

    def running(self):
        # Without its daemon nothing runs, and asking would start it.
        if not shutil.which("sbx") or not any(args[1:2] == ["daemon"] for _, args in processes("sbx")):
            return []
        return [s["name"] for s in run_json("sbx", "ls", "--format", "json") if s.get("status") == "running"]

    def command(self, sandbox):
        return ["sbx", "exec", "-i", sandbox]


SANDBOXES = [Sbx()]
SANDBOX_INTERVAL = 5  # seconds between looks for sandboxes that started

# Runs inside a sandbox for as long as it has sessions. It reports each live
# session's file, process and terminal times (see terminal_client()), and its
# transcript's tail when that grew, then waits for a line on its input; so it
# ends when the daemon does.
# Every line of its own starts with the word passed as $1; other lines are transcript.
PROBE = r"""
n=$1
dir=${CLAUDE_CONFIG_DIR:-$HOME/.claude}
sizes=
while :; do
    live= seen=
    for f in "$dir"/sessions/*.json; do
        pid=${f##*/} pid=${pid%.json} stat= json=
        read -r stat <"/proc/$pid/stat" || [ -n "$stat" ] || continue
        read -r json <"$f" || [ -n "$json" ] || continue
        sid=${json#*\"sessionId\":} sid=${sid#*\"} sid=${sid%%\"*}
        case $sid in ""|*[!0-9a-f-]*) continue ;; esac
        live=1
        printf '%s\n' "$n session $json" "$n stat $stat" "$n tty $(stat -L -c '%X %Y' "/proc/$pid/fd/0")"
        for t in "$dir"/projects/*/"$sid".jsonl; do
            [ -f "$t" ] || continue
            size=$(($(wc -c <"$t")))
            seen="$seen $sid:$size "
            case $sizes in *" $sid:$size "*) continue ;; esac
            printf '%s\n' "$n transcript $sid"
            tail -c """ + str(TRANSCRIPT_TAIL) + r""" "$t"
            printf '\n%s\n' "$n end"
        done
    done 2>/dev/null
    sizes=$seen
    printf '%s\n' "$n done"
    [ -n "$live" ] && read -r next || exit 0
done
"""

_sandboxed = {}  # (kind of sandbox, sandbox) -> its sessions, as load_sessions() returns them


def watch_sandbox(kind, sandbox):
    """Keep the sessions of one sandbox up to date until it has none left."""
    mark = os.urandom(8).hex()  # a transcript cannot contain it, so it cannot pose as the probe
    metas = {}                  # session id -> what parse_transcript() made of its transcript
    sessions, session, transcript = {}, None, None
    proc = subprocess.Popen(kind.command(sandbox) + ["sh", "-c", PROBE, "sh", mark],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        for raw in proc.stdout:
            line = raw.decode("utf-8", "replace").rstrip("\n")
            if not line.startswith(mark + " "):
                if transcript:
                    transcript[1].append(line)
                continue
            word, _, rest = line[len(mark) + 1:].partition(" ")
            if word == "session":
                try:
                    session = json.loads(rest)
                except ValueError:
                    session = None
            elif word == "stat" and isinstance(session, dict):
                fields = stat_fields(rest)
                if fields and is_session(session, fields[2]):
                    session.update(sandbox=(kind, sandbox),
                                   key=f"{kind.name}:{sandbox}:{session.get('pid')}:{fields[2]}")
                    sessions[session["key"]] = session
            elif word == "tty" and isinstance(session, dict):
                try:
                    session["tty"] = tuple(float(t) for t in rest.split())
                except ValueError:
                    pass
            elif word == "transcript":
                transcript = (rest, [])
            elif word == "end" and transcript:
                metas[transcript[0]] = parse_transcript(transcript[1], metas.get(transcript[0], {}))
                transcript = None
            elif word == "done":
                for s in sessions.values():
                    s["meta"] = metas.get(s["sessionId"], {})
                _sandboxed[kind, sandbox] = sessions
                sessions = {}
                time.sleep(1)
                os.write(proc.stdin.fileno(), b"\n")  # ask for the next report
    except OSError:
        pass
    finally:
        _sandboxed.pop((kind, sandbox), None)
        proc.kill()
        proc.wait()


def watch_sandboxes():
    """Watch every running sandbox; meant to run in a thread of its own."""
    watching, errors = {}, {}
    while True:
        for kind in SANDBOXES:
            try:
                names, error = kind.running(), None
            except Exception as e:
                names, error = [], str(e)
            if error != errors.get(kind):
                errors[kind] = error
                if error:
                    log(f"{kind.name}: cannot list sandboxes: {error}")
            for name in names:
                if (kind, name) not in watching or not watching[kind, name].is_alive():
                    watching[kind, name] = threading.Thread(target=watch_sandbox, args=(kind, name), daemon=True)
                    watching[kind, name].start()
        time.sleep(SANDBOX_INTERVAL)


def processes(program):
    """(pid, arguments) of each process on this host that runs `program`."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            args = read_proc(entry, "cmdline").split("\0")
        except OSError:
            continue
        if os.path.basename(args[0]) == program:
            yield int(entry), args


class Sandboxes(Source):
    """Sessions in the sandboxes of SANDBOXES, as reported by a script that runs inside each."""

    name = "sandboxes"
    watcher = None

    def sessions(self):
        if not self.watcher:
            self.watcher = threading.Thread(target=watch_sandboxes, daemon=True)
            self.watcher.start()
        return {key: s for found in list(_sandboxed.values()) for key, s in found.items()}

    def info(self, session):
        return session.get("meta", {})  # read inside the sandbox, see watch_sandbox()

    def chain(self, session):
        return ancestors(sandbox_client(session))


SOURCES = [Local(), Containers(), Sandboxes()]


# What a session in a sandbox reads and writes passes through a client process
# on the host, such as `sbx run` or `docker run`, and through that one's
# terminal. So both terminals were last typed into, and last written to, at the
# same moments, which tells the client of one session from that of another.
TERMINAL_SLACK = 16  # seconds the times may differ: the kernel records them in steps of 8


def terminal_times(path):
    """(last read, last written) of the terminal at `path`, None if it is not one."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_atime, st.st_mtime) if stat.S_ISCHR(st.st_mode) else None


def terminal_client(program, times, likely):
    """Pid of the `program` process in a terminal through which the user works in a session.

    `times` are the terminal_times() of the session's own terminal. Without a
    process whose terminal has these, it is the latest one for which
    `likely(pid, arguments, start time)` holds."""
    exact, exact_off, guess, guess_start = None, TERMINAL_SLACK, None, -1
    for pid, args in processes(program):
        parent, tty, start = proc_stat(pid) or (0, "0", "")
        if tty == "0" or parent == os.getpid():  # gone, not in a terminal, or a probe of ours
            continue
        start = int(start)
        own = terminal_times(f"/proc/{pid}/fd/0")
        if times and own and len(times) == 2:
            off = abs(own[0] - times[0]) + abs(own[1] - times[1])
            if off < exact_off:
                exact, exact_off = pid, off
        if start > guess_start and likely(pid, args, start):
            guess, guess_start = pid, start
    return exact or guess


def sandbox_client(session):
    """Pid of the process in a terminal through which the user works in the sandboxed session."""
    kind, sandbox = session["sandbox"]
    cwd = session.get("cwd", "")
    return terminal_client(kind.program, session.get("tty"), lambda pid, args, start:
                           sandbox in args or cwd in args or os.path.realpath(f"/proc/{pid}/cwd") == cwd)


# --------------------------------------------------------- window focus ---

def ancestors(pid):
    chain = []
    while pid and pid > 1:
        chain.append(pid)
        pid = (proc_stat(pid) or (0,))[0]
    return chain


def docker_client(session):
    """Pid of the `docker run` process that hosts a containerised session."""
    cwd = session.get("cwd", "")
    born = int(proc_start(session["hostPid"]) or 0)
    # The client starts just before its container, so the latest one started
    # before the session is likely its own.
    return terminal_client("docker", terminal_times(f"/proc/{session['hostPid']}/fd/0"), lambda pid, args, start:
                           "run" in args and start <= born
                           and (cwd in args or os.path.realpath(f"/proc/{pid}/cwd") == cwd))


# Focusing a window differs per desktop. Each way to do it is a Backend, and
# BACKENDS lists them in the order they are tried.

class Backend:
    """A way to list and raise the windows of a desktop. A subclass describes one."""

    name = "?"

    def windows(self):
        """The open windows, each {"id", "pid", "title", "focus"}; None if this is not that desktop.

        "pid" is the process that owns the window and "focus" tells whether it
        has the focus. Raises if this is that desktop but it cannot be used."""
        return None

    def activate(self, wid):
        """Raise the window whose "id" is `wid` and give it the focus."""


def run_json(*cmd):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True).stdout
    return json.loads(out)


class GnomeShell(Backend):
    """GNOME, through a shell extension that offers List and Activate over D-Bus."""

    def __init__(self, name, path, interface):
        self.name, self.path, self.interface = name, path, interface

    def _call(self, method, *args):
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)
        params = GLib.Variant("(u)", args) if args else None
        reply = bus.call_sync("org.gnome.Shell", self.path, self.interface, method, params,
                              None, Gio.DBusCallFlags.NONE, 2000, None).unpack()
        return reply[0] if reply else None

    def windows(self):
        if "GNOME" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper():
            return None
        try:
            return json.loads(self._call("List"))
        except Exception:
            raise RuntimeError("not loaded") from None

    def activate(self, wid):
        self._call("Activate", wid)


class Sway(Backend):
    """Sway, through its IPC."""
    name = "sway"

    def windows(self):
        if not os.environ.get("SWAYSOCK"):
            return None
        found, todo = [], [run_json("swaymsg", "-t", "get_tree")]
        while todo:
            node = todo.pop()
            todo += node.get("nodes", []) + node.get("floating_nodes", [])
            if node.get("pid"):
                found.append({"id": node["id"], "pid": node["pid"], "title": node.get("name"),
                              "focus": bool(node.get("focused"))})
        return found

    def activate(self, wid):
        subprocess.run(["swaymsg", f"[con_id={wid}] focus"], capture_output=True, timeout=3)


class Hyprland(Backend):
    """Hyprland, through hyprctl."""
    name = "hyprland"

    def windows(self):
        if not os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
            return None
        return [{"id": c["address"], "pid": c.get("pid"), "title": c.get("title"),
                 "focus": c.get("focusHistoryID") == 0}
                for c in run_json("hyprctl", "clients", "-j")]

    def activate(self, wid):
        subprocess.run(["hyprctl", "dispatch", "focuswindow", f"address:{wid}"],
                       capture_output=True, timeout=3)


class KWin(Backend):
    """KDE Plasma on Wayland, through the kdotool program."""
    name = "kdotool"

    def _run(self, *args):
        return subprocess.run(["kdotool", *args], capture_output=True, text=True,
                              timeout=3, check=True).stdout.strip()

    def windows(self):
        if "KDE" not in os.environ.get("XDG_CURRENT_DESKTOP", "").upper() \
                or os.environ.get("XDG_SESSION_TYPE") != "wayland":
            return None
        found = []
        try:
            active = self._run("getactivewindow")
        except subprocess.SubprocessError:
            active = None
        for wid in self._run("search", "--name", ".").split():
            try:
                found.append({"id": wid, "pid": int(self._run("getwindowpid", wid)),
                              "title": self._run("getwindowname", wid), "focus": wid == active})
            except (subprocess.SubprocessError, ValueError):
                continue
        return found

    def activate(self, wid):
        self._run("windowactivate", wid)


class X11(Backend):
    """Any X11 window manager, through EWMH; under Wayland it reaches XWayland programs only."""
    name = "x11"

    class _Message(ctypes.Structure):  # XClientMessageEvent, padded to XEvent size
        _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                    ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                    ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                    ("format", ctypes.c_int), ("data", ctypes.c_long * 5),
                    ("pad", ctypes.c_long * 24)]

    def __init__(self):
        self.x = None
        path = ctypes.util.find_library("X11")
        if not path:
            return
        x = self.x = ctypes.CDLL(path)
        x.XOpenDisplay.restype = ctypes.c_void_p
        x.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x.XDefaultRootWindow.restype = ctypes.c_ulong
        x.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        x.XInternAtom.restype = ctypes.c_ulong
        x.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
        x.XGetWindowProperty.argtypes = [
            ctypes.c_void_p, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_long, ctypes.c_long,
            ctypes.c_int, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong), ctypes.POINTER(ctypes.c_void_p)]
        x.XFree.argtypes = [ctypes.c_void_p]
        x.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                 ctypes.c_long, ctypes.c_void_p]
        x.XFlush.argtypes = [ctypes.c_void_p]
        # Xlib's default handler exits the program, e.g. when a window closes
        # while we look at it.
        handler = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.c_void_p)
        self._ignore_errors = handler(lambda display, event: 0)
        x.XSetErrorHandler(self._ignore_errors)

    def _property(self, dpy, window, name):
        """Raw value of a window property: (bytes, item count), or (None, 0)."""
        x = self.x
        kind, fmt = ctypes.c_ulong(), ctypes.c_int()
        count, rest, data = ctypes.c_ulong(), ctypes.c_ulong(), ctypes.c_void_p()
        status = x.XGetWindowProperty(
            dpy, window, x.XInternAtom(dpy, name, False), 0, 4096, False, 0,
            ctypes.byref(kind), ctypes.byref(fmt), ctypes.byref(count),
            ctypes.byref(rest), ctypes.byref(data))
        if status != 0 or not data.value:
            return None, 0
        # Format 32 items are stored as C longs, whatever their size.
        size = ctypes.sizeof(ctypes.c_long) if fmt.value == 32 else fmt.value // 8
        raw = ctypes.string_at(data.value, count.value * size)
        x.XFree(data)
        return raw, count.value

    def _longs(self, dpy, window, name):
        raw, count = self._property(dpy, window, name)
        return list((ctypes.c_ulong * count).from_buffer_copy(raw)) if raw else []

    def windows(self):
        dpy = self.x and os.environ.get("DISPLAY") and self.x.XOpenDisplay(None)
        if not dpy:
            return None
        try:
            root = self.x.XDefaultRootWindow(dpy)
            active = self._longs(dpy, root, b"_NET_ACTIVE_WINDOW")
            found = []
            for wid in self._longs(dpy, root, b"_NET_CLIENT_LIST"):
                title = self._property(dpy, wid, b"_NET_WM_NAME")[0] \
                    or self._property(dpy, wid, b"WM_NAME")[0] or b""
                pid = self._longs(dpy, wid, b"_NET_WM_PID")
                found.append({"id": wid, "pid": pid[0] if pid else None,
                              "title": title.decode("utf-8", "replace"),
                              "focus": [wid] == active})
            return found
        finally:
            self.x.XCloseDisplay(dpy)

    def activate(self, wid):
        dpy = self.x.XOpenDisplay(None)
        if not dpy:
            return
        try:
            event = self._Message(type=33, send_event=1, window=wid, format=32,  # ClientMessage
                                  message_type=self.x.XInternAtom(dpy, b"_NET_ACTIVE_WINDOW", False))
            event.data[0] = 2  # request comes from a pager, so the WM does not refuse it
            redirect_and_notify = (1 << 20) | (1 << 19)
            self.x.XSendEvent(dpy, self.x.XDefaultRootWindow(dpy), False,
                              redirect_and_notify, ctypes.byref(event))
            self.x.XFlush(dpy)
        finally:
            self.x.XCloseDisplay(dpy)


# X11 comes last because under Wayland it only sees XWayland programs.
BACKENDS = [
    GnomeShell("window-calls extension", "/org/gnome/Shell/Extensions/Windows",
               "org.gnome.Shell.Extensions.Windows"),
    GnomeShell("claude-session-deck-windows extension", "/ch/claudesessiondeck/Windows",
               "ch.claudesessiondeck.Windows"),
    Sway(), Hyprland(), KWin(), X11(),
]


def window_score(session, chain, window):
    title = window.get("title") or ""
    project = os.path.basename(session.get("cwd", "").rstrip("/"))
    name = session.get("name") or ""
    score = 0
    if window.get("pid") in chain:
        score += 100
    if project:
        if re.match(re.escape(project) + r"(\s|$)", title):
            score += 30  # IDE style "project – file"
        elif re.search(r"(?<![\w-])" + re.escape(project) + r"(?![\w-])", title):
            score += 20
        elif project in title:
            score += 5
    if name and name in title:
        score += 20
    return score


def list_windows():
    """(backend, windows, notes) from the first backend usable on this desktop."""
    notes = []
    for backend in BACKENDS:
        try:
            windows = backend.windows()
        except Exception as e:  # backend applies here but is not usable
            notes.append(f"{backend.name}: {e}")
            continue
        if windows:
            notes.append(f"{backend.name}: {len(windows)} windows")
            return backend, windows, notes
    return None, [], notes


# All windows of an IDE share one process, so the window focused when a session
# starts or gets a prompt is remembered as that session's.
_learned = None  # session["key"] -> {"backend": name, "id": window id}


def learned_windows():
    global _learned
    if _learned is None:
        try:
            _learned = read_json(WINDOWS_FILE)
        except (OSError, ValueError):
            _learned = {}
    return _learned


def learn_window(session, live_keys):
    """Remember the focused window as the session's, if it belongs to its terminal or IDE."""
    backend, windows, _ = list_windows()
    chain = session["source"].chain(session)
    focused = [w for w in windows if w.get("focus") and w.get("pid") in chain]
    if not focused:
        return
    learned = learned_windows()
    entry = {"backend": backend.name, "id": focused[0]["id"]}
    if learned.get(session["key"]) == entry:
        return
    learned[session["key"]] = entry
    for key in set(learned) - live_keys:  # forget ended sessions
        del learned[key]
    save_json(WINDOWS_FILE, learned, "learned windows")


def find_window(session, chain, title=None):
    """(backend, window, notes) for the session; window is None if not found.

    `title` is the title of the session's window, if its tool reported one."""
    backend, windows, notes = list_windows()
    if backend is None:
        return None, None, notes
    for window in windows:
        if title and window.get("title") == title and window.get("pid") in chain:
            return backend, window, notes
    learned = learned_windows().get(session["key"])
    if learned and learned["backend"] == backend.name:
        for window in windows:
            if window["id"] == learned["id"]:
                return backend, window, notes
    best = max(windows, key=lambda w: window_score(session, chain, w))
    if window_score(session, chain, best) > 0:
        return backend, best, notes
    return None, None, notes


# Raising a window works for any terminal or IDE. A tool listed here knows the
# program a session runs in well enough to do more, such as selecting its tab.

class Tool:
    """A terminal or IDE that sessions run in. A subclass overrides what it supports."""

    name = "?"

    def runs(self, pid):
        """Whether the process `pid` is this tool and can be driven right now."""
        return False

    def focus(self, pid, chain):
        """Bring the session to the front inside the tool process `pid`.

        `chain` holds the session's parent processes. Returns the title of the
        window the session is in, if the tool knows it; raises if it fails."""
        return None


class JetBrains(Tool):
    """IntelliJ-based IDEs, through the plugin in jetbrains-plugin/.

    The plugin listens on a socket named after the IDE's process and selects the
    terminal tab whose shell is one of the processes it is sent."""

    name = "JetBrains IDE"

    def _socket(self, pid):
        return os.path.join(RUNTIME_DIR, f"jetbrains-{pid}.sock")

    def runs(self, pid):
        return os.path.exists(self._socket(pid))

    def focus(self, pid, chain):
        with socket.socket(socket.AF_UNIX) as conn:
            conn.settimeout(3)
            conn.connect(self._socket(pid))
            conn.sendall(f"focus {' '.join(map(str, chain))}\n".encode())
            reply = conn.makefile(encoding="utf-8").readline().rstrip("\n")
        status, _, title = reply.partition("\t")
        if status != "ok":
            raise RuntimeError("no terminal tab belongs to the session")
        return title or None



class Ptyxis(Tool):
    """Ptyxis, the terminal of GNOME.

    A Ptyxis that tells a tab's shell the ID of its tab, in PTYXIS_TAB, is asked
    for that tab. Up to version 50 it does not, and the tab is reached through
    what Ptyxis tells screen readers. That names the tabs of a window by their
    titles, which need not differ, and lets one step from a tab to the next, but
    not pick one. So the session's tab is found by a title written to its
    terminal, which is then set back, and stepped to."""

    name = "Ptyxis"
    WAIT = 1  # seconds for the title to show up, and for a step to take effect
    direct_error = None  # why the tab could last not be asked for

    def runs(self, pid):
        try:
            return read_proc(pid, "comm").strip() == "ptyxis-agent"  # the process every tab's shell is started by
        except OSError:
            return False

    def focus(self, pid, chain):
        if chain.index(pid) == 0:
            raise RuntimeError("the session's terminal is not known")
        shell = chain[chain.index(pid) - 1]  # what the tab runs
        tab = self._tab(shell)
        if tab:
            try:
                self._select(tab)
                error = None
            except Exception as e:
                error = str(e) or type(e).__name__
            if error != self.direct_error:
                self.direct_error = error
                if error:
                    log(f"{self.name}: cannot ask for a tab, so it is stepped to: {error}")
            if not error:
                return None  # which window holds the tab is not told
        return self._step(pid, shell)

    def _tab(self, shell):
        """ID of the tab that runs the process `shell`, None if Ptyxis did not tell it."""
        try:
            env = read_proc(shell, "environ").split("\0")
        except OSError:
            return None
        tab = next((v.partition("=")[2] for v in env if v.startswith("PTYXIS_TAB=")), "")
        return tab if re.fullmatch(r"[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}", tab) else None

    def _select(self, tab):
        """Ask Ptyxis for the tab with this ID. One that has no such tab does nothing, so each one is asked."""
        from gi.repository import Gio, GLib
        bus = Gio.bus_get_sync(Gio.BusType.SESSION)

        def call(name, path, interface, method, params=None):
            return bus.call_sync(name, path, interface, method, params, None, Gio.DBusCallFlags.NONE, 2000, None)

        names = call("org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames").unpack()[0]
        asked, error = 0, RuntimeError("it is not on the session bus")
        for name in names:
            if not re.search(r"\.Ptyxis(\.Devel)?$", name):
                continue
            try:
                call(name, "/" + name.replace(".", "/").replace("-", "_"), "org.gtk.Actions", "Activate",
                     GLib.Variant("(sava{sv})", ("focus-tab-by-uuid", [GLib.Variant("s", tab)], {})))
                asked += 1
            except GLib.Error as e:
                error = e
        if not asked:
            raise error

    def _step(self, pid, shell):
        """Step to the tab that runs the process `shell`; returns the title of its window."""
        import gi
        gi.require_version("Atspi", "2.0")
        from gi.repository import Atspi
        self.atspi = Atspi
        terminal = os.readlink(f"/proc/{shell}/fd/0")
        if not terminal.startswith("/dev/pts/"):
            raise RuntimeError("the session's terminal is not known")
        windows = self._windows((proc_stat(pid) or (0,))[0])
        before = [[page.get_name() for page in self._pages(window)] for window in windows]
        mark = "claude-session-deck-" + os.urandom(4).hex()
        self._title(terminal, mark)
        place, deadline = None, time.monotonic() + self.WAIT
        while place is None and time.monotonic() < deadline:
            time.sleep(0.02)
            place = next(((n, i, page.get_name()) for n, window in enumerate(windows)
                          for i, page in enumerate(self._pages(window)) if page.get_name().startswith(mark)), None)
        if place is None:
            self._title(terminal, "")
            raise RuntimeError("no tab took the title written to the session's terminal")
        n, index, name = place
        # After the title a tab names what runs in it; that part is not the title's.
        was, rest = (before[n][index] if index < len(before[n]) else ""), name[len(mark):]
        self._title(terminal, was[:len(was) - len(rest)] if rest and was.endswith(rest) else was)
        # All steps are taken at once, faster than the window is drawn, so the tabs in between do not show.
        deadline = time.monotonic() + self.WAIT
        while time.monotonic() < deadline:
            shown = self._shown(windows[n])
            if shown in (None, index):
                break
            self._do(windows[n], "page.next" if index > shown else "page.previous", abs(index - shown))
            time.sleep(0.05)  # until the window tells which tab it shows now
        return windows[n].get_name() or None

    def _title(self, terminal, title):
        with open(terminal, "w") as out:
            out.write(f"\033]0;{title}\007")

    def _walk(self, node):
        yield node
        for i in range(node.get_child_count()):
            child = node.get_child_at_index(i)
            if child:
                yield from self._walk(child)

    def _windows(self, pid):
        desktop = self.atspi.get_desktop(0)
        for i in range(desktop.get_child_count()):
            app = desktop.get_child_at_index(i)
            if app and app.get_process_id() == pid:
                return [app.get_child_at_index(n) for n in range(app.get_child_count())]
        raise RuntimeError("it tells screen readers nothing")

    def _pages(self, window):
        """What the tabs of a window show, in the tabs' order: panels that are named as their tabs are."""
        for node in self._walk(window):
            children = [node.get_child_at_index(i) for i in range(node.get_child_count())]
            pages = [c for c in children if c and c.get_role() == self.atspi.Role.PANEL and c.get_name()]
            if any(self._holds_terminal(page) for page in pages):
                return pages
        return []

    def _holds_terminal(self, page):
        return any(node.get_role() == self.atspi.Role.TERMINAL for node in self._walk(page))

    def _shown(self, window):
        """Index of the tab that is selected: only its page holds a terminal."""
        return next((i for i, page in enumerate(self._pages(window)) if self._holds_terminal(page)), None)

    def _do(self, window, action, times=1):
        for i in range(self.atspi.Action.get_n_actions(window)):
            if self.atspi.Action.get_action_name(window, i) == action:
                for _ in range(times):
                    self.atspi.Action.do_action(window, i)
                return
        raise RuntimeError(f"its windows cannot do {action}")


TOOLS = [JetBrains(), Ptyxis()]


def session_tool(chain):
    """(tool, its pid) for the supported tool the session runs in, else (None, None)."""
    for pid in chain:
        for tool in TOOLS:
            if tool.runs(pid):
                return tool, pid
    return None, None


def focus(session):
    chain = session["source"].chain(session)
    tool, pid = session_tool(chain)
    title = None
    if tool:
        try:
            title = tool.focus(pid, chain)
        except Exception as e:  # the window is still raised
            log(f"{tool.name}: cannot focus {label(session)}: {e}")
    backend, window, tried = find_window(session, chain, title)
    if window is None:
        log(f"no window found for {label(session)} ({'; '.join(tried) or 'no usable backend'})")
        return
    try:
        backend.activate(window["id"])
    except Exception as e:
        log(f"cannot focus window through {backend.name}: {e}")


# ------------------------------------------------------------------ main ---

def key_info(session, state, since, offset):
    """What render() draws a session's key from; `offset` shifts when its mascot appears."""
    meta = session["source"].info(session)
    if state == "busy":
        activity = meta.get("tool") or "thinking"
    elif state == "waiting":
        activity = session.get("waitingFor") or ""
    else:
        activity = ""  # colour and icon already say idle / done
    tokens = meta.get("tokens") or 0
    return {
        "name": label(session),
        "state": state,
        "activity": activity,
        "tokens": fmt_tokens(tokens),
        "model": fmt_model(meta.get("model")),
        "context": tokens / CONTEXT_WINDOW,
        "elapsed": fmt_duration(time.time() - since),
        "offset": offset,
    }


def attempt(action, *args):
    """Run a step that only serves finding windows: if it fails, the keys still have to be drawn."""
    try:
        action(*args)
    except Exception as e:
        log(f"{action.__name__} failed: {e!r}")


WOKEN, _wake = os.pipe()  # WOKEN can be read when the main loop has something new to look at
os.set_blocking(WOKEN, False)
_stopping = False


def wake():
    os.write(_wake, b".")


def stop(*signal):
    """End the main loop once it has finished what it is sending to a deck."""
    global _stopping
    _stopping = True
    wake()


class Login:
    """What the system's login manager tells: whether the screen is locked, and when the computer sleeps.

    Without PyGObject or logind neither is known, and both stay false."""

    NAME, PATH, MANAGER = "org.freedesktop.login1", "/org/freedesktop/login1", "org.freedesktop.login1.Manager"

    def __init__(self):
        self.locked = self.asleep = False     # wake() is called when one of them changes
        self._delay = None                    # open file for which the computer waits before it sleeps
        threading.Thread(target=self._watch, daemon=True).start()

    def _watch(self):
        try:
            from gi.repository import Gio, GLib
            self._gio, self._glib = Gio, GLib
            self._bus = Gio.bus_get_sync(Gio.BusType.SYSTEM)
            self._bus.signal_subscribe(self.NAME, self.MANAGER, "PrepareForSleep", self.PATH, None,
                                       Gio.DBusSignalFlags.NONE, self._sleep)
            self._bus.signal_subscribe(self.NAME, "org.freedesktop.DBus.Properties", "PropertiesChanged", None,
                                       "org.freedesktop.login1.Session", Gio.DBusSignalFlags.NONE, self._read)
            self._hold()
            self._read()
            GLib.MainLoop().run()
        except Exception as e:
            log(f"cannot tell a locked screen or sleep (is PyGObject installed?): {e}")

    def _call(self, path, interface, method, signature=None, *args):
        params = self._glib.Variant(signature, args) if signature else None
        return self._bus.call_with_unix_fd_list_sync(self.NAME, path, interface, method, params, None,
                                                     self._gio.DBusCallFlags.NONE, 2000, None, None)

    def _set(self, **state):
        if any(getattr(self, name) != value for name, value in state.items()):
            vars(self).update(state)
            wake()

    def _read(self, *signal):
        """Whether one of the user's sessions is locked."""
        try:
            sessions = self._call(self.PATH, self.MANAGER, "ListSessions")[0].unpack()[0]
            self._set(locked=any(
                self._call(path, "org.freedesktop.DBus.Properties", "Get", "(ss)",
                           "org.freedesktop.login1.Session", "LockedHint")[0].unpack()[0]
                for _, uid, _, _, path in sessions if uid == os.getuid()))
        except Exception as e:
            log(f"cannot tell whether the screen is locked: {e}")

    def _hold(self):
        """Make the computer wait with sleeping until ready() is called."""
        try:
            reply, files = self._call(self.PATH, self.MANAGER, "Inhibit", "(ssss)", "sleep",
                                      "claude-session-deck", "Darken the Stream Deck", "delay")
            self._delay = files.get(reply.unpack()[0])
        except Exception as e:
            log(f"cannot delay sleep, so the deck may stay lit: {e}")

    def _sleep(self, bus, sender, path, interface, signal, params):
        asleep = params.unpack()[0]
        if not asleep:  # woke up
            self._hold()
            self._read()
        self._set(asleep=asleep)

    def ready(self):
        """Let the computer sleep: the decks are dark."""
        delay, self._delay = self._delay, None
        if delay is not None:
            os.close(delay)


PLACES_KEPT = 60      # seconds after the daemon's start in which the keys of sessions that are not found are kept
SCAN_INTERVAL = 0.25  # seconds between looks at the sessions: often, to catch records before they are overwritten
JUST_STARTED = 15     # seconds after its start in which a session's window is taken to have the focus


class Sessions:
    """The live sessions and what their keys show; the same for every deck."""

    def __init__(self):
        self.sessions = {}     # as load_sessions() returns them
        self.by_age = []       # their keys, oldest session first
        self.slots = []        # the session on each key of a deck, None on a free one
        self.order = []        # the sessions in the order of their keys
        self.places = {}       # session -> the key it was put on by hand, see place()
        self.moving = None     # session that was picked up to be put on another key
        self.moving_until = 0  # when it is put down again where it was
        self.started = time.monotonic()
        try:
            saved = read_json(PLACES_FILE)
            self.places = {sid: slot for sid, slot in saved.items() if isinstance(slot, int) and slot >= 0}
        except (OSError, ValueError, AttributeError):
            pass
        self.infos = {}        # session -> key_info() of it
        self.last_status = {}  # session -> status last seen
        self.since = {}        # session -> epoch seconds the status was entered
        self.unseen = set()    # sessions that finished and were not visited yet
        self.cursor = None     # session that visit_next() went to last

    def scan(self):
        sessions = self.sessions = load_sessions()
        for sid in set(self.last_status) - set(sessions):  # ended sessions
            self.unseen.discard(sid)
            self.last_status.pop(sid, None)
            self.since.pop(sid, None)
        live_ids = {s["sessionId"] for s in sessions.values()}
        for stale in set(_transcripts) - live_ids:
            del _transcripts[stale]
        self.by_age = sorted(sessions, key=lambda sid: sessions[sid].get("startedAt") or 0)
        if self.moving not in sessions or time.monotonic() > self.moving_until:
            self.moving = None
        # Places of ended sessions are forgotten, but not right after the start: a sandbox reports its sessions late.
        if time.monotonic() - self.started > PLACES_KEPT and not self.places.keys() <= sessions.keys():
            self.places = {sid: slot for sid, slot in self.places.items() if sid in sessions}
            save_json(PLACES_FILE, self.places, "the keys of the sessions")
        self.arrange()
        self.infos = {}
        for place, sid in enumerate(self.by_age):
            session = sessions[sid]
            status = session.get("status", "?")
            previous = self.last_status.get(sid)
            if status != previous:
                if status == "idle" and previous == "busy":
                    self.unseen.add(sid)
                # A session that just started or got a prompt has the focus now.
                just_started = previous is None and \
                    time.time() - (session.get("startedAt") or 0) / 1000 < JUST_STARTED
                if just_started or (status == "busy" and previous in ("idle", "waiting")):
                    attempt(learn_window, session, set(sessions))
                stamp = session.get("statusUpdatedAt") or session.get("startedAt")
                self.since[sid] = stamp / 1000 if stamp else time.time()
                self.last_status[sid] = status
            if status != "idle":
                self.unseen.discard(sid)
            # The offset keeps the keys from showing the mascot in sync.
            self.infos[sid] = key_info(session, "done" if sid in self.unseen else status,
                                       self.since[sid], place * 7.3)

    def arrange(self):
        """Give every session a key: one that was put on a key by hand that key, the others the
        free keys in their order, oldest session first, so that they move up when a session ends."""
        taken = {}
        for sid, slot in list(self.places.items()):
            if sid in self.sessions:
                if slot in taken:  # only a damaged file says so
                    del self.places[sid]
                else:
                    taken[slot] = sid
        free = (slot for slot in itertools.count() if slot not in taken)
        for sid in self.by_age:
            if sid not in self.places:
                taken[next(free)] = sid
        self.slots = [taken.get(slot) for slot in range(max(taken, default=-1) + 1)]
        self.order = [sid for sid in self.slots if sid]

    def pick(self, sid):
        """Pick a session up, to be put on another key; returns whether there is such a session."""
        if sid not in self.sessions:
            return False
        self.moving, self.moving_until = sid, time.monotonic() + MOVE_SECONDS
        return True

    def place(self, slot, room):
        """Put the session that was picked up on key `slot` of a deck with `room` keys for sessions.

        It stays there. Sessions that were put on that key and the ones after it
        by hand move on by one key each, into the next row at the end of one; a
        session that would leave the deck this way takes a free key instead.
        The other sessions fill the free keys as ever, so one that had the key
        moves to the right if the session came from there, else to the left."""
        sid, self.moving = self.moving, None
        if sid not in self.sessions or not 0 <= slot < room or self.slots[slot:slot + 1] == [sid]:
            return  # nothing is picked up, the key is not for sessions, or it is put down where it was
        self.places.pop(sid, None)
        taken = {at: other for other, at in self.places.items() if other in self.sessions}
        while sid is not None and slot < room:
            self.places[sid] = slot
            sid, slot = taken.get(slot), slot + 1
        if sid is not None:
            del self.places[sid]
        save_json(PLACES_FILE, self.places, "the keys of the sessions")
        self.arrange()

    def visit(self, sid):
        """Jump to a session's window; returns whether there is such a session."""
        if sid not in self.sessions:
            return False
        self.unseen.discard(sid)
        attempt(focus, self.sessions[sid])
        return True

    def visit_next(self, state):
        """Jump to the next session in `state`, after the one last jumped to this way."""
        found = [sid for sid in self.order if self.infos[sid]["state"] == state]
        if not found:
            return False
        after = self.order.index(self.cursor) if self.cursor in self.order else -1
        self.cursor = next((sid for sid in found if self.order.index(sid) > after), found[0])
        return self.visit(self.cursor)

    def counts(self):
        """How many sessions are in each of PANELS' states."""
        states = [info["state"] for info in self.infos.values()]
        return [states.count(state) for state in PANELS]


def show(deck, board, now, frames):
    """Draw the sessions on a deck's keys; returns the frames a second that this needs.

    `frames` holds the images drawn for this moment, which decks with keys of one size share."""
    keys, size = deck.model.keys, deck.model.size
    # The account usage is on the deck's screen, or on the last key if it has none.
    usage = None if deck.model.screen else keys - 1
    deck.slots = (board.slots + [None] * keys)[:keys]
    if deck.model.screen:
        deck.set_screen(render_screen(deck.model, board.counts()))
    else:
        deck.slots[usage] = None
    rate = 2
    for key, sid in enumerate(deck.slots):
        info = board.infos.get(sid)
        if key == usage:
            image = usage_key(size, now)
        elif info is None:
            image = target(size) if board.moving else blank(size)
        else:
            if (sid, size) not in frames:
                frames[sid, size] = render(info, now, size)
            image = frames[sid, size]
            rate = max(rate, FPS if info["state"] in ANIMATED
                       else SCROLL_FPS if name_scroll(info["name"], now, size)[1] else 0)
            if sid == board.moving:
                if ("moving", size) not in frames:
                    frames["moving", size] = lifted(image, now)
                image, rate = frames["moving", size], FPS
        deck.set_image(key, image)
    return rate


def show_locked(deck, now, frames):
    """Draw the mascot across a deck's keys; returns the frames a second that this needs."""
    model = deck.model
    if ("locked", model.id) not in frames:
        frames["locked", model.id] = render_locked(model, now)
    deck.slots = [None] * model.keys
    if model.screen:
        deck.set_screen(Image.new("RGB", model.screen, (0, 0, 0)))
    for key, image in enumerate(frames["locked", model.id]):
        deck.set_image(key, image)
    return FPS if now % LOCK_EVERY < LOCK_SECONDS else 2


def act(deck, board, control, value):
    """Do what a press, turn or touch on a deck asks for; returns whether it jumped to a session."""
    model = deck.model
    if control in ("key", "hold"):
        if board.moving:  # any press puts it down: on that key, or where it was if the key is not for sessions
            board.place(value, model.keys - (0 if model.screen else 1))
            return False
        if control == "hold":
            board.pick(deck.slots[value])
            return False
        return board.visit(deck.slots[value])
    if control == "turn":  # any dial sets the brightness
        deck.dim(sum(value))
        return False
    if control == "touch":  # counts as a push of the dial under it
        value = value[0] * model.panels // model.screen[0]
    if control == "pad" or model.panels == 1:  # to whichever session needs the user most
        return board.visit_next("waiting") or board.visit_next("done")
    return value < len(PANELS) and board.visit_next(PANELS[value])


DECK_INTERVAL = 2  # seconds between looks for decks that appeared


def attach(decks, errors):
    """Open the decks that appeared. `errors` holds what was last logged about each path."""
    found = find_decks()
    for path in set(errors) - set(found):
        del errors[path]
    for path, product in found.items():
        if path in decks:
            continue
        try:
            decks[path] = Deck(path, product)
            error = None
            log(f"using {decks[path].model.name} at {path}")
        except (ConnectionRefusedError, DeckBusy):  # an emulator that ended, or one another daemon uses
            continue
        except OSError as e:  # not accessible yet, for example
            error = str(e)
        if error != errors.get(path):
            errors[path] = error
            if error:
                log(f"cannot use {path}: {error}")


def load_plugins():
    """Run the Python files in PLUGINS_DIR.

    A plugin imports this module as `claude_session_deck` and adds to SOURCES,
    TOOLS, BACKENDS, SANDBOXES or MODELS; see "Extending" in the README."""
    sys.modules.setdefault("claude_session_deck", sys.modules[__name__])  # not a second copy of this module
    for path in sorted(glob.glob(os.path.join(PLUGINS_DIR, "*.py"))):
        try:
            runpy.run_path(path)
            log(f"loaded plugin {path}")
        except Exception as e:
            log(f"cannot load plugin {path}: {type(e).__name__}: {e}")


def main():
    decks, errors = {}, {}  # both by path
    board = Sessions()
    login = Login()
    next_frame = next_scan = next_look = 0.0

    def lose(deck, error):  # unplugged, or its emulator ended
        log(f"lost {deck.path}: {error}")
        deck.close()
        del decks[deck.path]

    try:
        while not _stopping:
            wait = max(0.0, next_frame - time.monotonic())
            if any(deck.held for deck in decks.values()):
                wait = min(wait, HOLD_SECONDS / 10)  # to notice in time that a key is held
            ready = select.select([WOKEN, *(deck.fd for deck in decks.values())], [], [], wait)[0]
            if WOKEN in ready:
                os.read(WOKEN, 64)
                next_frame = 0.0  # show it at once
            for deck in [deck for deck in decks.values() if deck.fd in ready or deck.held]:
                try:
                    for control, value in deck.events():
                        if (login.locked or login.asleep) and control != "turn":
                            continue  # nobody is to jump to a window from a locked screen
                        if act(deck, board, control, value):
                            next_scan = 0.0
                        next_frame = 0.0  # show what it did at once
                except OSError as e:
                    lose(deck, e)
            now = time.monotonic()
            if now < next_frame:
                continue
            if login.asleep:  # going to sleep, or just woken up before that is told
                for deck in list(decks.values()):
                    try:
                        deck.darken()
                    except OSError as e:
                        lose(deck, e)
                login.ready()
                next_frame = now + 1
                continue
            if now >= next_look:
                next_look = now + DECK_INTERVAL
                attach(decks, errors)
            if not decks:
                next_frame = next_look
                continue
            if now >= next_scan:
                next_scan = now + SCAN_INTERVAL
                if login.locked:
                    board.moving = None
                board.scan()
            rate, frames = 2, {}
            for deck in list(decks.values()):
                try:
                    deck.lighten()
                    rate = max(rate, show_locked(deck, now, frames) if login.locked
                               else show(deck, board, now, frames))
                except OSError as e:
                    lose(deck, e)
            next_frame = now + 1 / rate
    finally:
        for deck in decks.values():
            try:
                deck.darken()
            except OSError:
                pass
            deck.close()


# white, red, yellow, green, blue
CALIBRATION_BANDS = [(255, 255, 255), (230, 40, 40), (240, 220, 0), (0, 190, 70), (40, 110, 255)]
CALIBRATION_BAND_PX = 3


def calibration_image(size):
    img = Image.new("RGB", (size, size), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, color in enumerate(CALIBRATION_BANDS):
        inset = i * CALIBRATION_BAND_PX
        d.rectangle((inset, inset, size - 1 - inset, size - 1 - inset), fill=color)
    inset = len(CALIBRATION_BANDS) * CALIBRATION_BAND_PX
    d.rectangle((inset, inset, size - 1 - inset, size - 1 - inset), fill=(0, 0, 0))
    d.line((size // 2, inset, size // 2, size - 1 - inset), fill=(90, 90, 90))
    d.line((inset, size // 2, size - 1 - inset, size // 2), fill=(90, 90, 90))
    return img


def calibrate():
    """Show nested colour frames on every key of every deck, revealing how much the keycaps hide."""
    decks = []
    for path, product in find_decks().items():
        try:
            decks.append(Deck(path, product))
        except OSError as e:
            log(f"cannot use {path}: {e}")
    if not decks:
        sys.exit("no Stream Deck found")
    for deck in decks:
        for key in range(deck.model.keys):
            deck.set_image(key, calibration_image(deck.model.size))
    print("Test pattern is showing. Press Ctrl+C to end.")
    try:
        while True:
            time.sleep(1)
    finally:
        for deck in decks:
            deck.close()


if __name__ == "__main__":
    load_plugins()
    if "--calibrate" in sys.argv[1:]:
        try:
            calibrate()
        except KeyboardInterrupt:
            pass
        sys.exit()
    signal.signal(signal.SIGTERM, stop)  # stopped, as at logout and shutdown: main() leaves the decks dark
    try:
        main()
    except KeyboardInterrupt:
        pass
