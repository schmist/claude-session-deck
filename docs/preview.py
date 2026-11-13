#!/usr/bin/env python3
"""Draw docs/preview.png: a Stream Deck MK.2 with example sessions, keys drawn by the daemon's code.

One image pixel is one pixel of a key, so the keys are shown exactly as the
daemon sends them: 72 x 72 px each. The deck around them is to the same scale.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import claude_session_deck as csd  # noqa: E402

# The deck is 118 x 84 mm. Its keys are windows onto one screen with about 5 px
# per mm, in which 36 px lie hidden between two keys.
PX_PER_MM = 5.05
BODY = (round(118 * PX_PER_MM), round(84 * PX_PER_MM))
GAP = 36
COLS, ROWS = 5, 3
SS = csd.SS       # the deck is drawn supersampled like the keys, which are pasted unscaled
KEY_RADIUS = 9    # corners of a key's window
BODY_RADIUS = 40
MARGIN = 34       # around the deck, for its shadow
T = 3.0           # moment shown: names are up, and one creature is on stage

# name, state, line under the name, tokens, time in the state, mascot offset
SESSIONS = [
    ("web-shop", "busy", "Bash", 156_000, "2m", 10),
    ("gateway", "waiting", "permission", 56_000, "41s", 0),
    ("docs", "done", "", 88_000, "12s", 0),
    ("infra-live", "idle", "", 720_000, "3h05", 0),
    ("mobile", "busy", "", 231_000, "6m", 0.6),
    ("billing", "busy", "Edit", 412_000, "58s", 12),
    ("pipeline", "busy", "thinking", 874_000, "14m", 15),
    ("auth", "idle", "", 31_000, "26m", 0),
]
USAGE = [("5h", 0.42, 2 * 3600 + 13 * 60), ("week", 0.18, 4 * 86400)]


def key_images():
    csd.account_usage = lambda: USAGE
    images = [csd.BLANK] * csd.KEYS
    for key, (name, state, activity, tokens, elapsed, offset) in enumerate(SESSIONS):
        images[key] = csd.render({
            "name": name, "state": state, "activity": activity, "elapsed": elapsed,
            "tokens": csd.fmt_tokens(tokens), "context": tokens / csd.CONTEXT_WINDOW,
            "model": "", "offset": offset}, T)
    images[csd.USAGE_KEY] = csd.render_usage()
    return images


def rounded(size, radius, fill):
    """Antialiased mask of a rounded rectangle of `size`."""
    mask = Image.new("L", (size[0] * SS, size[1] * SS), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, mask.width - 1, mask.height - 1), radius * SS, fill=fill)
    return mask.resize(size, Image.LANCZOS)


def main():
    size = (BODY[0] + 2 * MARGIN, BODY[1] + 2 * MARGIN)
    out = Image.new("RGBA", size, (0, 0, 0, 0))

    shadow = Image.new("L", size, 0)
    shadow.paste(rounded(BODY, BODY_RADIUS, 150), (MARGIN, MARGIN + 8))
    out.putalpha(shadow.filter(ImageFilter.GaussianBlur(12)))

    # Body with a bevelled edge, and the faceplate inside it.
    big = Image.new("RGBA", (size[0] * SS, size[1] * SS), (0, 0, 0, 0))
    g = ImageDraw.Draw(big)

    def box(x, y, w, h, radius, **style):
        g.rounded_rectangle(((MARGIN + x) * SS, (MARGIN + y) * SS, (MARGIN + x + w) * SS - 1,
                             (MARGIN + y + h) * SS - 1), radius * SS, **style)

    box(0, 0, *BODY, BODY_RADIUS, fill=(34, 36, 41))
    box(1.5, 1.5, BODY[0] - 3, BODY[1] - 3, BODY_RADIUS - 1.5, fill=(20, 21, 25))
    box(13, 13, BODY[0] - 26, BODY[1] - 26, BODY_RADIUS - 13, fill=(11, 12, 14))
    pitch = csd.SIZE + GAP
    left = (BODY[0] - COLS * csd.SIZE - (COLS - 1) * GAP) // 2
    top = (BODY[1] - ROWS * csd.SIZE - (ROWS - 1) * GAP) // 2
    spots = [(left + key % COLS * pitch, top + key // COLS * pitch) for key in range(csd.KEYS)]
    for x, y in spots:  # the rim of each keycap
        box(x - 4, y - 4, csd.SIZE + 8, csd.SIZE + 8, KEY_RADIUS + 4, fill=(3, 3, 4), outline=(40, 42, 48), width=SS // 2)
    out.alpha_composite(big.resize(size, Image.LANCZOS))

    window = rounded((csd.SIZE, csd.SIZE), KEY_RADIUS, 255)
    for (x, y), image in zip(spots, key_images()):
        out.paste(image, (MARGIN + x, MARGIN + y), window)

    out.save(os.path.join(os.path.dirname(__file__), "preview.png"), optimize=True)


if __name__ == "__main__":
    main()
