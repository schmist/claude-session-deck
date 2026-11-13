#!/usr/bin/env python3
"""Show one Stream Deck key per running Claude Code session; a key press focuses its window."""
import ctypes
import ctypes.util
import fcntl
import glob
import io
import json
import math
import os
import re
import random
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time

from PIL import Image, ImageDraw, ImageFont

SESSIONS_DIR = os.path.expanduser("~/.claude/sessions")
PROJECTS_DIR = os.path.expanduser("~/.claude/projects")
STATUS_FILE = os.path.expanduser("~/.claude/claude-session-deck/status.json")   # written by statusline.sh
WINDOWS_FILE = os.path.expanduser("~/.cache/claude-session-deck/windows.json")  # see learn_window()
RUNTIME_DIR = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}",
                           "claude-session-deck")                                # sockets of TOOLS
ELGATO_VENDOR = 0x0FD9
PRODUCTS = {0x006D, 0x0080, 0x00A5}  # "Original V2" protocol: 15 keys, 72 px JPEG, rotated 180°
KEYS, SIZE = 15, 72
USAGE_KEY = KEYS - 1        # bottom right
BRIGHTNESS = 60             # percent
FPS = 12                    # while something is animating
CONTEXT_WINDOW = 1_000_000  # tokens at which the context bar is full
SAFE_BOTTOM = 9             # bottom pixels hidden by the keycap rim
MASCOT_EVERY = 25           # seconds between mascot appearances while working
MASCOT_SECONDS = 6          # how long it stays
MODEL_EVERY, MODEL_SECONDS = 9, 3  # the model replaces the session's name for 3 s every 9 s
FONT = "/usr/share/fonts/truetype/ubuntu/Ubuntu[wdth,wght].ttf"
FALLBACK_FONT = "sans:bold"  # fontconfig pattern, used if FONT is missing


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- device ---

class Deck:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.pressed = [False] * KEYS
        self.shown = [None] * KEYS
        try:
            self._feature(bytes([0x03, 0x08, BRIGHTNESS]))
        except OSError:
            self.close()
            raise

    @staticmethod
    def find():
        for dev in glob.glob("/sys/class/hidraw/hidraw*"):
            try:
                with open(dev + "/device/uevent") as f:
                    uevent = f.read()
            except OSError:
                continue
            m = re.search(r"HID_ID=\w+:(\w+):(\w+)", uevent)
            if m and int(m[1], 16) == ELGATO_VENDOR and int(m[2], 16) in PRODUCTS:
                return "/dev/" + os.path.basename(dev)
        return None

    def _feature(self, payload):
        buf = bytearray(payload.ljust(32, b"\0"))
        hidiocsfeature = (3 << 30) | (len(buf) << 16) | (ord("H") << 8) | 0x06
        fcntl.ioctl(self.fd, hidiocsfeature, buf)

    def _write(self, data):
        while True:
            try:
                os.write(self.fd, data)
                return
            except BlockingIOError:
                select.select([], [self.fd], [], 1)

    def set_image(self, key, image):
        """Draw `image` on `key`; skipped if `image` is what is shown already."""
        pixels = image.tobytes()
        if self.shown[key] == pixels:
            return
        buf = io.BytesIO()
        image.rotate(180).save(buf, "JPEG", quality=90)
        data, page = buf.getvalue(), 0
        while data:
            chunk, data = data[:1016], data[1016:]
            header = bytes([0x02, 0x07, key, 0 if data else 1,
                            len(chunk) & 0xFF, len(chunk) >> 8,
                            page & 0xFF, page >> 8])
            self._write((header + chunk).ljust(1024, b"\0"))
            page += 1
        self.shown[key] = pixels

    def poll_presses(self, timeout):
        """Wait up to `timeout` seconds; return keys that were newly pressed."""
        presses = []
        if not select.select([self.fd], [], [], timeout)[0]:
            return presses
        while True:
            try:
                report = os.read(self.fd, 512)
            except BlockingIOError:
                break
            if len(report) < 4 + KEYS or report[0] != 0x01:
                continue
            states = [bool(b) for b in report[4:4 + KEYS]]
            presses += [k for k in range(KEYS) if states[k] and not self.pressed[k]]
            self.pressed = states
        return presses

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


def load_sessions():
    """Live interactive sessions, keyed by the identity of their process."""
    own_ns = os.readlink("/proc/self/ns/pid")
    sessions = {}
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            s = read_json(path)
        except (OSError, ValueError):
            continue
        # A file from another pid namespace belongs to a container: see below.
        domain = re.search(r"pid:\[\d+\]", s.get("pidDomain", ""))
        start = proc_start(s.get("pid"))
        if (domain and domain[0] != own_ns) or not is_session(s, start):
            continue
        s.update(hostPid=s["pid"], key=f"{s['pid']}:{start}")
        sessions[s["key"]] = s
    sessions.update(container_sessions(own_ns))
    for found in list(_sandboxed.values()):
        sessions.update(found)
    # Sessions sharing a folder are told apart by the name Claude gave them.
    counts = {}
    for s in sessions.values():
        counts[label(s)] = counts.get(label(s), 0) + 1
    for s in sessions.values():
        if counts[label(s)] > 1 and s.get("name"):
            s["label"] = s["name"]
    return sessions


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


def container_sessions(own_ns):
    """Sessions of Claude processes in containers, from the session file each container sees."""
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
        s.update(hostPid=pid, key=f"{pid}:{start}", container=True, projects=os.path.join(config, "projects"))
        found[s["key"]] = s
    return found


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
    if "meta" in session:  # read inside its sandbox, see watch_sandbox()
        return session["meta"]
    sid = session["sessionId"]
    cached = _transcripts.get(sid)
    path = cached[0] if cached else None
    if not path:
        projects = session.get("projects", PROJECTS_DIR)
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

SS = 4  # graphics are drawn supersampled and scaled down; text is drawn at 1x
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
NAME_WIDTH = SIZE - 2 * PAD  # room for the name
TRACK = (48, 52, 60)         # the empty part of a bar
_gradient = Image.linear_gradient("L").resize((SIZE * SS, SIZE * SS)).point(lambda v: 255 - v)
BLANK = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))


def background(accent, tint):
    """A key's supersampled background: `accent` at the top, fading out towards the bottom."""
    big = SIZE * SS
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


def name_scroll(name, t):
    """(pixels a name too wide for its key is moved left at time `t`, whether it is moving).

    Each time the name appears it rests for a second, moves until its end shows and rests there."""
    over = font(NAME_SIZE).getlength(name) - NAME_WIDTH
    if over <= 0:
        return 0, False
    shown = MODEL_EVERY - MODEL_SECONDS            # seconds the name is shown for
    moves = min(over / 16, shown - 2)              # at 16 px a second, faster if that takes too long
    done = (t % MODEL_EVERY - 1) / moves
    return round(over * min(1, max(0, done))), 0 < done < 1


MASCOT_COLOR = (217, 119, 87)
MASCOT_ROWS, MASCOT_COLS = 7, 12
# animation -> relative probability
MASCOT_ANIMATIONS = {"walk": 35, "think": 25, "peek": 20, "wave": 20}


def draw_sprite(g, x, y, px, feet="stand", look=0, blink=False, arms=(0, 0), color=None):
    """Draw the creature at (x, y), `px` pixels per cell; `feet` is "stand" or step 0/1,
    `look` shifts the eyes by -1/0/1 and `arms` raises each side's arm."""
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
        g.rectangle(((x + col * px) * SS, (y + row * px) * SS,
                     (x + (col + 1) * px) * SS - 1, (y + (row + 1) * px) * SS - 1), fill=color)

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


def draw_mascot(g, kind, phase, top, bottom):
    """One of the mascot animations; `phase` is seconds since it started and
    `top`/`bottom` bound the area it may use (1x pixels)."""
    px = 4 if bottom - top >= 34 else 3
    w, h = MASCOT_COLS * px, MASCOT_ROWS * px
    centre = (SIZE - w) / 2
    floor = round(top + (bottom - top - h) / 2)  # stands centred in its area
    if kind == "think":  # leave room for the dots over its head
        floor = min(bottom - h, max(floor, top + 2 * px + 1))
    left = MASCOT_SECONDS - phase
    step = int(phase / 0.16) % 2

    if kind == "walk":  # walks in, hops and blinks, walks off
        walk = 1.6
        if phase < walk:
            draw_sprite(g, -w + (centre + w) * phase / walk, floor - step, px, feet=step)
        elif left < walk:
            draw_sprite(g, centre + (SIZE - centre) * (1 - left / walk), floor - step, px, feet=step)
        else:
            hop = abs(math.sin((phase - walk) * math.pi / 0.7)) * min(5, floor - top)
            draw_sprite(g, centre, floor - hop, px, blink=(phase - walk) % 1.4 > 1.25)
        return

    # The others rise from the bottom edge and sink back at the end.
    rise = min(1.0, phase / 0.5, left / 0.5)
    y = floor + (SIZE - floor) * (1 - rise)
    inner = phase - 0.5
    if kind == "think":  # looks around while dots pop up over its head
        look = (0, -1, 0, 1)[int(inner / 0.9) % 4] if rise == 1 else 0
        draw_sprite(g, centre, y, px, look=look, blink=inner % 2.2 > 2.05)
        if rise == 1:
            for i in range(int(inner / 0.45) % 4):
                dx = centre + w / 2 - 2.5 * px + i * 2 * px
                g.rectangle((dx * SS, (y - 2 * px) * SS, (dx + px) * SS - 1, (y - px) * SS - 1),
                            fill=mix(MASCOT_COLOR, (255, 255, 255), 0.5))
    elif kind == "peek":  # peeks over the edge, glances both ways, pops up
        if inner < 2.4:
            y = max(y, floor + h * 0.55)
            look = -1 if inner < 1.2 else 1
        else:
            y = min(y, floor + h * 0.55 * max(0.0, 1 - (inner - 2.4) / 0.25)) if rise == 1 else y
            look = 0
        draw_sprite(g, centre, y, px, look=look if rise == 1 else 0, blink=3.6 < inner < 3.8)
    else:  # wave: swings its arms, with a little bounce
        beat = int(inner / 0.3) % 2 if rise == 1 else None
        arms = (0, 0) if beat is None else (1, 0) if beat else (0, 1)
        bounce = abs(math.sin(inner * math.pi / 0.6)) * 2 if rise == 1 else 0
        draw_sprite(g, centre, y - bounce, px, arms=arms, blink=inner % 1.8 > 1.65)


def render(info, t):
    """Key image for a session; `t` (seconds) drives the animations."""
    state = info["state"]
    # Now and then the model takes the place of the session's name, on all keys at once.
    show_model = bool(info["model"]) and t % MODEL_EVERY >= MODEL_EVERY - MODEL_SECONDS
    name_font = font(NAME_SIZE)
    name = fit(info["model"], name_font, NAME_WIDTH) if show_model else info["name"]
    shift = name_scroll(name, t)[0]
    if state not in ANIMATED:  # then only these make one image differ from the next
        drawn = (tuple(info.items()), name, shift)
        if drawn not in _still:
            if len(_still) > 4 * KEYS:
                _still.clear()
            _still[drawn] = draw_key(info, t, show_model, name, shift)
        return _still[drawn]
    return draw_key(info, t, show_model, name, shift)


_still = {}  # what a key without animation was drawn from -> its image


def draw_key(info, t, show_model, name, shift):
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

    big = SIZE * SS
    img = background(accent, tint)
    g = ImageDraw.Draw(img)
    name_font = font(NAME_SIZE)
    name_bottom = 5 + NAME_SIZE
    small = font(10, "Condensed Medium")
    time_y = SIZE - SAFE_BOTTOM - 9   # centre of the time row
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
        draw_mascot(g, kind, mascot, name_bottom + 2, SIZE - SAFE_BOTTOM)
    else:
        draw_icon(g, state, PAD, time_y - round(MASCOT_ROWS * ICON_PX / 2), accent, t)
        if info["tokens"]:
            x2 = (SIZE - PAD - small.getlength(info["tokens"]) - 4) * SS
            box = (PAD * SS, (bar_y - 2) * SS, x2, (bar_y + 2) * SS)
            g.rounded_rectangle(box, 2 * SS, fill=TRACK)
            used = min(1.0, info["context"])
            g.rounded_rectangle((box[0], box[1], box[0] + max(4 * SS, (x2 - box[0]) * used), box[3]),
                                2 * SS, fill=level_color(used, (165, 172, 185)))
    img = img.resize((SIZE, SIZE), Image.LANCZOS)

    # --- text, at 1x --------------------------------------------------------
    d = ImageDraw.Draw(img)
    dim = (150, 156, 168) if state != "idle" else (110, 116, 128)
    soft = mix(accent, (255, 255, 255), 0.25)
    color = (255, 255, 255) if state != "idle" else (190, 195, 205)
    if show_model:  # tinted, to tell it from the name
        color = mix(accent, color, 0.35)
    # The name is drawn on its own strip, which cuts off what is scrolled out of it.
    box = (PAD, 0, PAD + NAME_WIDTH, name_bottom + 4)
    strip = img.crop(box)
    if mascot is not None and name_font.getlength(name) <= NAME_WIDTH:  # centred like the mascot below it
        ImageDraw.Draw(strip).text((strip.width / 2, 5), name, font=name_font, fill=color, anchor="ma")
    else:
        ImageDraw.Draw(strip).text((-shift, 5), name, font=name_font, fill=color)
    img.paste(strip, box)
    if mascot is not None:
        return img
    big_font = font(18)
    time_x = PAD + round(MASCOT_COLS * ICON_PX) + 3
    d.text((time_x, time_y), info["elapsed"], font=big_font,
           fill=mix(accent, (255, 255, 255), 0.3), anchor="lm")
    if info["activity"]:
        line = font(11, "Condensed Medium")
        d.text((PAD, name_bottom + 1), fit(info["activity"], line, NAME_WIDTH), font=line, fill=soft)
    d.text((SIZE - PAD, bar_y), info["tokens"], font=small, fill=dim, anchor="rm")
    return img


_usage = (None, None)  # (mtime of STATUS_FILE, its rate limits)


def account_usage():
    """The account's usage windows as [(name, used fraction, seconds to reset)]."""
    global _usage
    try:
        mtime = os.stat(STATUS_FILE).st_mtime
        if _usage[0] != mtime:
            _usage = (mtime, read_json(STATUS_FILE).get("rate_limits") or {})
    except (OSError, ValueError):
        return []
    windows = []
    for name, field in (("5h", "five_hour"), ("week", "seven_day")):
        window = _usage[1].get(field)
        if not window:
            continue
        left = (window.get("resets_at") or 0) - time.time()
        # Once a window has reset, the last reported figure no longer applies.
        used = (window.get("used_percentage") or 0) / 100 if left > 0 else 0.0
        windows.append((name, used, max(0, left)))
    return windows


def render_usage():
    """Key showing how much of the account's usage limits is used."""
    windows = account_usage()
    big = SIZE * SS
    worst = max((used for _, used, _ in windows), default=0)
    accent = level_color(worst, MASCOT_COLOR)
    img = background(accent, 0.14)
    g = ImageDraw.Draw(img)
    g.rectangle((0, 0, big, 3 * SS), fill=mix(BASE, accent, 0.7))
    rows = [7 + i * 27 for i in range(len(windows))]  # top of each window's block
    for y, (_, used, _) in zip(rows, windows):
        box = (PAD * SS, (y + 16) * SS, (SIZE - PAD) * SS, (y + 21) * SS)
        g.rounded_rectangle(box, 2.5 * SS, fill=TRACK)
        g.rounded_rectangle((box[0], box[1], box[0] + max(5 * SS, (box[2] - box[0]) * min(1, used)),
                             box[3]), 2.5 * SS, fill=level_color(used, MASCOT_COLOR))
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    dim = (150, 156, 168)
    if not windows:
        d.text((SIZE / 2, 24), "usage", font=font(15), fill=(255, 255, 255), anchor="mm")
        d.text((SIZE / 2, 42), "no data yet", font=font(10, "Condensed Medium"), fill=dim, anchor="mm")
        return img
    small = font(10, "Condensed Medium")
    for y, (name, used, left) in zip(rows, windows):
        d.text((PAD, y + 13), name, font=font(11), fill=(255, 255, 255), anchor="ls")
        x = PAD + font(11).getlength(name) + 3
        # Time until the window resets, in whole hours once minutes stop mattering.
        reset = f"{int(left // 3600)}h" if 10 * 3600 <= left < 2 * 86400 else fmt_duration(left)
        d.text((x, y + 13), reset, font=small, fill=dim, anchor="ls")
        d.text((SIZE - PAD, y + 13), f"{round(used * 100)}%", font=font(15), fill=(255, 255, 255), anchor="rs")
    return img


# ------------------------------------------------------------- sandboxes ---
# A container shares the host's kernel, so its processes and files show up in
# /proc and container_sessions() finds them. A virtual machine or another host
# shows neither; its sessions are reported by a script that runs inside it.

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
    metas = {}                  # session id -> transcript_info() of it
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


# Focusing a window differs per desktop, so each backend offers activate(id) and
# windows() -> [{"id", "pid", "title", "focus"}], or None if it does not apply here.

def run_json(*cmd):
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=3, check=True).stdout
    return json.loads(out)


class GnomeShell:
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


class Sway:
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


class Hyprland:
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


class KWin:
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


class X11:
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


def session_chain(session):
    """Pids of the session's parent processes, up to its terminal or IDE."""
    if session.get("sandbox"):
        return ancestors(sandbox_client(session))
    chain = ancestors(session["hostPid"])
    if session.get("container"):
        chain += ancestors(docker_client(session))
    return chain


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
    chain = session_chain(session)
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
    try:
        os.makedirs(os.path.dirname(WINDOWS_FILE), exist_ok=True)
        with open(WINDOWS_FILE + ".tmp", "w") as out:
            json.dump(learned, out)
        os.replace(WINDOWS_FILE + ".tmp", WINDOWS_FILE)
    except OSError as e:
        log("cannot save learned windows:", e)


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


TOOLS = [JetBrains()]


def session_tool(chain):
    """(tool, its pid) for the supported tool the session runs in, else (None, None)."""
    for pid in chain:
        for tool in TOOLS:
            if tool.runs(pid):
                return tool, pid
    return None, None


def focus(session):
    chain = session_chain(session)
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
    meta = transcript_info(session)
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


SCAN_INTERVAL = 0.25  # seconds between looks at the sessions: often, to catch records before they are overwritten
JUST_STARTED = 15     # seconds after its start in which a session's window is taken to have the focus


def run(deck):
    slots = [None] * KEYS   # session shown on each key
    last_status = {}        # session -> status last seen
    since = {}              # session -> epoch seconds the status was entered
    unseen = set()          # sessions that finished and were not visited yet
    infos = [None] * KEYS
    next_frame = next_scan = usage_until = 0.0
    sessions = {}
    while True:
        for key in deck.poll_presses(max(0.0, next_frame - time.monotonic())):
            sid = slots[key]
            if sid in sessions:
                unseen.discard(sid)
                attempt(focus, sessions[sid])
                next_scan = 0.0
        now = time.monotonic()
        if now < next_frame:
            continue

        if now >= next_scan:
            next_scan = now + SCAN_INTERVAL
            sessions = load_sessions()
            for sid in set(last_status) - set(sessions):  # ended sessions
                unseen.discard(sid)
                last_status.pop(sid, None)
                since.pop(sid, None)
            live_ids = {s["sessionId"] for s in sessions.values()}
            for stale in set(_transcripts) - live_ids:
                del _transcripts[stale]
            # Oldest session first, so later ones move up when a session ends.
            order = sorted(sessions, key=lambda sid: sessions[sid].get("startedAt") or 0)
            slots = (order + [None] * KEYS)[:KEYS]
            slots[USAGE_KEY] = None  # reserved for the account usage
            for key, sid in enumerate(slots):
                if not sid:
                    infos[key] = None
                    continue
                session = sessions[sid]
                status = session.get("status", "?")
                previous = last_status.get(sid)
                if status != previous:
                    if status == "idle" and previous == "busy":
                        unseen.add(sid)
                    # A session that just started or got a prompt has the focus now.
                    just_started = previous is None and \
                        time.time() - (session.get("startedAt") or 0) / 1000 < JUST_STARTED
                    if just_started or (status == "busy" and previous in ("idle", "waiting")):
                        attempt(learn_window, session, set(sessions))
                    stamp = session.get("statusUpdatedAt") or session.get("startedAt")
                    since[sid] = stamp / 1000 if stamp else time.time()
                    last_status[sid] = status
                if status != "idle":
                    unseen.discard(sid)
                # The offset keeps the keys from showing the mascot in sync.
                infos[key] = key_info(session, "done" if sid in unseen else status, since[sid], key * 7.3)

        animated = scrolling = False
        for key, info in enumerate(infos):
            if key == USAGE_KEY:
                if now >= usage_until:  # it shows nothing finer than seconds
                    usage, usage_until = render_usage(), now + 1
                deck.set_image(key, usage)
            elif info is None:
                deck.set_image(key, BLANK)
            else:
                animated |= info["state"] in ANIMATED
                scrolling |= name_scroll(info["name"], now)[1]
                deck.set_image(key, render(info, now))
        next_frame = now + (1 / FPS if animated else 1 / SCROLL_FPS if scrolling else 0.5)


def main():
    threading.Thread(target=watch_sandboxes, daemon=True).start()
    while True:
        path = Deck.find()
        if not path:
            time.sleep(3)
            continue
        deck = None
        try:
            deck = Deck(path)
            log("using", path)
            run(deck)
        except OSError as e:  # unplugged, or not accessible yet
            log("deck error:", e)
            time.sleep(3)
        finally:
            if deck:
                try:
                    for key in range(KEYS):
                        deck.shown[key] = None
                        deck.set_image(key, BLANK)
                except OSError:
                    pass
                deck.close()


# white, red, yellow, green, blue
CALIBRATION_BANDS = [(255, 255, 255), (230, 40, 40), (240, 220, 0), (0, 190, 70), (40, 110, 255)]
CALIBRATION_BAND_PX = 3


def calibrate():
    """Show nested colour frames on every key, revealing how much the keycaps hide."""
    img = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))
    d = ImageDraw.Draw(img)
    for i, color in enumerate(CALIBRATION_BANDS):
        inset = i * CALIBRATION_BAND_PX
        d.rectangle((inset, inset, SIZE - 1 - inset, SIZE - 1 - inset), fill=color)
    inset = len(CALIBRATION_BANDS) * CALIBRATION_BAND_PX
    d.rectangle((inset, inset, SIZE - 1 - inset, SIZE - 1 - inset), fill=(0, 0, 0))
    d.line((SIZE // 2, inset, SIZE // 2, SIZE - 1 - inset), fill=(90, 90, 90))
    d.line((inset, SIZE // 2, SIZE - 1 - inset, SIZE // 2), fill=(90, 90, 90))
    path = Deck.find()
    if not path:
        sys.exit("no Stream Deck found")
    deck = Deck(path)
    for key in range(KEYS):
        deck.set_image(key, img)
    print("Test pattern is showing. Press Ctrl+C to end.")
    try:
        while True:
            time.sleep(1)
    finally:
        deck.close()


if __name__ == "__main__":
    if "--calibrate" in sys.argv[1:]:
        try:
            calibrate()
        except KeyboardInterrupt:
            pass
        sys.exit()
    try:
        main()
    except KeyboardInterrupt:
        pass
