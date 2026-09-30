"""Real-time spectrum from whatever the PC is playing.

Captures what is going to the speakers rather than a microphone: the
output's WASAPI loopback on Windows, and on Linux the default output's
monitor through parec, which PipeWire's PulseAudio layer provides as well
as PulseAudio itself. Nothing is
recorded or written anywhere: each buffer is turned into band levels and
discarded.

The device only needs a handful of coarse levels, so the FFT is reduced to
a few logarithmic bands. Music energy is distributed logarithmically -- an
octave near the top of the range spans thousands of hertz, one near the
bottom spans tens -- so linear bands would put almost everything in the
first bar and leave the rest flat.
"""

from __future__ import annotations

import collections
import logging
import math
import shutil
import subprocess
import sys
import threading
import time

log = logging.getLogger(__name__)

try:
    import numpy as np
except ImportError:  # pragma: no cover - exercised only without the extras
    np = None
try:
    import pyaudiowpatch as pyaudio
except ImportError:  # pragma: no cover - Windows only
    pyaudio = None

if sys.platform == "win32":
    AUDIO_AVAILABLE = np is not None and pyaudio is not None
    AUDIO_MISSING = "numpy/pyaudiowpatch not installed"
else:
    AUDIO_AVAILABLE = np is not None and shutil.which("parec") is not None
    AUDIO_MISSING = "numpy or parec (pulseaudio-utils) not installed"

BANDS = 16
LEVELS = 16  # 0..15, one hex digit per band

CHUNK = 1024
LOW_HZ = 40.0
HIGH_HZ = 16000.0

# The usable window. Narrow on purpose: a wide range leaves everything
# hovering mid-height, which reads as water sloshing rather than as music.
DB_FLOOR = -58.0
DB_CEIL = -20.0

# Snap up hard, fall fast enough to actually return between beats. Too
# slow a release is what makes bars look like a liquid level.
ATTACK = 0.80
RELEASE = 0.34

# Music loses roughly 3dB per octave as frequency rises, so without
# compensation the bass pins while the top half of the display sits dead.
# Lifting each band above the last evens the picture out.
TILT_DB_PER_BAND = 2.3

# Automatic gain, so a quiet track still fills the bars. The peak decays
# slowly, which keeps loud passages from permanently squashing quiet ones.
PEAK_DECAY = 0.995
MIN_PEAK = 0.30

# Beat tracking.
#
# Each frame adds one number to an onset envelope: how much energy has just
# appeared in the lower bands, which is what a drum hit or a strum is. The
# beat is found by fitting a grid to the last few seconds of it -- every
# tempo in the bob's range, at every offset -- and taking the grid whose
# points land on the most energy on average.
#
# This replaced timing the gaps between detected onsets. Those gaps came in
# whole 21ms frames, so their median was up to 2% off the true tempo; the
# tempo was then frozen, and the lag that left grew to a quarter or half a
# beat. Strummed eighth notes, common in Thai pop, also counted as beats,
# so 40% of Thai songs in the log locked at double speed. A grid fitted
# over seconds of audio is not limited by the frame size, and averaging
# per grid point rather than summing favours accented beats over the
# strums between them.
BEAT_MIN_BPM = 60.0
BEAT_MAX_BPM = 150.0      # anything faster is nodded at half speed, still on the beat
# A gentle preference for a comfortable nodding speed, so a groove that
# fits at both 62 and 124 BPM is nodded at 124.
BEAT_PRIOR_BPM = 110.0
BEAT_PRIOR_OCTAVES = 1.0
ENVELOPE_S = 8.0
MIN_ENVELOPE_S = 4.0
REPHASE_ENVELOPE_S = 2.5  # the tempo is known; only the offset is sought
# Coarser than this, a period a few ms out smears across fifteen beats and
# loses to a wrong grid that happens to sit on a step.
PERIOD_STEP_S = 0.003
ESTIMATE_EVERY = 12       # frames between estimates, about 0.25s
MIN_CLARITY = 1.6         # grid mean over envelope mean, to count at all

# Nothing is published until the same grid is found three times a second
# apart. Estimates a quarter second apart share nearly all their data, so
# agreeing would prove nothing; a still cat is better than a guessing one.
LOCK_TRY_EVERY = 4
LOCK_AGREE = 3
LOCK_TOLERANCE = 0.015

# Once locked, the tempo only moves within a few percent -- singing never
# gets to drag it -- and the grid eases toward each fit rather than
# jumping, so the bob stays continuous.
TRACK_RANGE = 0.03
PHASE_GAIN = 0.3
PERIOD_GAIN = 0.15

# A lock taken on an intro, or half a beat out, would otherwise last the
# whole song. Every couple of seconds the whole range is fitted again, and
# a clearly better grid three times running takes over.
RECHECK_EVERY = 8
RELOCK_MARGIN = 1.15
RELOCK_AFTER = 3
# Those corrections are allowed only while the lock settles. After that
# the tempo holds unless the song really changes tempo: a new grid not
# 2:1, 3:2 or 4:3 from the old, clearly better for about ten seconds.
SETTLE_S = 20.0
SETTLE_MOVES = 2
CHANGE_MARGIN = 1.3
CHANGE_AFTER = 5
_RELATED = (0.5, 2 / 3, 0.75, 1.0, 4 / 3, 1.5, 2.0)

# Any such move is glided into rather than landed in one step, which the
# cat showed as a lurch: the tempo eases toward the new one by a quarter of
# the way, at most 3%, every quarter second, and the offset with it.
GLIDE_GAIN = 0.25
GLIDE_MAX_STEP = 0.03


def _related(a: float, b: float) -> bool:
    """Whether two beat periods are the same pulse counted differently."""
    return any(abs(a / b / r - 1.0) <= 0.04 for r in _RELATED)

# After a pause, or a second with nothing on the beat, the song may come
# back anywhere against the grid, so the offset is found afresh; the beat
# is withheld meanwhile. If it does not come back near the old tempo, the
# lock is dropped and found from scratch.
QUIET_AFTER = 4
REPHASE_GIVE_UP = 8


def _nod_preference(period: float) -> float:
    bpm = 60.0 / period
    return math.exp(-0.5 * (math.log2(bpm / BEAT_PRIOR_BPM) / BEAT_PRIOR_OCTAVES) ** 2)


def band_edges(rate: int, bands: int = BANDS) -> list[tuple[int, int]]:
    """FFT bin ranges for logarithmically spaced bands."""
    nyquist = rate / 2.0
    high = min(HIGH_HZ, nyquist * 0.98)
    edges = []
    for i in range(bands + 1):
        frac = i / bands
        edges.append(LOW_HZ * ((high / LOW_HZ) ** frac))

    bin_hz = rate / CHUNK
    out = []
    for i in range(bands):
        lo = int(edges[i] / bin_hz)
        hi = int(edges[i + 1] / bin_hz)
        if hi <= lo:
            hi = lo + 1
        out.append((lo, hi))
    return out


def to_hex(levels) -> str:
    """Pack levels into one hex digit each, which is what goes on the wire."""
    return "".join("0123456789abcdef"[max(0, min(LEVELS - 1, int(v)))] for v in levels)


class AudioLevels:
    """Background loopback capture producing smoothed band levels."""

    def __init__(self, bands: int = BANDS):
        self.bands = bands
        self._levels = [0.0] * bands
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self.available = False
        self.device_name = ""
        self.last_error = ""

        # Published to the render loop, under _lock.
        self._period = 0.0
        self._beat_at = 0.0   # reference beat, free-running
        self._locked = False
        # Bumped by reset_beat, so an estimate already under way for the
        # last song cannot publish over the reset.
        self._generation = 0

        # Everything below belongs to the capture thread alone.
        self._seen_generation = 0
        self._frame_s = CHUNK / 48000.0
        self._env = collections.deque(maxlen=int(ENVELOPE_S / self._frame_s))
        self._prev_bands = None
        self._clock = None
        self._since = 0
        self._estimates = 0
        self._since_lock_try = 0
        self._recent = collections.deque(maxlen=LOCK_AGREE)
        self._locked_period = 0.0
        self._better = 0
        self._quiet = 0
        self._rephase = False
        self._rephase_tries = 0
        self._locked_at = 0.0
        self._settle_moves = 0
        self._glide = None

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not AUDIO_AVAILABLE:
            self.last_error = AUDIO_MISSING
            log.info("audio reactivity unavailable: %s", self.last_error)
            return
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="audio-capture", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def levels(self) -> list[int]:
        """Current band levels, 0..15."""
        with self._lock:
            return [int(v) for v in self._levels]

    def hex_levels(self) -> str:
        return to_hex(self.levels())

    @property
    def locked(self) -> bool:
        """True once the tempo has settled and stopped being re-estimated."""
        with self._lock:
            return self._locked

    def reset_beat(self) -> None:
        """Forget the tempo. Called on a track change, since the next song
        has no reason to share the last one's pulse.

        What is published stops at once. The tracking state belongs to the
        capture thread, which clears it on its next frame.
        """
        with self._lock:
            self._locked = False
            self._period = 0.0
            self._beat_at = 0.0
            self._generation += 1

    @property
    def bpm(self) -> float:
        """Estimated tempo, or 0 before enough beats have been seen."""
        with self._lock:
            return 60.0 / self._period if self._period > 0 else 0.0

    @property
    def beat_phase(self) -> float:
        """Position within the current beat: 0 on the beat, rising to 1.

        Predicted from the tracked period rather than from the last
        detection alone, so a missed onset does not stall the animation --
        it keeps moving on the grid and resynchronises when the next
        onset lands.
        """
        with self._lock:
            if self._period <= 0 or self._beat_at <= 0:
                return 0.0
            return ((time.monotonic() - self._beat_at) / self._period) % 1.0

    @property
    def silent(self) -> bool:
        with self._lock:
            return all(v < 0.5 for v in self._levels)

    # ---- capture ---------------------------------------------------------

    def _track_beat(self, bands: list[float]) -> None:
        """Add this frame to the onset envelope and keep the grid fitted."""
        with self._lock:
            generation = self._generation
        if generation != self._seen_generation:
            self._seen_generation = generation
            self._forget()

        f = self._frame_s
        read = time.monotonic()
        # Frames are timed by count rather than by when each read returned:
        # reads wobble by about 7ms and arrive in bursts. The count is eased
        # toward the reads so it follows the audio clock, and restarts when
        # audio stops arriving for a while, as it does on a pause.
        if self._clock is None or abs(read - (self._clock + f)) > 0.25:
            self._clock = read
            self._env.clear()
            self._prev_bands = None
            if self._locked_period > 0:
                self._start_rephase(generation)
        else:
            self._clock += f
            self._clock += 0.02 * (read - self._clock)

        current = np.array(bands[:8], dtype=np.float32)
        if self._prev_bands is None:
            self._prev_bands = current
            return
        # Only rises count: energy fading away is not an onset. The lowest
        # bands count twice, since kick and bass mark the beat more than
        # anything strummed over it.
        rise = np.maximum(0.0, current - self._prev_bands)
        self._prev_bands = current
        self._env.append(float(rise.sum() + rise[:4].sum()))

        self._since += 1
        need = REPHASE_ENVELOPE_S if self._rephase else MIN_ENVELOPE_S
        if self._since < ESTIMATE_EVERY or len(self._env) * f < need:
            return
        self._since = 0
        self._estimates += 1

        env = np.asarray(self._env, dtype=np.float32)
        newest = self._clock - f / 2.0  # an attack lands mid-frame, on average
        if self._locked_period <= 0:
            self._try_lock(env, newest, generation)
        elif self._rephase:
            self._find_again(env, newest, generation)
        else:
            self._follow(env, newest, generation)

    def _try_lock(self, env, newest: float, generation: int) -> None:
        self._since_lock_try += 1
        if self._since_lock_try < LOCK_TRY_EVERY:
            return
        self._since_lock_try = 0

        score, period, offset = self._fit(env)
        if not self._clear(score, period, env):
            self._recent.clear()
            return
        beat_at = newest - offset
        if self._recent:
            last_period, last_at = self._recent[-1]
            drift = ((beat_at - last_at) / last_period) % 1.0
            if (abs(period / last_period - 1.0) > LOCK_TOLERANCE
                    or min(drift, 1.0 - drift) > 0.15):
                self._recent.clear()
        self._recent.append((period, beat_at))
        if len(self._recent) < LOCK_AGREE:
            return

        self._locked_period = period
        self._locked_at = newest
        self._settle_moves = 0
        self._better = 0
        self._quiet = 0
        if self._publish(generation, period, beat_at, locked=True):
            log.info("tempo locked at %.1f BPM", 60.0 / period)

    def _find_again(self, env, newest: float, generation: int) -> None:
        near = self._locked_period
        periods = np.linspace(near * (1 - TRACK_RANGE), near * (1 + TRACK_RANGE), 13)
        score, period, offset = self._search(
            env, periods, lambda p: np.arange(0.0, p, self._frame_s)
        )
        score, period, offset = self._refine(env, period, offset)
        if self._clear(score, period, env):
            self._rephase = False
            self._quiet = 0
            self._publish(generation, period, newest - offset)
            return
        # The beat has not come back near the old tempo. Rather than wait
        # on it for the rest of the song, start over.
        self._rephase_tries += 1
        if self._rephase_tries >= REPHASE_GIVE_UP:
            self._rephase = False
            self._locked_period = 0.0
            self._recent.clear()
            self._publish(generation, 0.0, 0.0, locked=False)

    def _follow(self, env, newest: float, generation: int) -> None:
        with self._lock:
            period, beat_at = self._period, self._beat_at
        if period <= 0:
            return
        if self._glide is not None:
            self._glide_step(newest, generation, period, beat_at)
            return
        offset_now = (newest - beat_at) % period
        score, fitted, offset = self._refine(env, period, offset_now)
        if not self._clear(score, fitted, env):
            self._quiet += 1
            if self._quiet >= QUIET_AFTER:
                self._start_rephase(generation)
        else:
            self._quiet = 0
            error = offset - offset_now
            if error > fitted / 2:
                error -= fitted
            if error < -fitted / 2:
                error += fitted
            target = period + PERIOD_GAIN * (fitted - period)
            lo = self._locked_period * (1 - TRACK_RANGE)
            hi = self._locked_period * (1 + TRACK_RANGE)
            new_period = min(hi, max(lo, target))
            self._publish(
                generation,
                new_period,
                self._rebased(newest, new_period, period, beat_at) - PHASE_GAIN * error,
            )

        if self._estimates % RECHECK_EVERY:
            return
        best, best_period, best_offset = self._fit(env)
        same_tempo = abs(best_period / period - 1.0) <= TRACK_RANGE
        drift = ((best_offset - offset_now) / period) % 1.0
        same_grid = same_tempo and min(drift, 1.0 - drift) <= 0.15
        settling = (
            newest - self._locked_at < SETTLE_S
            and self._settle_moves < SETTLE_MOVES
        )
        if settling:
            # Early on, a lock taken on an intro or half a beat out is
            # put right.
            wanted, margin, runs = not same_grid, RELOCK_MARGIN, RELOCK_AFTER
        else:
            # After that the tempo stays. Verse and chorus often fit grids
            # 3:2 or 2:1 apart about equally well, and following each in
            # turn made the cat stumble; only a genuinely new tempo moves it.
            wanted = not _related(best_period, period)
            margin, runs = CHANGE_MARGIN, CHANGE_AFTER
        if wanted and best > margin * score:
            self._better += 1
        else:
            self._better = 0
        if self._better >= runs:
            self._better = 0
            self._rephase = False
            if settling:
                self._settle_moves += 1
            else:
                # A new tempo is a new lock, and gets to settle like one.
                self._locked_at = newest
                self._settle_moves = 0
            self._locked_period = best_period
            self._glide = (best_period, newest - best_offset)
            log.info("tempo moving to %.1f BPM", 60.0 / best_period)

    def _rebased(self, newest: float, new_period: float,
                 period: float | None = None, beat_at: float | None = None) -> float:
        """Where to anchor a grid of new_period so the bob is exactly where
        it was at newest. The anchor can be a minute back; changing the
        period under it re-timed every beat since, and moved the bob."""
        if period is None:
            with self._lock:
                period, beat_at = self._period, self._beat_at
        fraction = ((newest - beat_at) % period) / period
        return newest - fraction * new_period

    def _glide_step(self, newest: float, generation: int,
                    period: float, beat_at: float) -> None:
        target_period, target_at = self._glide
        now = ((newest - beat_at) % period) / period
        wanted = ((newest - target_at) % target_period) / target_period
        error = wanted - now
        if error > 0.5:
            error -= 1.0
        if error < -0.5:
            error += 1.0
        step = GLIDE_GAIN * (target_period - period)
        limit = GLIDE_MAX_STEP * period
        new_period = period + max(-limit, min(limit, step))
        fraction = now + GLIDE_GAIN * error
        if abs(new_period / target_period - 1.0) < 0.004 and abs(error) < 0.03:
            new_period, fraction = target_period, wanted
            self._glide = None
            log.info("tempo moved to %.1f BPM", 60.0 / target_period)
        self._publish(generation, new_period, newest - fraction * new_period)

    def _start_rephase(self, generation: int) -> None:
        self._glide = None
        self._rephase = True
        self._rephase_tries = 0
        self._publish(generation, 0.0, 0.0)

    def _publish(self, generation: int, period: float, beat_at: float,
                 locked: bool | None = None) -> bool:
        with self._lock:
            if self._generation != generation:
                return False  # a track change landed mid-estimate
            self._period = period
            self._beat_at = beat_at
            if locked is not None:
                self._locked = locked
            return True

    def _forget(self) -> None:
        self._env.clear()
        self._prev_bands = None
        self._since = 0
        self._since_lock_try = 0
        self._recent.clear()
        self._locked_period = 0.0
        self._better = 0
        self._quiet = 0
        self._rephase = False
        self._rephase_tries = 0
        self._locked_at = 0.0
        self._settle_moves = 0
        self._glide = None

    # ---- grid fitting ----------------------------------------------------

    def _clear(self, score: float, period: float, env) -> bool:
        """Whether a fit stands out from the envelope at all."""
        mean = float(env.mean())
        return mean > 0 and score / _nod_preference(period) / mean >= MIN_CLARITY

    def _grid_means(self, env, period: float, offsets):
        """Mean envelope on the grid at each offset, in seconds back from
        the newest frame, reading between frames by interpolation."""
        f, n = self._frame_s, len(env)
        k = np.arange(int((n - 1) * f / period) + 1)
        idx = (n - 1) - (offsets[:, None] + k[None, :] * period) / f
        inside = idx >= 0
        lo = np.clip(np.floor(idx).astype(int), 0, n - 1)
        hi = np.clip(lo + 1, 0, n - 1)
        w = idx - np.floor(idx)
        values = (env[lo] * (1 - w) + env[hi] * w) * inside
        return values.sum(axis=1) / np.maximum(inside.sum(axis=1), 1)

    def _search(self, env, periods, offsets_for):
        """The best (score, period, offset) among the candidate grids."""
        best = (-1.0, 0.0, 0.0)
        for period in periods:
            offsets = offsets_for(period)
            means = self._grid_means(env, period, offsets)
            j = int(np.argmax(means))
            score = float(means[j]) * _nod_preference(period)
            if score > best[0]:
                best = (score, float(period), float(offsets[j]))
        return best

    def _refine(self, env, period: float, offset: float):
        """The best grid close to the one given."""
        periods = np.linspace(period * (1 - TRACK_RANGE), period * (1 + TRACK_RANGE), 25)
        return self._search(
            env, periods,
            lambda p: np.linspace(offset - 0.12 * p, offset + 0.12 * p, 25) % p,
        )

    def _fit(self, env):
        """The best grid over the whole range: coarse, then refined."""
        coarse = np.arange(60.0 / BEAT_MAX_BPM, 60.0 / BEAT_MIN_BPM, PERIOD_STEP_S)
        _, period, offset = self._search(
            env, coarse, lambda p: np.arange(0.0, p, self._frame_s)
        )
        return self._refine(env, period, offset)

    def _run(self) -> None:
        source = None
        try:
            source = WasapiSource() if sys.platform == "win32" else ParecSource()
            rate, channels = source.rate, source.channels
            self._frame_s = CHUNK / float(rate)
            self._env = collections.deque(maxlen=int(ENVELOPE_S / self._frame_s))
            self.device_name = source.name

            edges = band_edges(rate, self.bands)
            window = np.hanning(CHUNK).astype(np.float32)
            smoothed = np.zeros(self.bands, dtype=np.float32)
            peak = 0.0
            self.available = True
            log.info("audio reactivity on: %s @ %dHz", self.device_name, rate)

            while not self._stop.is_set():
                buf = source.read(CHUNK)
                samples = np.frombuffer(buf, dtype=np.int16).astype(np.float32)
                if channels > 1:
                    samples = samples.reshape(-1, channels).mean(axis=1)
                if samples.size < CHUNK:
                    continue

                spectrum = np.abs(np.fft.rfft(samples[:CHUNK] * window))
                # Normalise against full scale so the result is level-independent.
                spectrum /= (CHUNK * 32768.0) / 4.0

                raw = []
                for i, (lo, hi) in enumerate(edges):
                    chunk = spectrum[lo:hi]
                    magnitude = float(chunk.max()) if chunk.size else 0.0
                    db = 20.0 * math.log10(magnitude + 1e-9)
                    db += i * TILT_DB_PER_BAND
                    norm = (db - DB_FLOOR) / (DB_CEIL - DB_FLOOR)
                    raw.append(max(0.0, min(1.0, norm)))

                # Track the loudest band and normalise against it, so the
                # display uses its full height whatever the volume is.
                frame_peak = max(raw) if raw else 0.0
                peak = max(frame_peak, peak * PEAK_DECAY)
                gain = 1.0 / max(MIN_PEAK, peak)

                for i, value in enumerate(raw):
                    target = min(1.0, value * gain) * (LEVELS - 1)
                    rate_of_change = ATTACK if target > smoothed[i] else RELEASE
                    smoothed[i] += (target - smoothed[i]) * rate_of_change

                self._track_beat(raw)

                with self._lock:
                    self._levels = [float(v) for v in smoothed]

        except Exception as exc:
            self.last_error = str(exc)
            log.warning("audio capture stopped: %s", exc)
        finally:
            self.available = False
            if source is not None:
                source.close()


class WasapiSource:
    """The loopback companion of Windows' current default output."""

    def __init__(self):
        self._audio = pyaudio.PyAudio()
        try:
            api = self._audio.get_host_api_info_by_type(pyaudio.paWASAPI)
            default_out = self._audio.get_device_info_by_index(api["defaultOutputDevice"])
            # Prefer the loopback whose name matches the active output, so
            # switching speakers does not leave us recording the wrong device.
            chosen = None
            for dev in self._audio.get_loopback_device_info_generator():
                if default_out["name"] in dev["name"]:
                    chosen = dev
                    break
                if chosen is None:
                    chosen = dev
            if chosen is None:
                raise OSError("no WASAPI loopback device")
            self.rate = int(chosen["defaultSampleRate"])
            self.channels = int(chosen["maxInputChannels"])
            self.name = chosen["name"]
            self._stream = self._audio.open(
                format=pyaudio.paInt16,
                channels=self.channels,
                rate=self.rate,
                input=True,
                input_device_index=chosen["index"],
                frames_per_buffer=CHUNK,
            )
        except Exception:
            self._audio.terminate()
            raise

    def read(self, frames: int) -> bytes:
        return self._stream.read(frames, exception_on_overflow=False)

    def close(self) -> None:
        try:
            self._stream.stop_stream()
            self._stream.close()
        except Exception:
            pass
        try:
            self._audio.terminate()
        except Exception:
            pass


class ParecSource:
    """Linux: the default output's monitor, recorded by parec.

    A monitor keeps delivering silence when nothing plays, unlike a WASAPI
    loopback, so frames arrive at an even pace throughout.
    """

    def __init__(self, rate: int = 48000, channels: int = 2):
        self.rate = rate
        self.channels = channels
        self.name = "default output monitor"
        self._proc = subprocess.Popen(
            [
                "parec",
                "--device=@DEFAULT_MONITOR@",
                "--format=s16le",
                f"--rate={rate}",
                f"--channels={channels}",
                "--latency-msec=20",
                "--raw",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )

    def read(self, frames: int) -> bytes:
        want = frames * self.channels * 2
        data = self._proc.stdout.read(want)
        if len(data) < want:
            raise OSError("parec stopped")
        return data

    def close(self) -> None:
        self._proc.terminate()
        try:
            self._proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._proc.kill()
