"""Tests for reading and writing shortcut combos. Registering them with
Windows is checked by hand: it takes the keys from the whole desktop."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.hotkeys import format_combo, parse_combo


def test_parse_and_format():
    assert parse_combo("Ctrl+F11") == (0x2, 0x7A)
    assert format_combo(0x2, 0x7B) == "ctrl+f12"


def test_round_trip_puts_modifiers_in_order():
    assert format_combo(*parse_combo("shift + ctrl + numpad3")) == "ctrl+shift+numpad3"
    assert format_combo(*parse_combo("win+alt+pageup")) == "alt+win+pageup"


def test_single_special_keys_allowed():
    assert parse_combo("pause") == (0, 0x13)
    assert parse_combo("scrolllock") == (0, 0x91)


@pytest.mark.parametrize("bad", ["ctrl+", "hyper+q", "", "ctrl+f25", "q", "7", "ctrl+f11+f12"])
def test_rejects(bad):
    with pytest.raises(ValueError):
        parse_combo(bad)
