# desk-console

A USB-tethered desk display. An Arduino UNO R4 WiFi drives a 128×64 OLED, and
a Python service on the PC feeds it either the current synced lyric line or
live hardware stats. Modes are switched from a small local web page.

No WiFi is used. The board supports it; this project does not. Everything
goes over USB serial.

## Parts

| Part | Bus | Notes |
|---|---|---|
| Arduino UNO R4 WiFi | USB CDC | permanently tethered to the PC |
| SSD1309 128×64 OLED | hardware SPI | monochrome |

That's the whole parts list. No buzzer, no sensors, no reader.

> An NFC tag reader was part of the original design — tapping a card would
> cycle modes. It was removed after the module turned out not to work: it
> answered on the I²C bus at an address no PN532 uses (0x40 rather than
> 0x24), accepted commands without ever replying, and never responded over
> UART in either wiring orientation. Mode switching lives in the web panel
> instead. The reader code is recoverable from git history if the hardware
> is ever replaced.

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

## Firmware setup

Two libraries, both from Library Manager:

- **U8g2** (tested against 2.35.30)
- **ArduinoJson** (tested against 7.4.2 — the v7 API, not v6)

Open `arduino/desk_console/desk_console.ino` and upload, selecting **Arduino
UNO R4 WiFi** as the board.

### SPI bus speed

The sketch clocks the display at **1MHz**, well below the R4's default:

```c
static const uint32_t DISPLAY_BUS_HZ = 1000000;
```

This is not arbitrary. At the default speed on this build, initialisation
commands arrived corrupted — the panel came up inverted about half the time —
and the display dropped its state a few seconds after each init while the
microcontroller carried on fine. 1MHz fixed it completely, and a 1KB frame
buffer at 30fps only needs around 250kbit/s, so nothing is lost.

If your wiring is short and tidy, 2MHz and then 4MHz are worth trying. The
symptoms of running too fast are an upside-down image, or a panel that blanks
and needs a reset.

### If the display looks wrong

SSD1309 panels ship with two common init sequences and the module does not
report which one it wants. Both are in the sketch behind one define:

```c
#define DISPLAY_VARIANT 0   // try 1 if the display looks wrong
```

On this build, variant **0** is correct. On boot the sketch runs a self-test —
a full-white flash, then a border with a checkerboard fill, then an identity
card. With the right variant the border is crisp against the panel edge, the
checkerboard reads as an even grey rather than as bands, and the white fill is
uniform. If it looks washed out, shifted by a few columns, or inverted, change
the define to `1` and re-upload.

A **completely blank** screen is not this setting — check wiring and the RES
pin first.

`arduino/oled_probe/` is a throwaway diagnostic that cycles through candidate
controller profiles and SPI bus speeds with a heartbeat counter, driven by
single-character serial commands. It is how the two settings above were
determined, and it is kept for the next time a panel misbehaves.

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
rather than audio-reactive. Long lines wrap and step down through three font
sizes so they fit rather than being cut off.

Lyrics come from [lrclib.net](https://lrclib.net), which needs no API key.
When no synced lyrics exist the display falls back to a title card rather than
showing an untimed line in a position it cannot justify. Responses are cached
under `server/cache/`, which is gitignored.

**Stats** — three labelled rows with inline usage bars: CPU clock,
temperature and load; GPU temperature, load and VRAM; and system RAM used
against total. Any reading that is unavailable shows as `--` for that field
alone; one dead sensor never blanks the mode.

Switch between them with the buttons on the control panel. `refresh now`
forces an immediate re-poll rather than waiting for the next interval.

## When the PC goes away

There is no RTC and no network, so a disconnected device cannot know the time
and will not pretend to. If no frame arrives for four seconds, the display
dims the last known frame to a checkerboard and blinks a `no link` badge with
a seconds-stale counter. The content is still true, just old — a blank screen
would read as a dead device.

Before the PC has ever been heard from, the display shows a waiting state with
a slow sweep, so an idle device never looks like a crashed one.

After **three minutes** with no frames, the panel switches off entirely and
the board announces `{"t":"display","asleep":true}`. It wakes on the next
frame, instantly.

This matters because many motherboards keep supplying power to USB in
soft-off, so the board can stay running all night after the PC shuts down.
The microcontroller does not mind that at all, but an OLED does: brightness
decays with hours lit, and static content — the stats labels, the `no link`
badge — burns in permanently. Sleeping costs nothing and protects the one
part that actually wears out.

Change `SLEEP_AFTER_MS` in the sketch to adjust the delay. If you would
rather the port cut power at shutdown instead, that is a BIOS setting: on
Gigabyte boards, *Settings → Platform Power → ErP* and *USB Power Delivery
in Soft-Off State (S5)*. Note ErP also disables Wake-on-LAN and USB wake.

## Hardware stats

Stats work out of the box with no extra software: GPU figures come from
`nvidia-smi` (installed with the NVIDIA driver), and CPU load and clock plus
system RAM from `psutil`.

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

Newline-delimited JSON at 115200 baud. Unparseable lines are dropped rather
than resynchronised — a dropped frame is invisible at 4Hz, a desynchronised
parser is not.

PC to device:

```json
{"t":"frame","mode":"lyrics","meta":"Artist - Title","main":"current line","hold_ms":3200,"eq":1,"state":"playing","lyr":"synced"}
{"t":"frame","mode":"stats","cpu":{"temp":61,"load":34,"clock":4850},"gpu":{"temp":68,"load":99,"vram_used":4211,"vram_total":8188},"ram":{"used":12680,"total":32690,"percent":38.8}}
```

Device to PC — just the one message, sent at boot and repeated until the PC
answers:

```json
{"t":"hello","fw":"1.0.0","variant":0}
```

`hold_ms` tells the device how long the current lyric line stays valid, so it
can draw a progress hairline and show when a frame is late instead of sitting
on a stale line. All device-bound text is folded to ASCII on the PC, because
the OLED fonts do not carry the full Unicode range.

## Troubleshooting

**Nothing on the display at all.** Check RES on pin 8 and that the panel has
power. This is not the `DISPLAY_VARIANT` setting — a wrong variant produces a
poor image, not no image.

**The image is upside down, or the panel blanks after a few seconds.** The SPI
bus is running faster than the wiring can carry. Lower `DISPLAY_BUS_HZ`.

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

**Upload fails with "serial port busy".** The service is holding the port.
Stop it before flashing.

## Layout

```
arduino/desk_console/   firmware
arduino/oled_probe/     display diagnostic sketch
server/                 PC service, tests, autostart script
docs/superpowers/       design spec and implementation plan
```

Tests: `cd server && python -m pytest`. They cover the parsing and
frame-shaping seams and need neither the board nor the network.
