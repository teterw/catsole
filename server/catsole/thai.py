"""Thai word boundaries, marked for the display's line wrapping.

Thai leaves no spaces between words, so a line wider than the panel has no
obvious place to break. Guessing at syllable edges on the board tore a word
in a third of real lyric lines. The board has no room for a dictionary; the
PC does. Each boundary between two words is marked with a zero-width space,
which the firmware treats as a place a row may end and draws as nothing.

PyThaiNLP is optional. Without it Thai passes through unmarked and the
firmware falls back to guessing.
"""

from __future__ import annotations

import functools
import logging

from .protocol import WORD_BREAK, has_thai

log = logging.getLogger(__name__)

ZWSP = WORD_BREAK

try:
    from pythainlp.tokenize import word_tokenize
except ImportError:  # optional: see the module docstring
    word_tokenize = None

# Characters that belong to whatever precedes them: the repetition and
# abbreviation marks, the vowels written after their letter, and every mark
# that sits above or below one. A row must never start with one.
_NO_LINE_START = frozenset(
    "ๆฯะาำๅ"
    + "".join(chr(c) for c in (0x0E31, *range(0x0E34, 0x0E3B), *range(0x0E47, 0x0E4F)))
)


@functools.lru_cache(maxsize=256)
def mark_word_breaks(text: str) -> str:
    """Put a zero-width space at each Thai word boundary in text.

    Only between two adjacent runs of text with Thai on at least one side:
    a space is already a break, and Latin words already have spaces.
    Cached, since the same line goes out many times a second.
    """
    if word_tokenize is None or not has_thai(text):
        return text
    try:
        tokens = word_tokenize(text, engine="newmm", keep_whitespace=True)
    except Exception:  # a segmenter failure must not cost the line
        log.exception("Thai word segmentation failed")
        return text
    out = tokens[:1]
    for before, token in zip(tokens, tokens[1:]):
        if (
            token
            and not before.isspace()
            and not token.isspace()
            and token[0] not in _NO_LINE_START
            and (has_thai(before) or has_thai(token))
        ):
            out.append(ZWSP)
        out.append(token)
    return "".join(out)


def warm_up() -> None:
    """Load the dictionary now, so the first Thai line does not stall a frame."""
    mark_word_breaks("ทดสอบ")
