"""Real-time spectrum from whatever the PC is playing.

Captures the output device's loopback stream through WASAPI, so it hears
the mix that reaches the speakers rather than a microphone. Nothing is
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
import threading
import time

log = logging.getLogger(__name__)

try:
    import numpy as np
    import pyaudiowpatch as pyaudio

    AUDIO_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without the extras
    np = None
    pyaudio = None
    AUDIO_AVAILABLE = False

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

# Beat tracking. Onsets come from positive spectral flux in the lower
# bands -- energy appearing where there was less a moment ago, which is
# what a drum hit is. Hopping on every onset looks erratic, because
# detection is never perfect, so the gaps between onsets are used to
# estimate a period and the beat is predicted on that grid instead.
FLUX_HISTORY = 48
ONSET_SENSITIVITY = 1.45
MIN_BEAT_GAP_S = 0.26   # 230 BPM ceiling
MAX_BEAT_GAP_S = 1.10   # 55 BPM floor

# How far the beat grid is dragged toward each detected onset. Snapping
# the grid onto every detection made the phase jump backwards whenever
# detection was early or late, which showed up as a stutter. Correcting
# a fifth of the error keeps the phase continuous and still converges
# within a few beats.
PHASE_CORRECTION = 0.20

# A rhythm can be counted at one speed or at twice it, and the median
# flips between the two when a track has offbeats as strong as its
# downbeats. Each flip resets the grid, which is what reads as the
# animation getting confused. A new estimate that is half or double
# the current one has to persist before it is believed.
DOUBLE_TIME_GUARD = 6


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

        self._flux_hist = collections.deque(maxlen=FLUX_HISTORY)
        self._gaps = collections.deque(maxlen=10)
        self._prev_bands = None
        self._last_onset = 0.0
        self._period = 0.0
        self._beat_at = 0.0   # reference beat, free-running
        self._flips = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if not AUDIO_AVAILABLE:
            self.last_error = "numpy/pyaudiowpatch not installed"
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

    def _open_loopback(self, audio):
        """Find the loopback companion of the current default output."""
        api = audio.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = audio.get_device_info_by_index(api["defaultOutputDevice"])

        # Prefer the loopback whose name matches the active output, so
        # switching speakers does not leave us recording the wrong device.
        chosen = None
        for dev in audio.get_loopback_device_info_generator():
            if default_out["name"] in dev["name"]:
                chosen = dev
                break
            if chosen is None:
                chosen = dev
        return chosen

    def _track_beat(self, bands: list[float]) -> None:
        """Find onsets and keep a running estimate of the beat period."""
        current = np.array(bands[:8], dtype=np.float32)
        if self._prev_bands is None:
            self._prev_bands = current
            return

        # Only rises count: energy fading away is not an onset.
        flux = float(np.sum(np.maximum(0.0, current - self._prev_bands)))
        self._prev_bands = current
        self._flux_hist.append(flux)

        if len(self._flux_hist) < 12:
            return

        history = np.array(self._flux_hist, dtype=np.float32)
        threshold = history.mean() + ONSET_SENSITIVITY * history.std()
        now = time.monotonic()

        if flux <= threshold or flux < 0.04:
            return
        if now - self._last_onset < MIN_BEAT_GAP_S:
            return

        if self._last_onset > 0:
            gap = now - self._last_onset
            if MIN_BEAT_GAP_S <= gap <= MAX_BEAT_GAP_S:
                self._gaps.append(gap)

        self._last_onset = now

        # The median rejects the occasional double-time hit or missed beat
        # that a mean would smear through the estimate.
        if len(self._gaps) >= 4:
            candidate = float(np.median(self._gaps))
            with self._lock:
                if self._period > 0:
                    ratio = candidate / self._period
                    halved = 0.40 < ratio < 0.62
                    doubled = 1.60 < ratio < 2.50
                    if halved or doubled:
                        self._flips += 1
                        if self._flips < DOUBLE_TIME_GUARD:
                            candidate = self._period
                        else:
                            self._flips = 0
                    else:
                        self._flips = 0
                self._period = candidate

        with self._lock:
            if self._period <= 0:
                return
            if self._beat_at <= 0:
                self._beat_at = now
                return

            # Drag the grid toward this onset instead of restarting it on
            # top of it. Where the onset fell relative to the nearest grid
            # beat, signed so an early hit pulls back and a late one pushes
            # forward.
            offset = (now - self._beat_at) % self._period
            if offset > self._period / 2.0:
                offset -= self._period
            self._beat_at += offset * PHASE_CORRECTION

    def _run(self) -> None:
        audio = None
        stream = None
        try:
            audio = pyaudio.PyAudio()
            device = self._open_loopback(audio)
            if device is None:
                self.last_error = "no WASAPI loopback device"
                log.warning("audio: %s", self.last_error)
                return

            rate = int(device["defaultSampleRate"])
            channels = int(device["maxInputChannels"])
            self.device_name = device["name"]

            stream = audio.open(
                format=pyaudio.paInt16,
                channels=channels,
                rate=rate,
                input=True,
                input_device_index=device["index"],
                frames_per_buffer=CHUNK,
            )

            edges = band_edges(rate, self.bands)
            window = np.hanning(CHUNK).astype(np.float32)
            smoothed = np.zeros(self.bands, dtype=np.float32)
            peak = 0.0
            self.available = True
            log.info("audio reactivity on: %s @ %dHz", self.device_name, rate)

            while not self._stop.is_set():
                buf = stream.read(CHUNK, exception_on_overflow=False)
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
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            try:
                if audio is not None:
                    audio.terminate()
            except Exception:
                pass
