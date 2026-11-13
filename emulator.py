#!/usr/bin/env python3
"""Stand in for Stream Decks that are not plugged in, and show them in a browser.

Each emulated deck is a socket that claude_session_deck.py finds like a real
deck. It is sent the reports the real model is sent and decodes them as that
model would, so what the page shows is what the daemon's own code for that
model put on the wire. A click on a key is reported to the daemon as a press,
and so is what is done to a model's dials, touch screen or touch keys.
"""
import argparse
import base64
import io
import json
import os
import queue
import select
import signal
import socket
import struct
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from PIL import Image

import claude_session_deck as csd

PORT = 8317
UNDO = {  # the turn that takes back each turn
    Image.Transpose.ROTATE_90: Image.Transpose.ROTATE_270,
    Image.Transpose.ROTATE_270: Image.Transpose.ROTATE_90,
}


class ProtocolError(Exception):
    """The daemon sent something the real deck would not understand."""


class Firmware:
    """What a deck makes of the reports it gets, and the reports it sends.

    Written from the protocol, not from the daemon's code, so the two can
    disagree. This one is that of the Original V2 and the models after it."""

    dials = pads = 0  # how many dials and touch keys the deck has

    def brightness(self, report):
        """Percent that a feature report sets the brightness to, None if it is another report."""
        return report[2] if report[:2] == b"\x03\x08" else None

    def image_part(self, report):
        """(where it goes, page, whether it is the last, data) of an output report with part of an image.

        Where it goes is a key's number, or the box on the screen as (left, top, right, bottom)."""
        if len(report) != 1024 or report[:2] != b"\x02\x07":
            raise ProtocolError(f"not an image report: {report[:8].hex(' ')}, {len(report)} bytes")
        return (report[2], *self.part(report))

    def part(self, report):
        """(page, whether it is the last, data) of a report with the header that key images have."""
        length = report[4] | report[5] << 8
        if length > 1016:
            raise ProtocolError(f"{length} bytes do not fit in one report")
        return report[6] | report[7] << 8, bool(report[3]), report[8:8 + length]

    def report(self, control, states, changed=None):
        """The input report after a change to `control`: "key", "push", "turn", "touch" or "pad".

        `states` holds the states of all keys, dials and touch keys by control;
        `changed` is the steps each dial was turned by, or where the screen was touched."""
        return bytes([0x01, 0x00, len(states["key"]), 0x00]) + bytes(states["key"])


class PlusFirmware(Firmware):
    dials = 4

    def image_part(self, report):
        if len(report) != 1024 or report[:2] != b"\x02\x0c":
            return super().image_part(report)
        x, y, w, h, last, page, length = struct.unpack_from("<HHHHBHH", report, 2)
        if length > 1008:
            raise ProtocolError(f"{length} bytes do not fit in one report")
        return (x, y, x + w, y + h), page, bool(last), report[16:16 + length]

    def report(self, control, states, changed=None):
        if control == "push":
            return bytes([0x01, 0x03, 0x05, 0x00, 0x00]) + bytes(states["push"])
        if control == "turn":
            return bytes([0x01, 0x03, 0x05, 0x00, 0x01]) + bytes(step & 0xFF for step in changed)
        if control == "touch":
            return struct.pack("<BBHBBHH", 0x01, 0x02, 0x0E, 0x01, 0x00, *changed).ljust(14, b"\0")
        return super().report(control, states)


class NeoFirmware(Firmware):
    pads = 2

    def image_part(self, report):
        if len(report) != 1024 or report[:2] != b"\x02\x0b":
            return super().image_part(report)
        return (None, *self.part(report))  # the whole screen

    def report(self, control, states, changed=None):
        both = states["key"] + states["pad"]
        return bytes([0x01, 0x00, len(both), 0x00]) + bytes(both)


class MiniFirmware(Firmware):
    def brightness(self, report):
        return report[5] if report[:5] == b"\x05\x55\xaa\xd1\x01" else None

    def image_part(self, report):
        if len(report) != 1024 or report[:2] != b"\x02\x01":
            raise ProtocolError(f"not an image report: {report[:8].hex(' ')}, {len(report)} bytes")
        # It does not tell its length: the image's own header does.
        return report[5] - 1, report[2], bool(report[4]), report[16:]

    def report(self, control, states, changed=None):
        return b"\x01" + bytes(states["key"])


FIRMWARE = {"plus": PlusFirmware, "neo": NeoFirmware, "mini": MiniFirmware}


def data_url(image):
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


class Device:
    """One emulated deck."""

    def __init__(self, number, model, publish):
        self.number, self.model, self.publish = number, model, publish
        self.firmware = FIRMWARE.get(model.id, Firmware)()
        self.product = min(model.products)
        self.path = os.path.join(csd.RUNTIME_DIR, f"deck-{os.getpid()}-{number}.sock")
        self.images = {}      # key -> its image as a data: URL
        self.screen = model.screen and Image.new("RGB", model.screen, (0, 0, 0))
        self.parts = {}       # where an image goes -> the parts of it that have arrived
        self.states = {"key": [0] * model.keys, "push": [0] * self.firmware.dials,
                       "pad": [0] * self.firmware.pads}
        self.brightness = None
        self.host = None      # the connection of the daemon that uses this deck
        self.error = None     # the last ProtocolError
        self.lock = threading.Lock()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_SEQPACKET)
        self.listener.bind(self.path)
        self.listener.listen()
        threading.Thread(target=self.serve, daemon=True).start()

    def describe(self):
        m = self.model
        return {"type": "deck", "deck": self.number, "name": m.name, "cols": m.cols, "rows": m.rows,
                "size": m.size, "screen": m.screen, "dials": self.firmware.dials, "pads": self.firmware.pads,
                "connected": self.host is not None, "brightness": self.brightness, "error": self.error}

    def serve(self):
        while True:
            for ready in select.select([s for s in (self.listener, self.host) if s], [], [])[0]:
                if ready is self.listener:
                    conn = self.listener.accept()[0]
                    if self.host:  # like a real deck, it serves one program
                        conn.close()
                        continue
                    conn.send(f"{csd.ELGATO_VENDOR:04x}:{self.product:04x}".encode())
                    with self.lock:
                        self.host, self.parts, self.error = conn, {}, None
                    self.publish(self.describe())
                    continue
                try:
                    message = ready.recv(4096)
                except OSError:
                    message = b""
                if not message:
                    ready.close()
                    with self.lock:
                        self.host = None
                    self.publish(self.describe())
                    continue
                try:
                    self.receive(message[:1], message[1:])
                except ProtocolError as e:
                    print(f"{self.model.name}: {e}", file=sys.stderr, flush=True)
                    self.error = str(e)
                    self.publish(self.describe())

    def receive(self, kind, report):
        if kind == b"F":
            percent = self.firmware.brightness(report)
            if percent is not None:
                self.brightness = percent
                self.publish(self.describe())
            return
        if kind != b"O":
            raise ProtocolError(f"neither an output nor a feature report: {kind!r}")
        where, page, last, data = self.firmware.image_part(report)
        if isinstance(where, int):
            if not 0 <= where < self.model.keys:
                raise ProtocolError(f"there is no key {where}")
            what, size, turns = f"key {where}", (self.model.size,) * 2, self.model.turns
        else:
            w, h = self.model.screen
            where = where or (0, 0, w, h)
            if not (0 <= where[0] < where[2] <= w and 0 <= where[1] < where[3] <= h):
                raise ProtocolError(f"the screen has no part {where}")
            what, size, turns = "screen", (where[2] - where[0], where[3] - where[1]), self.model.screen_turns
        parts = self.parts.setdefault(where, [])
        if page != len(parts):
            del self.parts[where]
            raise ProtocolError(f"{what}: part {page} came when part {len(parts)} was due")
        parts.append(data)
        if not last:
            return
        del self.parts[where]
        image = self.decode(what, b"".join(parts), "JPEG" if what == "screen" else self.model.image_format,
                            size, turns)
        with self.lock:
            if what == "screen":
                self.screen.paste(image, where[:2])
                event = self.screen_event()
            else:
                self.images[where] = data_url(image)
                event = {"type": "key", "deck": self.number, "key": where, "image": self.images[where]}
        self.publish(event)

    def screen_event(self):
        return {"type": "screen", "deck": self.number, "image": data_url(self.screen)}

    def decode(self, what, data, image_format, size, turns):
        """The image a deck of this model shows for the data it was sent."""
        try:
            image = Image.open(io.BytesIO(data))
            image.load()
        except Exception as e:
            raise ProtocolError(f"{what}: not an image: {e}") from None
        if image.format != image_format or image.size != size:
            raise ProtocolError(f"{what}: got {image.format} of {image.size[0]} x {image.size[1]}, "
                                f"this model takes {image_format} of {size[0]} x {size[1]}")
        for turn in reversed(turns):
            image = image.transpose(UNDO.get(turn, turn))
        return image.convert("RGB")

    def input(self, control, a, b):
        """Something was done on the page: key, dial ("push") or touch key ("pad") `a` went down
        or up as `b` says, dial `a` was turned by `b` steps, or the screen was touched at (a, b)."""
        with self.lock:
            changed = None
            if control in ("key", "push", "pad"):
                states = self.states[control]
                if not 0 <= a < len(states) or states[a] == b:
                    return
                states[a] = int(b)
            elif control == "turn" and 0 <= a < self.firmware.dials and b:
                changed = [max(-127, min(127, b)) if dial == a else 0 for dial in range(self.firmware.dials)]
            elif control == "touch" and self.firmware.dials and self.screen:
                changed = (max(0, min(self.screen.width - 1, a)), max(0, min(self.screen.height - 1, b)))
            else:
                return
            if self.host:
                try:
                    self.host.send(self.firmware.report(control, self.states, changed))
                except OSError:
                    pass

    def snapshot(self):
        """The events that bring a page up to date."""
        with self.lock:
            return [self.describe()] + ([self.screen_event()] if self.screen else []) + [
                {"type": "key", "deck": self.number, "key": key, "image": url}
                for key, url in self.images.items()]

    def close(self):
        self.listener.close()
        try:
            os.unlink(self.path)
        except OSError:
            pass


class Hub:
    """Passes what the decks show on to the pages that are open."""

    def __init__(self):
        self.devices = []
        self.pages = set()  # a queue of events for each
        self.lock = threading.Lock()

    def publish(self, event):
        with self.lock:
            for page in self.pages:
                page.put(event)

    def join(self):
        page = queue.Queue()
        with self.lock:
            for device in self.devices:
                for event in device.snapshot():
                    page.put(event)
            self.pages.add(page)
        return page

    def leave(self, page):
        with self.lock:
            self.pages.discard(page)


PAGE = r"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Stream Deck emulator</title>
<style>
  :root { color-scheme: dark; }
  body { margin: 0; padding: 32px 16px 48px; background: #17181c; color: #c9ccd3;
         font: 14px/1.4 system-ui, sans-serif; }
  header { max-width: 1100px; margin: 0 auto 28px; display: flex; flex-wrap: wrap; gap: 8px 24px;
           align-items: baseline; }
  h1 { font-size: 18px; font-weight: 600; margin: 0; color: #f1f2f4; }
  header label { margin-left: auto; color: #8d919b; }
  select { font: inherit; color: inherit; background: #23252b; border: 1px solid #3a3d46;
           border-radius: 6px; padding: 3px 6px; }
  main { max-width: 1100px; margin: 0 auto; display: flex; flex-wrap: wrap; gap: 36px 32px;
         align-items: flex-start; }
  figure { margin: 0; max-width: 100%; }
  figcaption { margin-bottom: 10px; }
  figcaption b { color: #f1f2f4; font-weight: 600; margin-right: 8px; }
  figcaption span { color: #8d919b; }
  .state { display: block; font-size: 13px; }
  .state.on::before, .state.off::before { content: ""; display: inline-block; width: 8px; height: 8px;
           border-radius: 50%; margin-right: 6px; background: #5f636d; }
  .state.on::before { background: #3cdc78; }
  .state.bad { color: #ff7a7a; }
  .scroll { overflow-x: auto; }
  .body { display: flex; flex-direction: column; align-items: center; width: max-content;
          background: #0b0c0e; border: 2px solid #23252a; box-shadow: 0 10px 28px #0008; }
  .keys { display: grid; }
  .row { display: flex; align-items: center; justify-content: space-around; align-self: stretch; }
  .key, .dial, .pad, .screen { background: #000; border: 0; padding: 0; outline: 4px solid #030304;
         cursor: pointer; overflow: hidden; touch-action: manipulation; }
  .key img, .screen img { display: block; width: 100%; height: 100%; }
  .screen { cursor: crosshair; }
  .dial { border-radius: 50%; background: radial-gradient(circle at 50% 35%, #3a3d45, #17181c 70%);
          outline-color: #23252a; position: relative; }
  .dial::after { content: ""; position: absolute; left: calc(50% - 1.5px); top: 10%; width: 3px;
          height: 22%; border-radius: 2px; background: #c9ccd3; }
  .pad { border-radius: 50%; background: #1d1f24; outline-color: #23252a; }
  .key:active, .key.down, .pad.down, .dial.down { filter: brightness(.75); transform: scale(.97); }
  :is(.key, .dial, .pad):focus-visible { outline: 2px solid #7aa7ff; }
  .hint { color: #8d919b; font-size: 13px; margin: 8px 0 0; }
  p.empty { color: #8d919b; }
</style>
<header>
  <h1>Stream Deck emulator</h1>
  <span id="link">connecting…</span>
  <label>Zoom <select id="zoom"><option value="1">1 : 1</option><option value="1.5">1.5 ×</option>
    <option value="2">2 ×</option></select></label>
</header>
<main></main>
<script>
const decks = new Map();
const main = document.querySelector("main"), zoomBox = document.getElementById("zoom");
let zoom = 1;
try { zoom = +localStorage.getItem("zoom") || 1; } catch (e) {}
zoomBox.value = String(zoom);
zoomBox.onchange = () => {
  zoom = +zoomBox.value;
  try { localStorage.setItem("zoom", zoom); } catch (e) {}
  decks.forEach(layout);
};

function layout(d) {
  // To scale: between two keys lies half a key's width of the deck's screen.
  const size = d.info.size * zoom, gap = size / 2;
  const px = v => v + "px";
  Object.assign(d.body.style, {gap: px(gap), padding: px(gap), borderRadius: px(gap)});
  Object.assign(d.grid.style, {gridTemplateColumns: `repeat(${d.info.cols}, ${px(size)})`, gap: px(gap)});
  for (const key of d.keys)
    Object.assign(key.style, {width: px(size), height: px(size), borderRadius: px(size / 8)});
  for (const knob of d.round)
    Object.assign(knob.style, {width: px(knob.dataset.size * zoom), height: px(knob.dataset.size * zoom)});
  if (d.screen)
    Object.assign(d.screen.style, {width: px(d.info.screen[0] * zoom), height: px(d.info.screen[1] * zoom),
                                   borderRadius: px(6 * zoom)});
  for (const img of d.body.querySelectorAll("img"))
    img.style.imageRendering = Number.isInteger(zoom) ? "pixelated" : "auto";
}

function send(deck, control, a, b) {
  fetch(`/input/${deck}/${control}/${a}/${b}`, {method: "POST"});
}

// A button that reports when it goes down and up, as key, dial or touch key `n`.
function pressable(d, control, n, className, title) {
  const button = document.createElement("button");
  button.className = className;
  button.title = title;
  let down = false;
  const set = now => {
    if (now === down) return;
    down = now;
    button.classList.toggle("down", down);
    send(d.info.deck, control, n, down ? "down" : "up");
  };
  button.onpointerdown = e => { if (e.button === 0) set(true); };
  button.onpointerup = button.onpointerleave = button.onpointercancel = () => set(false);
  button.onkeydown = e => { if (e.key === " " || e.key === "Enter") set(true); };
  button.onkeyup = button.onblur = () => set(false);
  button.onclick = e => e.preventDefault();
  return button;
}

function addDeck(info) {
  const figure = document.createElement("figure");
  figure.innerHTML = '<figcaption><b></b><span></span><span class="state"></span></figcaption>' +
                     '<div class="scroll"><div class="body"><div class="keys"></div></div></div>';
  const d = {info, figure, body: figure.querySelector(".body"), grid: figure.querySelector(".keys"),
             keys: [], round: [], screen: null};
  for (let k = 0; k < info.cols * info.rows; k++) {
    const key = pressable(d, "key", k, "key", "Key " + k);
    key.appendChild(document.createElement("img")).alt = "";
    d.grid.appendChild(key);
    d.keys.push(key);
  }
  const round = (control, n, className, title, size) => {
    const knob = pressable(d, control, n, className, title);
    knob.dataset.size = size;
    d.round.push(knob);
    return knob;
  };
  if (info.screen) {
    const row = d.body.appendChild(document.createElement("div"));
    row.className = "row";
    d.screen = document.createElement("div");
    d.screen.className = "screen";
    d.screen.appendChild(document.createElement("img")).alt = "";
    if (info.dials) {  // the + reports where its screen is touched
      d.screen.title = "Click to touch";
      d.screen.onclick = e => {
        const box = d.screen.getBoundingClientRect();
        send(info.deck, "touch", Math.floor((e.clientX - box.left) / zoom), Math.floor((e.clientY - box.top) / zoom));
      };
    } else {
      d.screen.style.cursor = "default";
    }
    if (info.pads) row.appendChild(round("pad", 0, "pad", "Touch key 0", 36));
    row.appendChild(d.screen);
    if (info.pads > 1) row.appendChild(round("pad", 1, "pad", "Touch key 1", 36));
  }
  if (info.dials) {
    const row = d.body.appendChild(document.createElement("div"));
    row.className = "row";
    for (let n = 0; n < info.dials; n++) {
      const dial = round("push", n, "dial", `Dial ${n}: click to push, scroll or arrow keys to turn`, 72);
      let angle = 0;
      const turn = steps => {
        angle += steps * 15;
        dial.style.rotate = angle + "deg";
        send(info.deck, "turn", n, steps);
      };
      dial.addEventListener("wheel", e => { e.preventDefault(); turn(e.deltaY < 0 ? 1 : -1); }, {passive: false});
      dial.addEventListener("keydown", e => {
        if (e.key === "ArrowRight" || e.key === "ArrowUp") turn(1);
        if (e.key === "ArrowLeft" || e.key === "ArrowDown") turn(-1);
      });
      row.appendChild(dial);
    }
    const hint = figure.appendChild(document.createElement("p"));
    hint.className = "hint";
    hint.textContent = "Dials: click to push, scroll over one to turn it. Click the screen to touch it.";
  }
  decks.set(info.deck, d);
  main.appendChild(figure);
  return d;
}

function onEvent(event) {
  if (event.type === "deck") {
    const d = decks.get(event.deck) || addDeck(event);
    d.info = event;
    d.figure.querySelector("b").textContent = event.name;
    d.figure.querySelector("span").textContent =
      `${event.cols} × ${event.rows} keys of ${event.size} px` +
      (event.screen ? `, screen of ${event.screen[0]} × ${event.screen[1]} px` : "");
    const state = d.figure.querySelector(".state");
    state.className = "state " + (event.error ? "bad" : event.connected ? "on" : "off");
    state.textContent = event.error ? "Protocol error: " + event.error
      : !event.connected ? "Waiting for the daemon"
      : "In use" + (event.brightness == null ? "" : `, brightness ${event.brightness}%`);
    layout(d);
  } else if (event.type === "key") {
    const d = decks.get(event.deck);
    if (d) d.keys[event.key].firstChild.src = event.image;
  } else if (event.type === "screen") {
    const d = decks.get(event.deck);
    if (d && d.screen) d.screen.firstChild.src = event.image;
  }
}

const link = document.getElementById("link");
const source = new EventSource("/events");
source.onopen = () => { link.textContent = ""; };
source.onerror = () => { link.textContent = "The emulator is not running."; };
source.onmessage = e => onEvent(JSON.parse(e.data));
</script>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hub = None

    def log_message(self, *args):
        pass

    def reply(self, status, body=b"", kind="text/plain"):
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/":
            return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")
        if self.path != "/events":
            return self.reply(404)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        page = self.hub.join()
        try:
            while True:
                try:
                    data = f"data: {json.dumps(page.get(timeout=15))}\n\n"
                except queue.Empty:
                    data = ": still here\n\n"  # so a page that left is noticed
                self.wfile.write(data.encode())
                self.wfile.flush()
        except OSError:
            pass
        finally:
            self.hub.leave(page)

    def do_POST(self):
        # A press makes the daemon raise a window, so another site's page must not send one.
        origin = self.headers.get("Origin")
        if origin and origin.split("//")[-1] != self.headers.get("Host"):
            return self.reply(403)
        parts = self.path.split("/")
        try:
            device, control = self.hub.devices[int(parts[2])], parts[3]
            a = int(parts[4])
            b = {"down": True, "up": False}[parts[5]] if control in ("key", "push", "pad") else int(parts[5])
        except (IndexError, ValueError, KeyError):
            return self.reply(404)
        if parts[1] != "input":
            return self.reply(404)
        device.input(control, a, b)
        self.reply(204)


def main():
    by_id = {model.id: model for model in csd.MODELS}
    parser = argparse.ArgumentParser(
        description="Emulate Stream Decks for claude_session_deck.py and show them in a browser.",
        epilog="models: " + ", ".join(f"{m.id} ({m.name}, {m.cols} x {m.rows} keys of {m.size} px)"
                                      for m in csd.MODELS))
    parser.add_argument("models", nargs="*", metavar="model",
                        help="a deck to emulate; may be given more than once (default: one of each)")
    parser.add_argument("--port", type=int, default=PORT, help=f"port of the page (default: {PORT})")
    args = parser.parse_args()
    for id in args.models:
        if id not in by_id:
            parser.error(f"unknown model {id!r}; there are: {', '.join(by_id)}")

    os.makedirs(csd.RUNTIME_DIR, mode=0o700, exist_ok=True)
    hub = Hub()
    Handler.hub = hub
    try:
        server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    except OSError as e:
        sys.exit(f"cannot serve on port {args.port}: {e}; choose another with --port")
    server.daemon_threads = True
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so the sockets are removed
    try:
        for number, id in enumerate(args.models or by_id):
            hub.devices.append(Device(number, by_id[id], hub.publish))
            print(f"{by_id[id].name}: {hub.devices[-1].path}")
        print(f"Open http://127.0.0.1:{server.server_port}/ to see them. Press Ctrl+C to end.", flush=True)
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for device in hub.devices:
            device.close()


if __name__ == "__main__":
    main()
