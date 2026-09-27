"""Tests for serial framing and ASCII folding."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.protocol import decode_line, encode_frame, fold_ascii, fold_text


def test_fold_ascii_strips_accents():
    assert fold_ascii("Beyoncé") == "Beyonce"
    assert fold_ascii("Sigur Rós") == "Sigur Ros"
    assert fold_ascii("Björk") == "Bjork"


def test_fold_ascii_normalises_punctuation():
    assert fold_ascii("don’t — stop") == "don't - stop"
    assert fold_ascii("“quoted”") == '"quoted"'
    assert fold_ascii("wait…") == "wait..."


def test_fold_ascii_drops_unmappable_characters():
    assert fold_ascii("hello 你好") == "hello"
    assert fold_ascii("ЖЖЖ") == ""


def test_fold_ascii_collapses_whitespace_left_behind():
    # Dropping the CJK run must not leave a double space in the middle.
    assert fold_ascii("one 你 two") == "one two"


def test_fold_ascii_handles_empty():
    assert fold_ascii("") == ""


def test_fold_text_keeps_thai_intact():
    # Sara am must not be decomposed into nikhahit plus sara aa.
    assert fold_text("น้ำตา") == "น้ำตา"
    assert fold_text("ที่รัก") == "ที่รัก"


def test_fold_text_folds_everything_around_thai():
    assert fold_text("Café ที่รัก — 你好") == "Cafe ที่รัก -"


def test_fold_text_matches_fold_ascii_without_thai():
    for text in ("Beyoncé", "don’t — stop", "hello 你好", ""):
        assert fold_text(text) == fold_ascii(text)


def test_encode_frame_sends_thai_main_as_utf8():
    out = encode_frame({"t": "frame", "main": "รักเธอ"})
    assert "รักเธอ".encode("utf-8") in out
    assert decode_line(out.decode("utf-8"))["main"] == "รักเธอ"


def test_encode_frame_keeps_thai_out_of_other_fields():
    # The meta strip has no Thai face, so Thai there is dropped as before.
    out = encode_frame({"t": "frame", "meta": "Artist - รักเธอ"})
    assert out.decode("ascii")  # still pure ASCII
    assert decode_line(out.decode())["meta"] == "Artist -"


def test_encode_frame_folds_and_terminates():
    out = encode_frame({"t": "frame", "meta": "Sigur Rós"})
    assert out.endswith(b"\n")
    assert b"Sigur Ros" in out


def test_encode_frame_folds_nested_values():
    out = encode_frame({"t": "frame", "cpu": {"label": "Café"}, "tags": ["naïve"]})
    assert b"Cafe" in out
    assert b"naive" in out


def test_encode_frame_leaves_numbers_alone():
    frame = decode_line(encode_frame({"t": "frame", "hold_ms": 3200, "eq": 1}).decode())
    assert frame["hold_ms"] == 3200
    assert frame["eq"] == 1


def test_encode_frame_is_single_line():
    out = encode_frame({"t": "frame", "main": "line one\nline two"})
    assert out.count(b"\n") == 1


def test_decode_line_returns_none_on_garbage():
    assert decode_line("{not json") is None
    assert decode_line("") is None
    assert decode_line("   ") is None


def test_decode_line_rejects_non_objects():
    assert decode_line("[1, 2, 3]") is None
    assert decode_line("42") is None


def test_decode_roundtrip():
    assert decode_line(encode_frame({"t": "tap"}).decode()) == {"t": "tap"}
