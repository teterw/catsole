"""CPU and GPU telemetry, layered so a missing source costs one field.

Three sources, tried in order and merged rather than chained:

1. LibreHardwareMonitor's local web API. The only source for Ryzen CPU
   package temperature, since that needs a ring0 driver. Optional.
2. `nvidia-smi`, which ships with the NVIDIA driver, for GPU temp, load
   and VRAM. Needs no install.
3. `psutil` for CPU load and clock.

Any field that cannot be read stays None and renders as `--` on the device.
Stats mode never blanks wholesale because one sensor is unavailable.
"""

from __future__ import annotations

import logging
import re
import subprocess
import time

import psutil
import requests

log = logging.getLogger(__name__)

LHM_URL = "http://localhost:8085/data.json"
LHM_TIMEOUT = 0.4

# Once LHM is found to be down, stop hammering a closed port for a while.
LHM_RETRY_AFTER_S = 30.0

NVIDIA_QUERY = (
    "temperature.gpu,utilization.gpu,memory.used,memory.total,fan.speed"
)
NVIDIA_FIELDS = ("temp", "load", "vram_used", "vram_total", "fan")
NVIDIA_TIMEOUT = 2.0

# Sensor names differ across LHM versions and chips; first match wins.
CPU_TEMP_CANDIDATES = ("Core (Tctl/Tdie)", "Tctl/Tdie", "CPU Package", "Core Average")
CPU_LOAD_CANDIDATES = ("CPU Total",)
CPU_CLOCK_CANDIDATES = ("Core #1", "CPU Core #1", "Core Max")
GPU_TEMP_CANDIDATES = ("GPU Core", "GPU Temperature")
GPU_LOAD_CANDIDATES = ("GPU Core", "D3D 3D")

_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")

# Keep the console window from flashing when launched via pythonw.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def empty_stats() -> dict:
    """The shape every poll returns, with nothing filled in."""
    return {
        "cpu": {"temp": None, "load": None, "clock": None},
        "gpu": {"temp": None, "load": None, "vram_used": None, "vram_total": None},
        "ram": {"used": None, "total": None, "percent": None},
        "fans": [],
    }


def parse_number(text) -> float | None:
    """Pull the leading number out of a formatted sensor value."""
    if not text or not isinstance(text, str):
        return None
    match = _NUMBER.search(text)
    if not match:
        return None
    try:
        # LHM formats for the system locale, so a comma may be the decimal point.
        return float(match.group(0).replace(",", "."))
    except ValueError:
        return None


def flatten_lhm(node: dict, prefix: str = "") -> list[tuple[str, str, str]]:
    """Walk LHM's nested tree into (path, sensor name, value) rows."""
    if not node:
        return []

    text = node.get("Text", "")
    children = node.get("Children") or []

    if not children:
        value = node.get("Value")
        if value:
            return [(prefix, text, value)]
        return []

    path = f"{prefix}/{text}" if prefix else text
    rows: list[tuple[str, str, str]] = []
    for child in children:
        rows.extend(flatten_lhm(child, path))
    return rows


def find_sensor(
    rows: list[tuple[str, str, str]],
    candidates: list[str] | tuple[str, ...],
    path_contains: str = "",
) -> float | None:
    """Find the first candidate sensor present, as a number.

    `path_contains` disambiguates sensors that share a name across devices —
    "GPU Core" is both a temperature and a load in the same tree.
    """
    for candidate in candidates:
        for path, sensor, value in rows:
            if candidate.lower() not in sensor.lower():
                continue
            if path_contains and path_contains.lower() not in path.lower():
                continue
            number = parse_number(value)
            if number is not None:
                return number
    return None


def find_fans(rows: list[tuple[str, str, str]], limit: int = 3) -> list[dict]:
    """Pull fan speeds out of a flattened LibreHardwareMonitor tree.

    Matched on the unit rather than the name: boards label fans
    inconsistently ("Fan #2", "Chassis Fan", "Pump"), but anything
    reported in RPM is a fan. Stopped fans are kept -- zero RPM is a real
    reading and worth showing, not an absent sensor.
    """
    out = []
    for path, sensor, value in rows:
        if "rpm" not in value.lower():
            continue
        speed = parse_number(value)
        if speed is None:
            continue
        name = sensor.strip() or "fan"
        out.append({"name": name[:12], "rpm": round(speed)})
        if len(out) >= limit:
            break
    return out


def parse_nvidia_smi(csv_line: str) -> dict:
    """Parse one CSV row from nvidia-smi into a partial GPU dict.

    Fields reported as [N/A] are omitted rather than zeroed, so an
    unsupported sensor reads as unavailable instead of as a real zero.
    """
    if not csv_line or not csv_line.strip():
        return {}

    parts = [part.strip() for part in csv_line.split(",")]
    if len(parts) < len(NVIDIA_FIELDS):
        return {}

    out = {}
    for name, raw in zip(NVIDIA_FIELDS, parts):
        value = parse_number(raw)
        if value is not None:
            out[name] = value
    return out


class HardwareReader:
    """Polls all available sensor sources and merges what it gets."""

    def __init__(self):
        self._lhm_down_until = 0.0
        self._nvidia_missing = False
        # First psutil call always reads 0.0; prime it so the first real
        # poll has a meaningful interval behind it.
        psutil.cpu_percent(interval=None)

    def poll(self) -> dict:
        stats = empty_stats()
        self._read_psutil(stats)
        self._read_nvidia(stats)
        self._read_lhm(stats)
        return stats

    def _read_psutil(self, stats: dict) -> None:
        try:
            stats["cpu"]["load"] = round(psutil.cpu_percent(interval=None), 1)
            freq = psutil.cpu_freq()
            if freq is not None and freq.current:
                stats["cpu"]["clock"] = round(freq.current, 0)
        except Exception as exc:
            log.debug("psutil cpu read failed: %s", exc)

        # Reported in MB to match VRAM, so the device formats both the
        # same way rather than carrying two unit conventions.
        try:
            mem = psutil.virtual_memory()
            stats["ram"]["used"] = round((mem.total - mem.available) / (1024 * 1024))
            stats["ram"]["total"] = round(mem.total / (1024 * 1024))
            stats["ram"]["percent"] = round(mem.percent, 1)
        except Exception as exc:
            log.debug("psutil memory read failed: %s", exc)

    def _read_nvidia(self, stats: dict) -> None:
        if self._nvidia_missing:
            return
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    f"--query-gpu={NVIDIA_QUERY}",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                timeout=NVIDIA_TIMEOUT,
                creationflags=_NO_WINDOW,
            )
        except FileNotFoundError:
            # No NVIDIA driver on this machine; stop trying.
            self._nvidia_missing = True
            log.info("nvidia-smi not found; GPU stats unavailable")
            return
        except Exception as exc:
            log.debug("nvidia-smi failed: %s", exc)
            return

        if result.returncode != 0:
            return
        first_line = result.stdout.strip().splitlines()
        if not first_line:
            return
        gpu = parse_nvidia_smi(first_line[0])
        fan_pct = gpu.pop("fan", None)
        stats["gpu"].update(gpu)

        # The GPU reports its own fan as a percentage, which needs no
        # elevated driver. Zero is a real reading on modern cards -- they
        # stop the fan entirely when idle -- so it is kept, not discarded.
        if fan_pct is not None:
            stats["fans"] = [{"name": "gpu", "pct": round(fan_pct)}]

    def _read_lhm(self, stats: dict) -> None:
        """Fill anything LHM can provide that the cheaper sources could not."""
        now = time.monotonic()
        if now < self._lhm_down_until:
            return

        try:
            response = requests.get(LHM_URL, timeout=LHM_TIMEOUT)
            tree = response.json()
        except Exception:
            # Expected whenever LHM simply is not running.
            self._lhm_down_until = now + LHM_RETRY_AFTER_S
            return

        rows = flatten_lhm(tree)
        if not rows:
            return

        # CPU temperature is the one thing only LHM can give us here.
        temp = find_sensor(rows, CPU_TEMP_CANDIDATES, path_contains="Temperature")
        if temp is not None:
            stats["cpu"]["temp"] = round(temp, 1)

        if stats["cpu"]["load"] is None:
            load = find_sensor(rows, CPU_LOAD_CANDIDATES, path_contains="Load")
            if load is not None:
                stats["cpu"]["load"] = round(load, 1)

        if stats["cpu"]["clock"] is None:
            clock = find_sensor(rows, CPU_CLOCK_CANDIDATES, path_contains="Clock")
            if clock is not None:
                stats["cpu"]["clock"] = round(clock, 0)

        if stats["gpu"]["temp"] is None:
            gpu_temp = find_sensor(rows, GPU_TEMP_CANDIDATES, path_contains="Temperature")
            if gpu_temp is not None:
                stats["gpu"]["temp"] = round(gpu_temp, 1)

        # LibreHardwareMonitor sees every case fan, so it wins when present.
        fans = find_fans(rows)
        if fans:
            stats["fans"] = fans

        if stats["gpu"]["load"] is None:
            gpu_load = find_sensor(rows, GPU_LOAD_CANDIDATES, path_contains="Load")
            if gpu_load is not None:
                stats["gpu"]["load"] = round(gpu_load, 1)

    @property
    def lhm_available(self) -> bool:
        return time.monotonic() >= self._lhm_down_until
