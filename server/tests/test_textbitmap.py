"""Tests for rendering the top-strip title as a bitmap."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole import textbitmap
from catsole.textbitmap import HEIGHT, MAX_WIDTH, needs_bitmap, render

needs_font = pytest.mark.skipif(textbitmap.find_font() is None, reason="no Thai-capable font")


def test_only_text_the_board_cannot_draw_needs_a_bitmap():
    assert needs_bitmap("Atom Chanakan - อ้าว")
    assert not needs_bitmap("An Artist - A Title")
    assert not needs_bitmap("")


@needs_font
def test_renders_eleven_rows_packed_like_xbm():
    width, data = render("Atom Chanakan - อ้าว")
    assert 40 < width <= MAX_WIDTH
    assert len(data) == ((width + 7) // 8) * HEIGHT
    assert any(data)                    # something is lit


@needs_font
def test_a_long_title_is_cut_to_the_widest_the_board_holds():
    width, data = render("ทดสอบข้อความ " * 40)
    assert width == MAX_WIDTH
    assert len(data) == (MAX_WIDTH // 8) * HEIGHT


def test_xbm_packing_puts_the_leftmost_pixel_in_the_low_bit():
    # One row, pixels 0 and 9 lit: 0b00000001, 0b00000010.
    assert textbitmap.pack_rows([[1, 0, 0, 0, 0, 0, 0, 0, 0, 1]], 10) == bytes([0x01, 0x02])


def test_without_a_font_there_is_no_bitmap(monkeypatch):
    monkeypatch.setattr(textbitmap, "find_font", lambda: None)
    textbitmap.render.cache_clear()
    textbitmap._font.cache_clear()
    try:
        assert render("Atom Chanakan - อ้าว") is None
    finally:
        textbitmap.render.cache_clear()
        textbitmap._font.cache_clear()
