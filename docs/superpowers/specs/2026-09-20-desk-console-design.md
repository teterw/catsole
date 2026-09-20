# desk-console — Design

**Date:** 2026-09-20
**Status:** Approved, implementing

A USB-tethered desk display: an Arduino UNO R4 WiFi drives an SSD1309 OLED
and reads NFC taps, while a Python process on the Windows PC feeds it
now-playing lyrics or live hardware stats over Serial.

## Hardware (fixed — not up for revision)

| Part | Bus | Pins |
|---|---|---|
| Arduino UNO R4 WiFi | USB CDC to PC | permanently tethered, COM3 |
| SSD1309 128x64 OLED | Hardware SPI | CS=10, DC=9, RES=8, SCK=13, MOSI=11 |
| Elechouse PN532 | I2C (onboard switch in I2C mode) | VCC/GND/SDA/SCL 4-pin header |

No buzzer, no other sensors. **No WiFi is used anywhere in this project**
despite the board supporting it — all PC communication is Serial over USB.

Board confirmed present at `USB\VID_2341&PID_1002`, serial `F0F5BD560764`,
CDC interface on COM3.

## Constraints

- No RTC and no WiFi, so the device cannot know the time on its own. Any
  clock-like display is only possible while the PC is feeding it.
- SMTC (System Media Transport Controls) is per-user-session, which rules
  out running the PC side as a Windows service. It must run in the
  interactive logon session.
- CPU package temperature on a Ryzen 5 7500F requires a ring0 driver, i.e.
  LibreHardwareMonitor running elevated. Everything else is obtainable
  without admin.

## Architecture

```
  PN532 --I2C--+
               +-- UNO R4 --USB CDC (NDJSON)-- Python --+-- winsdk (SMTC)
  SSD1309 -SPI-+                                        +-- lrclib.net HTTP
                                                        +-- LHM :8085 / nvidia-smi / psutil
                                                        +-- Flask 127.0.0.1:8730
```

### Division of responsibility

The Arduino is a **renderer, not a decision-maker**. It owns animation
(marquee, equalizer, hold ring), link-state detection, and tap
classification. It owns no content logic: it does not parse LRC files,
has no concept of a track, and does not decide what a mode means.

The PC owns all content and timing. This keeps the fiddly parts — lyric
sync math, API fallbacks, sensor degradation — in Python where they are
unit-testable, and means changing layout copy never requires a reflash.

### Lyric timing

The PC resolves which lyric line is current and sends that line plus a
`hold_ms` indicating how long it remains valid. The Arduino draws a thin
progress hairline over that window. If the next frame is late, the
hairline completing is a visible cue rather than a silently stale line.

Position must be extrapolated, because Windows only pushes timeline
updates sporadically (Spotify in particular):

    position = timeline.position + (now_utc - timeline.last_updated_time) * playback_rate

clamped to the track end, and frozen when playback status is not playing.
A configurable `offset_ms` handles per-app latency calibration.

## Serial protocol

Newline-delimited JSON, 115200 baud (nominal — USB CDC ignores it).
Unparseable lines are dropped by both sides rather than resynchronised.

**PC to device**

    {"t":"frame","mode":"lyrics","meta":"Artist - Title","main":"current line","hold_ms":3200,"eq":1,"state":"playing"}
    {"t":"frame","mode":"stats","cpu":{"load":34,"clock":4850,"temp":61},"gpu":{"temp":68,"load":99,"vram":[4211,8188]}}
    {"t":"ping"}

**Device to PC**

    {"t":"hello","fw":"1.0.0"}
    {"t":"tap","uid":"04A2B3C4","kind":"short"}
    {"t":"tap","uid":"04A2B3C4","kind":"hold"}

All device-bound text is ASCII-folded on the PC (NFKD decomposition plus
smart-quote and dash substitution) because the OLED font set does not
carry the full Unicode range.

## Modes

1. **Lyrics** — meta strip with marqueed artist/title, current synced
   lyric line in the main band, cosmetic 12-bar equalizer along the
   bottom. Falls back through: synced LRC, then plain lyrics (static, no
   timing), then title/artist only.
2. **Stats** — CPU clock/temp/usage and GPU temp/usage/VRAM as two
   labelled rows with inline fill bars, refreshed every ~1s. Fields that
   are unavailable render as `--` individually rather than blanking the
   whole mode.

Short tap cycles modes. Long hold (1.5s or more) forces an immediate
re-poll of both media and hardware sources. Tag UID is reported and
logged but does not alter behaviour; any tag acts as a generic tap.

## Display states

| State | Trigger | Treatment |
|---|---|---|
| BOOT | power-on | self-test pattern, then identity card |
| WAITING | no frame ever received | slow pulsing prompt |
| LIVE | frame within 4s | normal mode rendering |
| STALE | more than 4s since last frame | last frame XOR-dithered to a true dim, blinking `NO LINK` badge with seconds-stale counter |
| IDLE | LIVE but nothing playing | equalizer decays to a flat line |

The disconnected state must read as deliberate, not as a frozen or blank
screen. This is an explicit design requirement, not a nicety.

## PC-side modules

One entry point, `server/run.py`, over focused modules:

| Module | Responsibility | Depends on |
|---|---|---|
| `media.py` | SMTC session, metadata, extrapolated position | winsdk |
| `lyrics.py` | lrclib fetch, LRC parse, disk cache, fallback chain | requests |
| `hardware.py` | LHM, then nvidia-smi, then psutil; per-field degradation | requests, psutil |
| `link.py` | serial transport, VID/PID autodetect, reconnect | pyserial |
| `protocol.py` | frame encode/decode, ASCII folding | stdlib |
| `web.py` | dark/mono control panel | flask |
| `app.py` | mode state machine, poll loop, wiring | all of the above |

`lyrics.py` sends a descriptive User-Agent as lrclib requests, and caches
responses to disk so repeated plays do not refetch. Cached lyrics are
gitignored; no lyric content is committed to the repository.

## Hardware stats sourcing

Try in order, per field, degrading individually:

1. LibreHardwareMonitor web API at `http://localhost:8085/data.json` —
   the only source for Ryzen CPU package temperature. Optional.
2. `nvidia-smi --query-gpu=...` — GPU temp, usage, VRAM. Needs no install.
3. `psutil` — CPU load and clock.

Stats mode is fully functional without LHM; only CPU temperature is lost.

## Autostart

A logon-triggered Scheduled Task running as the user, registered by
`server/autostart.ps1` (`-Install` / `-Uninstall` / `-Status`), which the
user runs themselves. It launches `pythonw.exe run.py` so no console
window persists, with a ~20s post-logon delay so the CDC port and LHM are
up first. Logs rotate into `%LOCALAPPDATA%\desk-console\`. LHM's own
elevated autostart is documented in the README as an optional step, not
bundled.

## Verification

- The sketch is compiled with `arduino-cli` against the Renesas UNO core.
  Written-but-never-compiled firmware does not count as delivered.
- pytest covers the testable seams: LRC parsing, position extrapolation
  and line selection, ASCII folding, protocol round-trip, LHM tree
  walking, and stats degradation. Test fixtures use invented placeholder
  text, never real lyrics.
- `run.py --no-serial` exercises the real SMTC, lrclib and nvidia-smi
  paths on this machine with no board attached, so the data pipeline is
  demonstrable before hardware is involved.

## Open risks

- **U8g2 constructor.** Whether this panel wants the `NONAME0` or
  `NONAME2` init sequence cannot be determined without running it. Both
  constructors ship in the sketch behind a one-line `DISPLAY_VARIANT`
  define, and a boot self-test pattern makes the correct choice obvious
  on first upload. A wrong choice looks washed out or column-shifted,
  not blank.
- **PN532 library.** The Elechouse library is not in the Library Manager
  and installs as two folders manually. `SAMConfig()` after `begin()` is
  mandatory or reads fail silently. `readPassiveTargetID` blocks for its
  full timeout, so it is polled at ~50ms to keep animation alive.
- **SMTC app variance.** Not every player reports position, or reports it
  honestly. The fallback chain degrades to a static title/artist card
  rather than showing a wrong line.
