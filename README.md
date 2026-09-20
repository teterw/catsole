# desk-console

A USB-tethered desk display. An Arduino UNO R4 WiFi drives a 128×64 OLED and
reads NFC taps; a Python service on the PC feeds it either the current synced
lyric line or live hardware stats. Tap a tag to change mode, hold it to force
a refresh.

No WiFi is used. The board supports it; this project does not. Everything
goes over USB serial.

## Parts

| Part | Bus | Notes |
|---|---|---|
| Arduino UNO R4 WiFi | USB CDC | permanently tethered to the PC |
| SSD1309 128×64 OLED | hardware SPI | monochrome |
| Elechouse PN532 NFC/RFID | I2C | onboard switch set to **I2C mode**, 4-pin header |

No buzzer, no other sensors.

## Wiring

| OLED pin | Arduino pin |
|---|---|
| CS | 10 |
| DC | 9 |
| RES | 8 |
| SCK | 13 (hardware SPI) |
| MOSI / SDA | 11 (hardware SPI) |
| VCC | 3.3V or 5V, per your module |
| GND | GND |

| PN532 pin | Arduino pin |
|---|---|
| VCC | 5V |
| GND | GND |
| SDA | SDA |
| SCL | SCL |

The PN532's DIP switches must be set to I2C. The 4-pin header has the pull-up
resistors on board, so no external ones are needed.

## Firmware setup

Three libraries. Two come from Library Manager:

- **U8g2** (tested against 2.35.30)
- **ArduinoJson** (tested against 7.4.2 — the v7 API, not v6)

The third does not. The Elechouse PN532 library is not in Library Manager and
has to be installed by hand, as **two** folders:

```bash
git clone --depth 1 https://github.com/elechouse/PN532.git
# copy both of these into your sketchbook libraries folder,
# e.g. C:\Users\<you>\Documents\Arduino\libraries\
#   PN532\
#   PN532_I2C\
```

Then open `arduino/desk_console/desk_console.ino` and upload, selecting
**Arduino UNO R4 WiFi** as the board.

### If the display looks wrong

SSD1309 panels ship with two common init sequences, and the module does not
report which one it wants. The sketch has both behind one define at the top:

```c
#define DISPLAY_VARIANT 0   // try 1 if the display looks wrong
```

On boot the sketch runs a self-test: a full-white flash, then a border with a
checkerboard fill, then an identity card. With the right variant the border is
crisp against the panel edge, the checkerboard reads as an even grey rather
than as bands, and the white fill is uniform. If it looks washed out, shifted
by a few columns, or inverted, change the define to `1` and re-upload.

A **completely blank** screen is not this setting — check wiring and the RES
pin first.

The identity card also reports whether the PN532 answered, which is the
quickest way to tell an I2C wiring problem from a library problem.

## PC setup

```bash
cd server
python -m pip install -r requirements.txt
python run.py
```

Then open <http://127.0.0.1:8730> for the control panel.

Useful flags:

| Flag | Effect |
|---|---|
| `--no-serial` | run the whole pipeline with no board attached |
| `--once` | print one frame and exit |
| `--port COM5` | pin a serial port instead of autodetecting |
| `--offset-ms 250` | shift lyric timing (positive = later) |
| `--no-web` | skip the control panel |
| `-v` | debug logging |

The port is normally found by USB VID/PID (`2341:1002`), so it survives
Windows renumbering the COM port.

### Note on `winsdk`

Most SMTC examples online use the `winsdk` package. It has no wheel for
Python 3.13 and falls back to a source build that needs Visual Studio. This
project uses the `winrt-*` split packages instead, which are its maintained
successor and ship 3.13 wheels. Import paths are `winrt.windows.media.control`
rather than `winsdk.windows.media.control`.

## Modes

**Lyrics** — artist and title in the top strip, marqueed when too long for the
panel, with the current synced lyric line below and a cosmetic equalizer along
the bottom. There is no microphone on this build, so the bars are animated
rather than audio-reactive.

Lyrics come from [lrclib.net](https://lrclib.net), which needs no API key.
When no synced lyrics exist the display falls back to a title card rather than
showing an untimed line in a position it cannot justify. Responses are cached
under `server/cache/`, which is gitignored.

**Stats** — CPU clock, temperature and load; GPU temperature, load and VRAM.
Any reading that is unavailable shows as `--` for that field alone; one dead
sensor never blanks the mode.

## Tap behaviour

| Gesture | Action |
|---|---|
| Short tap | next mode |
| Hold ~1.5s | force an immediate refresh |

Any tag works — the UID is reported to the PC and shown in the control panel,
but nothing branches on it. While a tag is held, a bar fills across the top of
the display and the action fires the moment it completes.

## When the PC goes away

There is no RTC and no network, so a disconnected device cannot know the time
and will not pretend to. If no frame arrives for four seconds, the display
dims the last known frame to a checkerboard and blinks a `no link` badge with
a seconds-stale counter. The content is still true, just old — a blank screen
would read as a dead device.

Before the PC has ever been heard from, the display shows a waiting state with
a slow sweep, so an idle device never looks like a crashed one.

## Hardware stats

Stats work out of the box with no extra software: GPU figures come from
`nvidia-smi` (installed with the NVIDIA driver) and CPU load and clock from
`psutil`.

**CPU temperature is the exception.** Reading a Ryzen package temperature
needs a ring0 driver, which means
[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor)
running elevated with its web server on. In LHM: Options → Remote Web Server →
Run, leaving the port at 8085. Without it everything else still works and
CPU temp shows `--`.

To have it available after every boot, give LHM its own scheduled task with
*Run with highest privileges* — it needs admin, which is why this project does
not bundle it.

## Starting automatically

```powershell
cd server
powershell -ExecutionPolicy Bypass -File autostart.ps1 -Install
```

This registers a Scheduled Task that runs at logon, 20 seconds in, launched
with `pythonw.exe` so no console window hangs around. `-Status` shows whether
it is installed and tails the log; `-Uninstall` removes it.

It is deliberately **not** a Windows service. Services run in session 0, and
the media transport controls are per-user-session, so a service would see no
media session and lyrics mode would be permanently blank.

Because `pythonw` has no console, logs go to
`%LOCALAPPDATA%\desk-console\desk-console.log` (rotating, 4 files).

## Protocol

Newline-delimited JSON in both directions at 115200 baud. Unparseable lines
are dropped rather than resynchronised — a dropped frame is invisible at 4Hz,
a desynchronised parser is not.

PC to device:

```json
{"t":"frame","mode":"lyrics","meta":"Artist - Title","main":"current line","hold_ms":3200,"eq":1,"state":"playing","lyr":"synced"}
{"t":"frame","mode":"stats","cpu":{"temp":61,"load":34,"clock":4850},"gpu":{"temp":68,"load":99,"vram_used":4211,"vram_total":8188}}
```

Device to PC:

```json
{"t":"hello","fw":"1.0.0","variant":0}
{"t":"tap","uid":"04A2B3C4","kind":"short"}
{"t":"tap","uid":"04A2B3C4","kind":"hold"}
```

`hold_ms` tells the device how long the current lyric line stays valid, so it
can draw a progress hairline and show when a frame is late instead of sitting
on a stale line. All device-bound text is folded to ASCII on the PC, because
the OLED fonts do not carry the full Unicode range.

## Troubleshooting

**Nothing on the display at all.** Check RES on pin 8 and that the panel has
power. This is not the `DISPLAY_VARIANT` setting — a wrong variant produces a
poor image, not no image.

**Display works, `pn532 not found` on the boot card.** The DIP switches are
not in I2C mode, or SDA/SCL are swapped.

**Lyrics mode shows the title instead of lyrics.** No synced lyrics exist for
that track on lrclib, or the track's reported duration is too far from any
match. The control panel shows which of those it is.

**Lyrics run early or late.** Set `lyric_offset_ms` in `server/config.json`,
or pass `--offset-ms`. Positive pushes later. Different players report
position with different lag.

**Nothing playing, but something is.** Not every app reports to SMTC. Check
whether Windows itself shows it in the volume flyout's media control.

**CPU temp is `--`.** LibreHardwareMonitor is not running elevated with its
web server on. See above.

**Control panel is empty but the service is running.** The panel polls
`/api/state`; if the service died, the preview dims exactly as the device
does. Check the log file.

## Layout

```
arduino/desk_console/   firmware
server/                 PC service, tests, autostart script
docs/superpowers/       design spec and implementation plan
```

Tests: `cd server && python -m pytest`. They cover the parsing and
state-machine seams and need neither the board nor the network.
