"""Tests for LRC parsing, line selection and lrclib result ranking.

Every fixture below is invented placeholder text. No real lyric content
lives in this repository; runtime fetches land in a gitignored cache.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.lyrics import (
    Lyrics,
    LyricsProvider,
    cache_key,
    parse_lrc,
    pick_best,
    select_line,
)

SAMPLE = (
    "[00:01.00]placeholder line one\n"
    "[00:04.50]placeholder line two\n"
    "[00:09.00]placeholder line three\n"
)


def test_parse_lrc_reads_timestamps():
    parsed = parse_lrc(SAMPLE)
    assert parsed[0] == (1000, "placeholder line one")
    assert parsed[1] == (4500, "placeholder line two")
    assert parsed[2] == (9000, "placeholder line three")


def test_parse_lrc_handles_multiple_tags_per_line():
    assert parse_lrc("[00:01.00][00:05.00]repeated\n") == [
        (1000, "repeated"),
        (5000, "repeated"),
    ]


def test_parse_lrc_ignores_metadata_tags():
    assert parse_lrc("[ar:Someone]\n[ti:Something]\n[00:02.00]real\n") == [
        (2000, "real")
    ]


def test_parse_lrc_accepts_colon_centiseconds():
    assert parse_lrc("[00:02:50]alt format\n") == [(2500, "alt format")]


def test_parse_lrc_handles_three_digit_milliseconds():
    assert parse_lrc("[00:02.500]millis\n") == [(2500, "millis")]


def test_parse_lrc_sorts_out_of_order_input():
    out = parse_lrc("[00:09.00]third\n[00:01.00]first\n")
    assert [text for _, text in out] == ["first", "third"]


def test_parse_lrc_keeps_instrumental_gaps_as_blank():
    out = parse_lrc("[00:01.00]words\n[00:05.00]\n")
    assert out[1] == (5000, "")


def test_parse_lrc_returns_empty_for_unsynced_text():
    assert parse_lrc("just a plain paragraph\nwith no timestamps\n") == []


def test_select_line_before_first_returns_empty_with_hold():
    line, hold = select_line(parse_lrc(SAMPLE), 0)
    assert line == ""
    assert hold == 1000


def test_select_line_returns_current_and_hold():
    line, hold = select_line(parse_lrc(SAMPLE), 5000)
    assert line == "placeholder line two"
    assert hold == 4000


def test_select_line_on_exact_boundary_takes_new_line():
    line, _ = select_line(parse_lrc(SAMPLE), 4500)
    assert line == "placeholder line two"


def test_select_line_past_last_holds_open():
    line, hold = select_line(parse_lrc(SAMPLE), 99000)
    assert line == "placeholder line three"
    assert hold > 0


def test_select_line_on_empty_input():
    assert select_line([], 1000) == ("", 0)


def test_pick_best_prefers_synced_over_plain():
    results = [
        {"plainLyrics": "text", "duration": 200},
        {"syncedLyrics": "[00:01.00]x", "duration": 200},
    ]
    assert pick_best(results, 200) is results[1]


def test_pick_best_picks_closest_duration():
    results = [
        {"syncedLyrics": "a", "duration": 120},
        {"syncedLyrics": "b", "duration": 201},
    ]
    assert pick_best(results, 200) is results[1]


def test_pick_best_falls_back_to_plain_when_no_synced():
    results = [{"plainLyrics": "text", "duration": 200}]
    assert pick_best(results, 200) is results[0]


def test_pick_best_returns_none_on_empty():
    assert pick_best([], 200) is None


def test_pick_best_ignores_entries_with_no_lyrics_at_all():
    assert pick_best([{"duration": 200, "instrumental": True}], 200) is None


def test_cache_key_is_stable_and_filesystem_safe():
    first = cache_key("An Artist", "A Title/With: Punctuation")
    second = cache_key("an artist", "a title/with: punctuation")
    assert first == second
    assert "/" not in first and ":" not in first


def test_provider_reads_back_cached_entry(tmp_path):
    provider = LyricsProvider(cache_dir=tmp_path, user_agent="test/1.0")
    stored = Lyrics(kind="synced", synced=[(1000, "placeholder")], plain="")
    provider._write_cache("artist", "title", stored)
    loaded = provider._read_cache("artist", "title")
    assert loaded is not None
    assert loaded.kind == "synced"
    assert loaded.synced == [(1000, "placeholder")]


def test_provider_cache_miss_returns_none(tmp_path):
    provider = LyricsProvider(cache_dir=tmp_path, user_agent="test/1.0")
    assert provider._read_cache("nobody", "nothing") is None


def test_provider_survives_a_corrupt_cache_file(tmp_path):
    provider = LyricsProvider(cache_dir=tmp_path, user_agent="test/1.0")
    path = tmp_path / (cache_key("artist", "title") + ".json")
    path.write_text("{ not json", encoding="utf-8")
    assert provider._read_cache("artist", "title") is None
