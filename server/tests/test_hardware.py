"""Tests for hardware sensor parsing and per-field degradation."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from catsole.hardware import (
    HardwareReader,
    find_fans,
    empty_stats,
    find_sensor,
    flatten_lhm,
    parse_number,
    parse_nvidia_smi,
)

TREE = {
    "Text": "Root",
    "Children": [
        {
            "Text": "AMD Ryzen 5 7500F",
            "Children": [
                {
                    "Text": "Temperatures",
                    "Children": [
                        {"Text": "Core (Tctl/Tdie)", "Value": "61.4 °C", "Children": []}
                    ],
                },
                {
                    "Text": "Clocks",
                    "Children": [
                        {"Text": "Core #1", "Value": "4850.0 MHz", "Children": []}
                    ],
                },
                {
                    "Text": "Load",
                    "Children": [
                        {"Text": "CPU Total", "Value": "12.5 %", "Children": []}
                    ],
                },
            ],
        }
    ],
}


def test_flatten_lhm_walks_tree():
    rows = flatten_lhm(TREE)
    assert (
        "Root/AMD Ryzen 5 7500F/Temperatures",
        "Core (Tctl/Tdie)",
        "61.4 °C",
    ) in rows


def test_flatten_lhm_finds_every_leaf():
    sensors = {sensor for _, sensor, _ in flatten_lhm(TREE)}
    assert sensors == {"Core (Tctl/Tdie)", "Core #1", "CPU Total"}


def test_flatten_lhm_handles_empty_tree():
    assert flatten_lhm({}) == []


def test_find_sensor_matches_first_candidate_in_order():
    value = find_sensor(flatten_lhm(TREE), ["CPU Package", "Tctl/Tdie"])
    assert value == 61.4


def test_find_sensor_returns_none_when_absent():
    assert find_sensor(flatten_lhm(TREE), ["GPU Hot Spot"]) is None


def test_parse_number_strips_units():
    assert parse_number("61.4 °C") == 61.4
    assert parse_number("4850.0 MHz") == 4850.0
    assert parse_number("12.5 %") == 12.5


def test_parse_number_handles_comma_decimal_separator():
    # LibreHardwareMonitor formats for the system locale.
    assert parse_number("61,4 °C") == 61.4


def test_parse_number_returns_none_on_junk():
    assert parse_number("") is None
    assert parse_number("N/A") is None
    assert parse_number(None) is None


def test_parse_nvidia_smi_reads_all_fields():
    assert parse_nvidia_smi("68, 99, 4211, 8188, 42") == {
        "temp": 68.0,
        "load": 99.0,
        "vram_used": 4211.0,
        "vram_total": 8188.0,
        "fan": 42.0,
    }


def test_parse_nvidia_smi_returns_empty_on_garbage():
    assert parse_nvidia_smi("N/A") == {}
    assert parse_nvidia_smi("") == {}


def test_parse_nvidia_smi_keeps_readable_fields_when_one_is_unsupported():
    # Laptop and some desktop GPUs report [N/A] for individual fields.
    got = parse_nvidia_smi("68, [N/A], 4211, 8188, [N/A]")
    assert got["temp"] == 68.0
    assert got["vram_total"] == 8188.0
    assert "load" not in got
    assert "fan" not in got


def test_parse_nvidia_smi_keeps_a_stopped_fan():
    # Zero is a real reading: modern cards stop the fan when idle.
    assert parse_nvidia_smi("48, 4, 1458, 16380, 0")["fan"] == 0.0


def test_empty_stats_has_all_sections_with_none_fields():
    stats = empty_stats()
    assert set(stats) == {"cpu", "gpu", "ram", "fans"}
    assert stats["fans"] == []
    assert stats["cpu"]["temp"] is None
    assert stats["gpu"]["vram_used"] is None
    assert stats["ram"]["used"] is None
    assert stats["ram"]["total"] is None
    assert stats["ram"]["percent"] is None


def test_poll_fills_ram_from_psutil():
    # RAM needs no external tool, so this is safe to assert against the
    # real machine: it is always available when psutil imports.
    stats = HardwareReader().poll()
    ram = stats["ram"]
    assert ram["total"] > 0
    assert 0 <= ram["used"] <= ram["total"]
    assert 0.0 <= ram["percent"] <= 100.0


def test_ram_is_reported_in_megabytes():
    # Same unit as VRAM, so the device formats both the same way.
    ram = HardwareReader().poll()["ram"]
    # Any real machine has between 1GB and 1TB of RAM.
    assert 1024 <= ram["total"] <= 1024 * 1024


FAN_TREE = {
    "Text": "Root",
    "Children": [
        {
            "Text": "Motherboard",
            "Children": [
                {
                    "Text": "Fans",
                    "Children": [
                        {"Text": "CPU Fan", "Value": "1240 RPM", "Children": []},
                        {"Text": "Chassis Fan #2", "Value": "0 RPM", "Children": []},
                        {"Text": "Voltage", "Value": "1.2 V", "Children": []},
                    ],
                }
            ],
        }
    ],
}


def test_find_fans_matches_on_rpm_unit():
    fans = find_fans(flatten_lhm(FAN_TREE))
    names = [f["name"] for f in fans]
    assert "CPU Fan" in names
    # Matched by unit, so a voltage sitting in the same group is skipped.
    assert "Voltage" not in names


def test_find_fans_keeps_stopped_fans():
    # Zero RPM is a real reading, not a missing sensor.
    fans = find_fans(flatten_lhm(FAN_TREE))
    stopped = [f for f in fans if f["rpm"] == 0]
    assert stopped and stopped[0]["name"].startswith("Chassis")


def test_find_fans_respects_the_limit():
    assert len(find_fans(flatten_lhm(FAN_TREE), limit=1)) == 1


def test_find_fans_on_empty_tree():
    assert find_fans([]) == []
