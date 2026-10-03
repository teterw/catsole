"""The top strip's title, drawn by the PC when the board has no font for it.

The strip is 7px tall, and the only Thai faces the board carries are 14 and
16px, so a Thai title used to vanish from it. The PC has real Thai fonts:
it renders the line here as an 11-row, one-bit bitmap and the board scrolls
that instead of text. Tahoma at 11px was the smallest that stayed readable
on the panel; at 10px the bottoms of letters like ว and ย are cut off and
read as other letters.

Pillow and a Thai-capable font are both optional. Without them the strip
falls back to the ASCII it always showed.
"""

from __future__ import annotations

import functools
import logging
import os
import shutil
import subprocess
import sys

from .protocol import WORD_BREAK, has_thai

log = logging.getLogger(__name__)

try:
    from PIL import Image, ImageDraw, ImageFont
except ImportError:  # optional: see the module docstring
    Image = None

HEIGHT = 11
# Wider titles are cut here; the board keeps the bitmap in a fixed buffer.
MAX_WIDTH = 480
# The tallest stacks of marks over and under Thai letters, plus Latin
# descenders, so every title is sized and placed the same way. Accented
# capitals are left out on purpose: their accents would shrink everything
# to size 8, which is unreadable, for the sake of a rare top pixel.
_PROBE = "ที่ปู่ญู gjy"

_WINDOWS_FONTS = ("tahoma.ttf", "LeelawUI.ttf", "leelawad.ttf")
_LINUX_FONTS = (
    "/usr/share/fonts/truetype/tlwg/Loma.ttf",
    "/usr/share/fonts/truetype/tlwg/Garuda.ttf",
    "/usr/share/fonts/truetype/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/noto/NotoSansThai-Regular.ttf",
    "/usr/share/fonts/google-noto/NotoSansThai-Regular.ttf",
)


def needs_bitmap(text: str) -> bool:
    """Whether the board's own fonts would lose part of this line."""
    return bool(text) and has_thai(text)


@functools.lru_cache(maxsize=1)
def find_font() -> str | None:
    """A font file that covers Thai, or None."""
    if Image is None:
        return None
    if sys.platform == "win32":
        windir = os.environ.get("WINDIR", r"C:\Windows")
        for name in _WINDOWS_FONTS:
            path = os.path.join(windir, "Fonts", name)
            if os.path.exists(path):
                return path
        return None
    for path in _LINUX_FONTS:
        if os.path.exists(path):
            return path
    # fc-list, not fc-match: fc-match always answers with something, even a
    # font with no Thai in it, which would draw empty boxes.
    if shutil.which("fc-list"):
        try:
            out = subprocess.run(
                ["fc-list", ":lang=th", "file"],
                capture_output=True, text=True, timeout=3,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            out = ""
        for line in sorted(out.splitlines()):
            path = line.strip().rstrip(":")
            if path.lower().endswith((".ttf", ".otf")) and os.path.exists(path):
                return path
    return None


@functools.lru_cache(maxsize=1)
def _font():
    """The font at the largest size whose tallest stack fits HEIGHT rows."""
    path = find_font()
    if path is None:
        return None
    best = None
    for size in range(6, 24):
        font = ImageFont.truetype(path, size)
        left, top, right, bottom = font.getbbox(_PROBE)
        if bottom - top > HEIGHT:
            break
        best = (font, top)
    return best


def pack_rows(rows, width: int) -> bytes:
    """Rows of 0/1 pixels as XBM: each row padded to whole bytes, the
    leftmost pixel in the lowest bit, which is what U8g2's drawXBM reads."""
    out = bytearray()
    for row in rows:
        for start in range(0, width, 8):
            byte = 0
            for bit, lit in enumerate(row[start:start + 8]):
                if lit:
                    byte |= 1 << bit
            out.append(byte)
    return bytes(out)


@functools.lru_cache(maxsize=32)
def render(text: str) -> tuple[int, bytes] | None:
    """(width, XBM bytes) for an 11-row rendering of text, or None."""
    sized = _font()
    if sized is None:
        return None
    font, top = sized
    try:
        width = min(MAX_WIDTH, max(1, int(font.getbbox(text)[2]) + 1))
        image = Image.new("L", (width, HEIGHT), 0)
        x = 0.0
        for run, thai in _script_runs(text):
            # Each script in the way it survives one bit best. FreeType's
            # black-and-white hinting keeps Thai marks crisp but turns a
            # Latin "A" into a "4"; smoothing then cutting at half keeps
            # Latin shapes but blurs the marks together.
            layer = Image.new("L", (width, HEIGHT), 0)
            draw = ImageDraw.Draw(layer)
            if thai:
                draw.fontmode = "1"
            draw.text((x, -top), run, font=font, fill=255)
            if not thai:
                layer = layer.point(lambda v: 255 if v >= 128 else 0)
            image.paste(255, (0, 0), layer)
            x += font.getlength(run)
    except Exception:
        log.exception("could not render the title")
        return None
    pixels = image.load()
    rows = [[1 if pixels[x, y] else 0 for x in range(width)] for y in range(HEIGHT)]
    return width, pack_rows(rows, width)


def _script_runs(text: str):
    """Split text into (run, is_thai) pieces, spaces going with what follows."""
    runs = []
    for char in text:
        thai = has_thai(char) or char == WORD_BREAK
        if char.isspace() and runs:
            runs[-1][0] += char
        elif runs and runs[-1][1] == thai:
            runs[-1][0] += char
        else:
            runs.append([char, thai])
    return [(run, thai) for run, thai in runs]
