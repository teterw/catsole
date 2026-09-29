"""Newline-delimited JSON framing for the catsole serial link.

The OLED's fonts carry Latin-1 at best, and the microcontroller has neither
the RAM nor the glyph data to fold text itself, so every string bound for
the device is reduced to ASCII here. The one exception is Thai in the main
lyric line: the firmware carries a Thai face for that band, so Thai passes
through as UTF-8 while everything else is still folded.

Both directions drop malformed lines rather than trying to resynchronise.
A dropped frame is invisible at 4Hz; a desynchronised parser is not.
"""

from __future__ import annotations

import json
import unicodedata

# Characters NFKD decomposition leaves intact but the display cannot draw.
_PUNCTUATION = {
    "‘": "'", "’": "'", "‚": "'", "‛": "'",
    "“": '"', "”": '"', "„": '"', "‟": '"',
    "«": '"', "»": '"', "‹": "'", "›": "'",
    "–": "-", "—": "-", "―": "-", "−": "-",
    "…": "...", " ": " ", "​": "", "‌": "",
    "•": "*", "·": "*", "×": "x", "÷": "/",
    "′": "'", "″": '"', "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE", "ß": "ss", "ø": "o",
    "Ø": "O", "đ": "d", "Đ": "D", "þ": "th",
    "™": "(TM)", "©": "(C)", "®": "(R)",
}

_TRANSLATION = str.maketrans(_PUNCTUATION)

MAX_LINE_BYTES = 1024

# Fields the firmware draws with a Thai-capable face. The meta strip is too
# short for one -- Thai marks stack above and below the letters -- so only
# the main band gets it.
THAI_FIELDS = frozenset({"main"})

# The Thai block. Thai vowels and tone marks are combining characters, so
# they must survive NFKD and the mark stripping that follows it.
_THAI_FIRST = 0x0E01
_THAI_LAST = 0x0E5B

# A zero-width space, marking where one Thai word ends and the next begins.
# The firmware breaks rows there and draws nothing for it. See thai.py.
WORD_BREAK = "\u200b"


def is_thai(char: str) -> bool:
    return _THAI_FIRST <= ord(char) <= _THAI_LAST


def has_thai(text: str) -> bool:
    return any(is_thai(char) for char in text)


def fold_ascii(text: str) -> str:
    """Reduce arbitrary text to printable ASCII the OLED fonts can render.

    Substitutes punctuation the decomposition would otherwise drop, strips
    combining marks, discards anything still unmappable, then collapses the
    whitespace that discarding leaves behind.
    """
    if not text:
        return ""
    substituted = text.translate(_TRANSLATION)
    decomposed = unicodedata.normalize("NFKD", substituted)
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    return " ".join(ascii_only.split())


def fold_text(text: str) -> str:
    """Like fold_ascii, but Thai passes through untouched.

    Thai is never decomposed: NFKD would split sara am into nikhahit and
    sara aa, which the device font draws as two separate cells. The word
    breaks marked between Thai words survive too. Runs of everything else
    are folded exactly as fold_ascii would fold them.
    """
    if not text:
        return ""
    if not has_thai(text):
        return fold_ascii(text)

    out: list[str] = []
    run: list[str] = []

    def flush() -> None:
        if run:
            chunk = "".join(run).translate(_TRANSLATION)
            chunk = unicodedata.normalize("NFKD", chunk)
            out.append(chunk.encode("ascii", "ignore").decode("ascii"))
            run.clear()

    for char in text:
        if is_thai(char) or char == WORD_BREAK:
            flush()
            out.append(char)
        else:
            run.append(char)
    flush()
    return " ".join("".join(out).split())


def _fold_values(obj, key: str | None = None):
    """Recursively fold every string in a frame, leaving other types alone."""
    if isinstance(obj, str):
        return fold_text(obj) if key in THAI_FIELDS else fold_ascii(obj)
    if isinstance(obj, dict):
        return {k: _fold_values(value, k) for k, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_fold_values(value) for value in obj]
    return obj


def encode_frame(obj: dict) -> bytes:
    """Serialise a frame to a single line terminated with a newline.

    Everything is ASCII except Thai in THAI_FIELDS, which goes as raw UTF-8
    rather than as \\u escapes: an escape is six bytes per character on
    the wire against three, and the device's receive buffer is small.
    """
    payload = json.dumps(
        _fold_values(obj), separators=(",", ":"), ensure_ascii=False
    )
    line = payload.encode("utf-8")
    if len(line) > MAX_LINE_BYTES:
        line = line[:MAX_LINE_BYTES]
    return line + b"\n"


def decode_line(line: str) -> dict | None:
    """Parse one inbound line, returning None for anything unusable."""
    if not line:
        return None
    line = line.strip()
    if not line:
        return None
    try:
        parsed = json.loads(line)
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None
