#!/usr/bin/env python3
"""Show one Stream Deck key per running Claude Code session; a key press focuses its window."""
import ctypes
import ctypes.util
import datetime
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
MODEL_EVERY, MODEL_SECONDS = 9, 3  # the model replaces the small creature for 3 s every 9 s
FONT = "/usr/share/fonts/truetype/ubuntu/Ubuntu[wdth,wght].ttf"
FALLBACK_FONT = "sans:bold"  # fontconfig pattern, used if FONT is missing
BOOT_TIME = time.time() - float(open("/proc/uptime").read().split()[0])


def log(*args):
    print(*args, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- device ---

class Deck:
    def __init__(self, path):
        self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.pressed = [False] * KEYS
        self.shown = [None] * KEYS
        self._feature(bytes([0x03, 0x08, BRIGHTNESS]))

    @staticmethod
    def find():
        for dev in glob.glob("/sys/class/hidraw/hidraw*"):
            try:
                uevent = open(dev + "/device/uevent").read()
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
        if self.shown[key] == image.tobytes():
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
        self.shown[key] = image.tobytes()

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

def proc_start(pid):
    try:
        stat = open(f"/proc/{pid}/stat").read()
    except OSError:
        return None
    return stat.rsplit(")", 1)[1].split()[19]  # field 22: starttime


def load_sessions():
    """Live interactive sessions, keyed by the identity of their process."""
    own_ns = os.readlink("/proc/self/ns/pid")
    sessions = {}
    for path in glob.glob(os.path.join(SESSIONS_DIR, "*.json")):
        try:
            s = json.load(open(path))
        except (OSError, ValueError):
            continue
        if s.get("kind", "interactive") != "interactive" or not s.get("sessionId"):
            continue
        # A file from another pid namespace mixes the fields of all containers: see below.
        domain = re.search(r"pid:\[\d+\]", s.get("pidDomain", ""))
        start = str(s.get("procStart"))
        if (domain and domain[0] != own_ns) or proc_start(s.get("pid")) != start:
            continue
        s["hostPid"] = s["pid"]
        sessions[s["pid"], start] = s
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


def container_sessions(own_ns):
    """Claude processes in containers, described from their transcripts."""
    # They are all pid 1 and patch the same session file, so that file cannot tell them apart.
    found = {}
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            if open(f"/proc/{entry}/comm").read().strip() != "claude":
                continue
            ns = os.readlink(f"/proc/{entry}/ns/pid")
            fields = open(f"/proc/{entry}/stat").read().rsplit(")", 1)[1].split()
            parent, tty, start = fields[1], fields[4], fields[19]
            if ns == own_ns or tty == "0" \
                    or open(f"/proc/{parent}/comm").read().strip() == "claude":
                continue
            cwd = os.readlink(f"/proc/{entry}/cwd")
        except (OSError, IndexError):
            continue
        own = container_own_session(entry, start)
        if own:
            found[int(entry), start] = own
            continue
        started = (BOOT_TIME + int(start) / os.sysconf("SC_CLK_TCK")) * 1000
        s = {"sessionId": container_session_id(entry), "pid": None, "hostPid": int(entry),
             "cwd": cwd, "status": "unknown", "startedAt": started}
        if s["sessionId"]:
            meta = transcript_info(s)
            s.update(status=meta.get("status", "idle"), waitingFor="input needed",
                     statusUpdatedAt=meta.get("since", started),
                     name=meta.get("custom-title") or meta.get("ai-title"),
                     nameSource="user" if meta.get("custom-title") else "derived")
        found[int(entry), start] = s
    return found


def container_own_session(pid, start):
    """Session of a containerised process, from the container's own Claude folder.

    None if the container uses the host's folder, or the file cannot be read."""
    try:
        env = dict(v.split("=", 1) for v in open(f"/proc/{pid}/environ").read().split("\0") if "=" in v)
        inner = env.get("CLAUDE_CONFIG_DIR") or os.path.join(env.get("HOME", "/"), ".claude")
        config = f"/proc/{pid}/root/{inner.lstrip('/')}"  # the container's files, as the host sees them
        if os.path.samefile(config, os.path.dirname(SESSIONS_DIR)):
            return None
        inner_pid = re.search(r"^NSpid:.*?(\d+)$", open(f"/proc/{pid}/status").read(), re.M)[1]
        s = json.load(open(os.path.join(config, "sessions", inner_pid + ".json")))
    except (OSError, ValueError, TypeError):
        return None
    if s.get("kind", "interactive") != "interactive" or not s.get("sessionId") \
            or str(s.get("procStart")) != start:
        return None
    s.update(hostPid=int(pid), projects=os.path.join(config, "projects"))
    return s


def container_session_id(pid):
    """Id of the session a containerised process runs, named by a folder in its /tmp."""
    def recency(folder):  # the session written to last is the current one
        slug, sid = folder.split("/")[-2:]
        times = []
        for path in (os.path.join(PROJECTS_DIR, slug, sid + ".jsonl"), folder):
            try:
                times.append(os.stat(path).st_mtime)
            except OSError:
                times.append(0)
        return times
    folders = glob.glob(f"/proc/{pid}/root/tmp/claude-*/*/*-*-*-*-*")
    return os.path.basename(max(folders, key=recency)) if folders else ""


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
TITLE_FIELDS = {"custom-title": "customTitle", "ai-title": "aiTitle"}  # entry type -> field


def transcript_info(session):
    """Context size, current tool, titles and state, read from the transcript tail."""
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
    for key in ("tool", "status", "since"):
        info.pop(key, None)
    seen_turn = seen_usage = switched = False
    titles = set()
    for line in reversed(lines):
        if seen_usage and '-title"' not in line:
            continue
        switch = not seen_usage and not switched and MODEL_SWITCH.search(line)
        if switch:  # a /model command newer than the last reply
            info["model"], switched = switch[1], True
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        if kind in TITLE_FIELDS:
            if kind not in titles:
                titles.add(kind)
                info[kind] = entry.get(TITLE_FIELDS[kind])
            continue
        if entry.get("isSidechain") or entry.get("isMeta") or kind not in ("assistant", "user"):
            continue
        msg = entry.get("message") or {}
        if not seen_turn:
            seen_turn = True
            content = msg.get("content")
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            tools = [b.get("name") or "" for b in blocks if b.get("type") == "tool_use"]
            text = content if isinstance(content, str) else "".join(b.get("text") or "" for b in blocks)
            if kind == "user":
                info["status"] = "idle" if text.startswith("[Request interrupted") else "busy"
            elif msg.get("stop_reason") not in (None, "tool_use"):
                info["status"] = "idle"
            else:
                info["status"] = "waiting" if tools[-1:] in (["AskUserQuestion"], ["ExitPlanMode"]) else "busy"
                if tools:
                    info["tool"] = tools[-1].split("__")[-1]
            try:
                stamp = entry.get("timestamp", "").replace("Z", "+00:00")
                info["since"] = datetime.datetime.fromisoformat(stamp).timestamp() * 1000
            except ValueError:
                pass
        usage = msg.get("usage")
        if not seen_usage and kind == "assistant" and usage:
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
    """Short form of a model: family initial and version, "claude-opus-5-5" or "Opus 5.5" -> "O5.5"."""
    parts = re.split(r"[-\s.]", model or "")
    family = [p for p in parts if p.isalpha() and p != "claude"]
    version = [p for p in parts if p.isdigit() and len(p) < 3]  # not a date
    return family[0][0].upper() + ".".join(version[:2]) if family else ""


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
_gradient = Image.linear_gradient("L").resize((SIZE * SS, SIZE * SS)).point(lambda v: 255 - v)
BLANK = Image.new("RGB", (SIZE, SIZE), (0, 0, 0))


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


NAME_SIZES = (16, 15, 14, 13, 12, 11, 10)  # font sizes tried in turn for the name


def fit_name(name):
    """The name and the largest font size at which it fits on one line, shortened if none does."""
    width = SIZE - 8
    for size in NAME_SIZES:
        if font(size).getlength(name) <= width:
            return name, size
    return fit(name, font(size), width), size


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
    accent = ACCENT.get(state, ACCENT["idle"])
    pulse = 0.0
    if state == "waiting":
        pulse = 0.5 - 0.5 * math.cos(t * 2 * math.pi / 1.1)
        tint = 0.22 + 0.5 * pulse
    elif state == "done":
        pulse = 0.5 - 0.5 * math.cos(t * 2 * math.pi / 3.0)
        tint = 0.26 + 0.16 * pulse
    else:
        tint = {"busy": 0.2}.get(state, 0.07)
    mascot = None
    if state == "busy":
        phase = (t + info.get("offset", 0)) % MASCOT_EVERY
        mascot = phase if phase < MASCOT_SECONDS else None
        # Pick the animation at random, but the same one for a whole appearance.
        rng = random.Random(f"{info.get('offset', 0)}:{(t + info.get('offset', 0)) // MASCOT_EVERY}")
        kind = rng.choices(list(MASCOT_ANIMATIONS), weights=MASCOT_ANIMATIONS.values())[0]

    # Now and then the model takes the place of the small creature, on all keys at once.
    show_model = bool(info.get("model")) and t % MODEL_EVERY >= MODEL_EVERY - MODEL_SECONDS

    big = SIZE * SS
    img = Image.composite(Image.new("RGB", (big, big), mix(BASE, accent, tint)),
                          Image.new("RGB", (big, big), BASE), _gradient)
    g = ImageDraw.Draw(img)
    name, size = fit_name(info["name"])
    name_bottom = 5 + size
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
        if not show_model:
            draw_icon(g, state, 4, time_y - round(MASCOT_ROWS * ICON_PX / 2), accent, t)
        if info.get("tokens"):
            x2 = (SIZE - 4 - small.getlength(info["tokens"]) - 4) * SS
            box = (4 * SS, (bar_y - 2) * SS, x2, (bar_y + 2) * SS)
            g.rounded_rectangle(box, 2 * SS, fill=(48, 52, 60))
            used = min(1.0, info.get("context", 0))
            fill = (255, 80, 80) if used > 0.85 else (255, 190, 70) if used > 0.65 else (165, 172, 185)
            g.rounded_rectangle((box[0], box[1], box[0] + max(4 * SS, (x2 - box[0]) * used), box[3]),
                                2 * SS, fill=fill)
    img = img.resize((SIZE, SIZE), Image.LANCZOS)

    # --- text, at 1x --------------------------------------------------------
    d = ImageDraw.Draw(img)
    dim = (150, 156, 168) if state != "idle" else (110, 116, 128)
    soft = mix(accent, (255, 255, 255), 0.25)
    color = (255, 255, 255) if state != "idle" else (190, 195, 205)
    if mascot is not None:  # centred like the mascot below it
        d.text((SIZE / 2, 5), name, font=font(size), fill=color, anchor="ma")
        return img
    d.text((4, 5), name, font=font(size), fill=color)
    big_font = font(18)
    time_x = 4 + round(MASCOT_COLS * ICON_PX) + 3
    d.text((time_x, time_y), info["elapsed"], font=big_font,
           fill=mix(accent, (255, 255, 255), 0.3), anchor="lm")
    if info.get("activity"):
        line = font(11, "Condensed Medium")
        d.text((4, name_bottom + 1), fit(info["activity"], line, SIZE - 8), font=line, fill=soft)
    if show_model:  # in the place of the small creature
        d.text((4 + MASCOT_COLS * ICON_PX / 2, time_y), info["model"], font=font(11),
               fill=mix(accent, (255, 255, 255), 0.3), anchor="mm")
    d.text((SIZE - 4, bar_y), info.get("tokens", ""), font=small, fill=dim, anchor="rm")
    return img


_usage = (None, None)  # (mtime of STATUS_FILE, its rate limits)


def account_usage():
    """The account's usage windows as [(name, used fraction, seconds to reset)]."""
    global _usage
    try:
        mtime = os.stat(STATUS_FILE).st_mtime
        if _usage[0] != mtime:
            _usage = (mtime, json.load(open(STATUS_FILE)).get("rate_limits") or {})
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
    accent = (255, 80, 80) if worst > 0.85 else (255, 190, 70) if worst > 0.65 else MASCOT_COLOR
    img = Image.composite(Image.new("RGB", (big, big), mix(BASE, accent, 0.14)),
                          Image.new("RGB", (big, big), BASE), _gradient)
    g = ImageDraw.Draw(img)
    g.rectangle((0, 0, big, 3 * SS), fill=mix(BASE, accent, 0.7))
    rows = [7 + i * 27 for i in range(len(windows))]  # top of each window's block
    for y, (_, used, _) in zip(rows, windows):
        box = (4 * SS, (y + 16) * SS, (SIZE - 4) * SS, (y + 21) * SS)
        g.rounded_rectangle(box, 2.5 * SS, fill=(48, 52, 60))
        fill = (255, 80, 80) if used > 0.85 else (255, 190, 70) if used > 0.65 else MASCOT_COLOR
        g.rounded_rectangle((box[0], box[1], box[0] + max(5 * SS, (box[2] - box[0]) * min(1, used)),
                             box[3]), 2.5 * SS, fill=fill)
    img = img.resize((SIZE, SIZE), Image.LANCZOS)
    d = ImageDraw.Draw(img)
    dim = (150, 156, 168)
    if not windows:
        d.text((SIZE / 2, 24), "usage", font=font(15), fill=(255, 255, 255), anchor="mm")
        d.text((SIZE / 2, 42), "no data yet", font=font(10, "Condensed Medium"), fill=dim, anchor="mm")
        return img
    small = font(10, "Condensed Medium")
    for y, (name, used, left) in zip(rows, windows):
        d.text((4, y + 13), name, font=font(11), fill=(255, 255, 255), anchor="ls")
        x = 4 + font(11).getlength(name) + 3
        # Time until the window resets, in whole hours once minutes stop mattering.
        reset = f"{int(left // 3600)}h" if 10 * 3600 <= left < 2 * 86400 else fmt_duration(left)
        d.text((x, y + 13), reset, font=small, fill=dim, anchor="ls")
        d.text((SIZE - 4, y + 13), f"{round(used * 100)}%", font=font(15), fill=(255, 255, 255), anchor="rs")
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
# transcript's tail when that grew, then
# waits for a line on its input; so it ends when the daemon does.
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
                start = rest.rsplit(")", 1)[-1].split()[19:20]
                if session.get("kind", "interactive") == "interactive" and session.get("sessionId") \
                        and [str(session.get("procStart"))] == start:
                    session.update(hostPid=None, sandbox=(kind, sandbox),
                                   key=f"{kind.name}:{sandbox}:{session.get('pid')}:{start[0]}")
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
            args = open(f"/proc/{entry}/cmdline").read().split("\0")
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


def terminal_client(program, times, likely=lambda pid, args, start: True):
    """Pid of the `program` process in a terminal through which the user works in a session.

    `times` are the terminal_times() of the session's own terminal. Without a
    process whose terminal has these, it is the latest one that is `likely`."""
    exact, exact_off, guess, guess_start = None, TERMINAL_SLACK, None, -1
    for pid, args in processes(program):
        try:
            fields = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()
            parent, tty, start = int(fields[1]), fields[4], int(fields[19])
        except (OSError, IndexError, ValueError):
            continue
        if tty == "0" or parent == os.getpid():  # not in a terminal, or a probe of ours
            continue
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
        try:
            pid = int(open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError):
            break
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
    if session["hostPid"] != session.get("pid"):  # containerised
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
_learned = None  # "hostPid:procStart" -> {"backend": name, "id": window id}


def process_key(session):
    return session.get("key") or f"{session['hostPid']}:{proc_start(session['hostPid'])}"


def learned_windows():
    global _learned
    if _learned is None:
        try:
            _learned = json.load(open(WINDOWS_FILE))
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
    if learned.get(process_key(session)) == entry:
        return
    learned[process_key(session)] = entry
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
    learned = learned_windows().get(process_key(session))
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

def key_info(session, state, since):
    meta = transcript_info(session)
    if state == "busy":
        activity = meta.get("tool") or "thinking"
    elif state == "waiting":
        activity = session.get("waitingFor") or ""
    elif state == "unknown":
        activity = "no status yet"
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
        "elapsed": fmt_duration(time.time() - since) if state != "unknown" else "?",
    }


def run(deck):
    slots = [None] * KEYS   # session shown on each key
    last_status = {}        # session -> status last seen
    since = {}              # session -> epoch seconds the status was entered
    unseen = set()          # sessions that finished and were not visited yet
    infos = [None] * KEYS
    next_frame = next_scan = 0.0
    sessions = {}
    while True:
        for key in deck.poll_presses(max(0.0, next_frame - time.monotonic())):
            sid = slots[key]
            if sid in sessions:
                unseen.discard(sid)
                focus(sessions[sid])
                next_scan = 0.0
        now = time.monotonic()
        if now < next_frame:
            continue

        if now >= next_scan:
            next_scan = now + 0.25  # often, to catch records before they are overwritten
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
                if status != last_status.get(sid):
                    previous = last_status.get(sid)
                    if status == "idle" and previous == "busy":
                        unseen.add(sid)
                    # A session that just started or got a prompt has the focus now.
                    just_started = previous is None and \
                        time.time() - (session.get("startedAt") or 0) / 1000 < 15
                    if just_started or (status == "busy" and previous in ("idle", "waiting")):
                        learn_window(session, {process_key(s) for s in sessions.values()})
                    stamp = session.get("statusUpdatedAt") or session.get("startedAt")
                    since[sid] = stamp / 1000 if stamp else time.time()
                    last_status[sid] = status
                if status != "idle":
                    unseen.discard(sid)
                infos[key] = key_info(session, "done" if sid in unseen else status, since[sid])
                infos[key]["offset"] = key * 7.3  # so keys don't show the mascot in sync

        animated = False
        for key, info in enumerate(infos):
            if key == USAGE_KEY:
                deck.set_image(key, render_usage())
            elif info is None:
                deck.set_image(key, BLANK)
            else:
                animated |= info["state"] in ANIMATED
                deck.set_image(key, render(info, now))
        next_frame = now + (1 / FPS if animated else 0.5)


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
