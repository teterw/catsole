"""Tests for Thai word-boundary marking."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole import thai
from catsole.thai import ZWSP, mark_word_breaks

needs_dictionary = pytest.mark.skipif(
    thai.word_tokenize is None, reason="pythainlp is not installed"
)


@needs_dictionary
def test_marks_the_boundary_between_two_thai_words():
    assert mark_word_breaks("ทดสอบข้อความ") == f"ทดสอบ{ZWSP}ข้อความ"


@needs_dictionary
def test_does_not_mark_beside_a_space():
    # A space is already a break; a mark beside it would be noise.
    assert mark_word_breaks("ทดสอบ ข้อความ") == "ทดสอบ ข้อความ"


@needs_dictionary
def test_never_marks_before_a_character_that_cannot_start_a_line():
    # Mai yamok repeats the word before it and must stay attached to it.
    assert ZWSP + "ๆ" not in mark_word_breaks("ดีๆ")


def test_leaves_text_without_thai_alone():
    assert mark_word_breaks("placeholder line") == "placeholder line"
    assert mark_word_breaks("") == ""


def test_passes_thai_through_unmarked_without_the_dictionary(monkeypatch):
    monkeypatch.setattr(thai, "word_tokenize", None)
    mark_word_breaks.cache_clear()
    try:
        assert mark_word_breaks("ทดสอบข้อความ") == "ทดสอบข้อความ"
    finally:
        mark_word_breaks.cache_clear()
