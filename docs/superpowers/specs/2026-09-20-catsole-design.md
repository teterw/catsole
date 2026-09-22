# catsole — Design

**Date:** 2026-09-20
**Status:** Built and running. Amended 2026-09-21: the NFC reader was
removed from the project (see *Amendment* at the end).

A USB-tethered desk display: an Arduino UNO R4 WiFi drives an SSD1309 OLED
while a Python process on the Windows PC feeds it now-playing lyrics or
live hardware stats over Serial.

## Hardware (fixed — not up for revision)

| Part | Bus | Pins |
|---|---|---|
| Arduino UNO R4 WiFi | USB CDC to PC | permanently tethered, COM3 |
| SSD1309 128x64 OLED | Hardware SPI | CS=10, DC=9, RES=8, SCK=13, MOSI=11 |

No buzzer, no other sensors, and as built no NFC reader. **No WiFi is
used anywhere in this project**
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
  SSD1309 -SPI-- UNO R4 --USB CDC (NDJSON)-- Python --+-- winrt (SMTC)
                                                      +-- lrclib.net HTTP
                                                      +-- LHM :8085 / nvidia-smi / psutil
                                                      +-- Flask 127.0.0.1:8730
```

### Division of responsibility

The Arduino is a **renderer, not a decision-maker**. It owns animation
(marquee, equalizer) and link-state detection. It owns no content logic:
it does not parse LRC files, has no concept of a track, and does not
decide what a mode means.

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
    {"t":"frame","mode":"stats","cpu":{"load":34,"clock":4850,"temp":61},"gpu":{"temp":68,"load":99,"vram_used":4211,"vram_total":8188},"ram":{"used":12680,"total":32690,"percent":38.8}}
    {"t":"ping"}

**Device to PC**

    {"t":"hello","fw":"1.0.0","variant":0}

All device-bound text is ASCII-folded on the PC (NFKD decomposition plus
smart-quote and dash substitution) because the OLED font set does not
carry the full Unicode range.

## Modes

1. **Lyrics** — meta strip with marqueed artist/title, current synced
   lyric line in the main band, cosmetic 12-bar equalizer along the
   bottom. Falls back through: synced LRC, then plain lyrics (static, no
   timing), then title/artist only.
2. **Stats** — CPU clock/temp/usage, GPU temp/usage/VRAM, and system RAM
   used against total, as three labelled rows with inline fill bars on a
   17px pitch, refreshed every ~1s. Fields that are unavailable render as
   `--` individually rather than blanking the whole mode. RAM comes from
   psutil and is reported in MB to match VRAM, so the device carries one
   unit convention rather than two.

Modes are switched from the control panel, which also exposes an
immediate re-poll. The device itself has no input.

## Display states

| State | Trigger | Treatment |
|---|---|---|
| BOOT | power-on | self-test pattern, then identity card |
| WAITING | no frame ever received | slow pulsing prompt |
| LIVE | frame within 4s | normal mode rendering |
| STALE | more than 4s since last frame | last frame XOR-dithered to a true dim, blinking `NO LINK` badge with seconds-stale counter |
| IDLE | LIVE but nothing playing | equalizer decays to a flat line |
| SLEEP | more than 3min since last frame | panel powered off entirely; wakes on the next frame |

The disconnected state must read as deliberate, not as a frozen or blank
screen. This is an explicit design requirement, not a nicety.

Sleep is the exception, and it is about hardware rather than appearance.
USB ports on many motherboards keep supplying power in soft-off, so the
board can run all night after the PC shuts down. The MCU is indifferent to
that; the OLED is not, since brightness decays with hours lit and static
content burns in. After three minutes without frames the panel is powered
off and the state is announced on the link, so the PC can observe it.

## PC-side modules

One entry point, `server/run.py`, over focused modules:

| Module | Responsibility | Depends on |
|---|---|---|
| `media.py` | SMTC session, metadata, extrapolated position | winrt |
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
up first. Logs rotate into `%LOCALAPPDATA%\catsole\`. LHM's own
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

- **SMTC app variance.** Not every player reports position, or reports it
  honestly. The fallback chain degrades to a static title/artist card
  rather than showing a wrong line.

## Amendment, 2026-09-21

The NFC reader was removed from the project after it could not be made to
work. What the hardware actually did:

- Nothing ever answered on I2C at 0x24, the only address a PN532 uses and
  the only one the Elechouse library addresses.
- A device did answer consistently at **0x40**, and it was demonstrably the
  module: both bus lines idled high while it was connected and went low the
  moment it was unplugged, so its pull-ups were present and it was powered.
- That device accepted a full nine-byte PN532 `GetFirmwareVersion` frame
  without complaint and never replied to it.
- Over UART on `Serial1`, it was not found in either wiring orientation.

A part that acknowledges its address, accepts commands, answers at an
address its chip cannot use, and never replies is not the device the design
assumed. Rather than keep a mode the hardware could not support, taps were
dropped and mode switching moved entirely to the control panel.

Consequences for this design:

- The parts list is the board and the OLED.
- The device is display-only. The protocol is now one-way in practice: the
  only thing it sends is its boot `hello`.
- The long-hold "force refresh" gesture is gone; the panel's refresh button
  covers the same need.

The reader code is in git history if the hardware is ever replaced.

Two hardware findings from the build are worth keeping:

- **The display SPI bus must be clocked slowly** (1MHz). At the R4's default
  speed, init commands arrived corrupted, so the panel came up inverted
  about half the time and dropped its state seconds after each init while
  the MCU ran on unaffected.
- **`DISPLAY_VARIANT 0` (SSD1309 NONAME0) is correct** for this panel.
  NONAME2 and SH1106 both wrap columns on it; SSD1306 flickers.
