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

import logging
import math
import threading

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
