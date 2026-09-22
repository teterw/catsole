"""Newline-delimited JSON framing for the catsole serial link.

The OLED's fonts carry Latin-1 at best, and the microcontroller has neither
the RAM nor the glyph data to fold text itself, so every string bound for
the device is reduced to ASCII here.

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


def _fold_values(obj):
    """Recursively fold every string in a frame, leaving other types alone."""
    if isinstance(obj, str):
        return fold_ascii(obj)
    if isinstance(obj, dict):
        return {key: _fold_values(value) for key, value in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_fold_values(value) for value in obj]
    return obj


def encode_frame(obj: dict) -> bytes:
    """Serialise a frame to a single ASCII line terminated with a newline."""
    payload = json.dumps(_fold_values(obj), separators=(",", ":"))
    line = payload.encode("ascii", "ignore")
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
