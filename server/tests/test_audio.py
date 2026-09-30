"""Tests for beat tracking, driven with synthetic grooves whose beats are known.

Frames are rendered in the tracker's own 0..1 band domain, one per 1024
samples at 48kHz, with the read-time wobble measured on the real machine.
"""

import math
import random
import sys
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole import audio

FRAME_S = 1024 / 48000.0
BANDS = 16
SOUNDS = {  # bands, amplitude, decay in seconds
    "kick": (range(0, 4), 0.95, 0.07),
    "snare": (range(3, 11), 0.65, 0.09),
    "strum": (range(2, 10), 0.70, 0.16),
    "vocal": (range(3, 9), 0.55, 0.35),
}


def groove(bpm, seconds, seed, drums=True, eighths=True):
    """Kick on 1 and 3, snare on 2 and 4, strums on every eighth, plus
    singing onsets that have nothing to do with the beat."""
    rng = random.Random(seed)
    beat = 60.0 / bpm
    events, beats = [], []
    t, i = 0.5, 0
    while drums and t < seconds:
        beats.append(t)
        events.append((t, "kick" if i % 2 == 0 else "snare"))
        events.append((t, "strum"))
        if eighths:
            events.append((t + beat / 2, "strum"))
        t, i = t + beat, i + 1
    t = 0.5
    while t < seconds:
        t += rng.expovariate(2.5)
        events.append((t, "vocal"))
    return events, beats


def render(events, seconds, seed):
    rng = np.random.default_rng(seed)
    frames = np.zeros((int(seconds / FRAME_S), BANDS), dtype=np.float32)
    for at, kind in events:
        bands, amp, decay = SOUNDS[kind]
        first = int(at / FRAME_S)
        for k in range(first, min(len(frames), first + int(6 * decay / FRAME_S) + 2)):
            end = (k + 1) * FRAME_S
            if end > at:
                share = min(1.0, (end - at) / FRAME_S)
                frames[k, list(bands)] += amp * share * math.exp(-max(0.0, end - at - FRAME_S) / decay)
    frames += rng.normal(0.18, 0.03, frames.shape).astype(np.float32)
    return np.clip(frames, 0.0, 1.0)


class Clock:
    now = 1000.0

    @classmethod
    def monotonic(cls):
        return cls.now


@pytest.fixture
def clock(monkeypatch):
    monkeypatch.setattr(audio, "time", Clock)
    Clock.now = 1000.0
    return Clock


def play(tracker, frames, clock, seed=0, start=0.0):
    """Feed frames in real time; returns the times the cat's dips land."""
    rng = np.random.default_rng(seed + 1)
    dips, last = [], None
    for k, frame in enumerate(frames):
        t = start + (k + 1) * FRAME_S
        clock.now = 1000.0 + t + rng.normal(0.0, 0.007)
        tracker._track_beat([float(v) for v in frame])
        period, beat_at = tracker._period, tracker._beat_at
        if period > 0 and beat_at > 0:
            n = math.floor((1000.0 + t - beat_at) / period)
            if last is not None and n > last:
                dips.append(beat_at + n * period - 1000.0)
            last = n
    return np.array(dips)


def on_beat(dips, beats, after):
    dips = dips[dips > after]
    beats = np.array(beats)
    return np.mean([np.min(np.abs(beats - d)) <= 0.06 for d in dips])


def test_strummed_eighths_do_not_double_the_tempo(clock):
    # The log had 40% of Thai songs locked at double speed: every strum
    # between the beats was counted as a beat.
    events, _ = groove(105, 20.0, seed=1)
    tracker = audio.AudioLevels()
    play(tracker, render(events, 20.0, 1), clock)
    assert tracker.locked
    assert tracker.bpm == pytest.approx(105, rel=0.02)


def test_dips_land_on_the_beat(clock):
    # Gap timing froze a tempo up to 2% out, which left the bob a quarter
    # to half a beat behind; the grid fit keeps it within a few ms.
    events, beats = groove(112, 40.0, seed=2)
    dips = play(audio.AudioLevels(), render(events, 40.0, 2), clock)
    assert on_beat(dips, beats, after=15.0) >= 0.9


def test_a_ballad_is_not_nodded_at_double_speed(clock):
    events, _ = groove(72, 24.0, seed=3)
    tracker = audio.AudioLevels()
    play(tracker, render(events, 24.0, 3), clock)
    assert tracker.bpm == pytest.approx(72, rel=0.02)


def test_nothing_is_published_while_there_is_only_singing(clock):
    events, _ = groove(105, 10.0, seed=4, drums=False)
    tracker = audio.AudioLevels()
    dips = play(tracker, render(events, 10.0, 4), clock)
    assert not tracker.locked
    assert len(dips) <= 2


def test_reset_stops_the_beat_at_once(clock):
    events, _ = groove(105, 20.0, seed=5)
    tracker = audio.AudioLevels()
    play(tracker, render(events, 20.0, 5), clock)
    assert tracker.bpm > 0
    tracker.reset_beat()
    assert tracker.bpm == 0
    assert not tracker.locked


def test_an_estimate_under_way_cannot_publish_over_a_reset(clock):
    tracker = audio.AudioLevels()
    stale = tracker._generation
    tracker.reset_beat()
    assert tracker._publish(stale, 0.5, 1000.0, locked=True) is False
    assert tracker.bpm == 0


def test_a_settled_tempo_ignores_the_same_pulse_counted_differently(clock):
    # Verse and chorus can fit grids 3:2 apart about equally well. The cat
    # used to follow each in turn mid-song, which read as stumbling.
    first, _ = groove(105, 35.0, seed=6)
    second, _ = groove(70, 30.0, seed=7)
    tracker = audio.AudioLevels()
    play(tracker, render(first, 35.0, 6), clock)
    play(tracker, render(second, 30.0, 7), clock, start=35.0)
    assert tracker.bpm == pytest.approx(105, rel=0.04)


def test_a_real_tempo_change_is_still_followed(clock):
    first, _ = groove(105, 35.0, seed=8)
    second, _ = groove(124, 40.0, seed=9)
    tracker = audio.AudioLevels()
    play(tracker, render(first, 35.0, 8), clock)
    play(tracker, render(second, 40.0, 9), clock, start=35.0)
    # Nodding at 124 or at half of it are both on the new song's beat.
    assert tracker.bpm == pytest.approx(124, rel=0.04) or tracker.bpm == pytest.approx(62, rel=0.04)


def test_related_periods():
    assert audio._related(0.5, 0.75)       # 3:2
    assert audio._related(0.5, 1.0)        # 2:1
    assert not audio._related(0.5, 0.58)   # a different tempo
