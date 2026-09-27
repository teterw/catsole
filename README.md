# catsole

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

Open `arduino/catsole/catsole.ino` and upload, selecting **Arduino
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

## Screens

Three, cycled from the control panel or automatically while idle.

**Lyrics** — artist and title marqueed along the top, the current synced
lyric line below, and a full-width equalizer that follows the actual audio.
Long lines wrap and step down through three font sizes rather than being cut
off, and a new line slides in as the old one slides out. The mascot perches
at the right, bobbing on the beat.

Thai lyrics are shown in Thai, not swapped for the title card. See
[Thai lyrics](#thai-lyrics) below.

**Stats** — CPU clock, temperature and load; GPU temperature, load and VRAM;
and system RAM, as three rows with usage bars. Fan speed sits in the header
with half a fan disc spinning in from the right edge, its rate following the
reading. Unavailable values show `--` for that field alone.

**Clock** — the time and date, sent from the PC as finished strings since the
board has no RTC of its own, alongside the board's own state: how hard the
microcontroller is working, its uptime, and its frame rate.

The screen jumps to stats on its own when the machine starts working hard —
above 80% on either CPU or GPU — and holds there until load falls back under
55%. Two thresholds rather than one, so load hovering near the line cannot
flap the display.

### The mascot

An original ASCII cat with five poses. It blinks on a deliberately uneven
rhythm so it does not read as a loop, perks up when a track starts, and curls
up when the PC is away.

It appears on every screen. On the lyrics screen it stands *in front* of the
equalizer rather than beside it: the bars are decoration, so occluding their
right end costs nothing, where taking layout space from the lyric would cost
the screen its point. On stats the bars stop short to give it a column, since
four rows of readings are not decoration. On the beat screen it is the
subject, and on the clock it keeps the time company.

The boot sequence is a white flash, the cat rising from below and easing into
place, two uneven blinks, then the name typing in beside it.

### Audio-reactive equalizer

The PC captures its own speaker output through WASAPI loopback, runs an FFT,
and sends sixteen logarithmic bands as a hex string at 20Hz. The device
interpolates those across 43 bars.

Three things make it look like music rather than water: a narrow 38dB window
so the range maps across the full bar height, a fast release so bars fall
between beats, and a per-band tilt to offset music's roughly 3dB-per-octave
rolloff. Automatic gain keeps quiet tracks filling the display.

Optional. Without `numpy` and `pyaudiowpatch`, or if nothing arrives for
600ms, the device falls back to a synthetic travelling wave.

### Bobbing on the beat

Onsets come from positive spectral flux in the lower bands, and the gaps
between them give a tempo. Once eight consecutive gaps agree closely the
tempo is taken as settled and stops being re-estimated: tracking it forever
means every vocal transient gets a vote, and a steady song would slowly drag
its own tempo off. After that the beat grid only creeps toward onsets rather
than following them, and a track change clears it.

The phase is what goes down the wire, not the beats themselves, so a missed
onset does not stall the animation — it keeps moving on the grid and
resynchronises when the next one lands. The phase is advanced by
`beat_lead_ms` to cancel the pipeline's own latency: a 1024-sample buffer at
48kHz is 21ms before analysis even starts.

### Thai lyrics

The firmware carries U8g2's ETL Thai faces (16px and 14px) for the lyric
band. A line with any Thai in it gets these; a line without keeps the Latin
faces, so English songs look exactly as before. Mixed lines are fine, since
the Thai faces include ASCII.

The ETL faces are typewriter fonts: vowels and tone marks each have a full
cell's width and are drawn to land on the letter before them. The firmware
draws each mark at the previous letter's position without advancing, as a
Thai typewriter would have, so marks stack on their letters rather than
sitting in cells of their own.

Thai puts no spaces between words, so wrapping cannot wait for one. A phrase
that does not fit is split inside itself, preferring a place that is
certainly a syllable edge: before เ แ โ ใ ไ, or after ะ า ำ ๆ. A break never
separates a letter from its marks. Without a dictionary a break can still
land mid-word now and then, but it never tears a character apart.

Thai needs 17px a line even at 14px, since marks stack above and below, so
the band holds two lines. A longer line pages through two rows at a time,
splitting the line's own duration (`hold_ms`) between the pages. The split is
even, which only roughly keeps pace with the singing - see
[Known issues](#known-issues). Title cards have no duration and cycle instead.

Only the main line gets Thai. The artist and title strip along the top is
7px tall, too short for any Thai face, so Thai there is dropped as it always
was. A Thai song with no synced lyrics still shows its Thai title, though,
because the title card puts the title in the main band.

### Idling

With nothing playing the screens rotate every nine seconds, so the device has
a life of its own. A hand-picked mode holds for 90 seconds before rotation
resumes, and the timer resets while music plays so rotation starts a full
interval after playback stops.

### What counts as music

Windows reports the *application*, not the site, so a YouTube tab and an
Instagram tab in the same browser are indistinguishable by app id. Filtering
is on shape instead: anything shorter than 60 seconds is treated as a story or
a reel and ignored. A missing duration is not treated as a failure, since live
streams routinely report none.

Tune `min_duration_s`, `allow_apps`, `block_apps` and `require_artist` in
`server/config.json`.

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

Fan speed comes from the GPU through `nvidia-smi`, reported as a percentage.
Zero is a real reading: modern cards stop the fan entirely when cool. If
LibreHardwareMonitor is running it takes priority, since it sees every case
fan rather than just the graphics card.

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
`%LOCALAPPDATA%\catsole\catsole.log` (rotating, 4 files).

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

`hold_ms` tells the device how long the current lyric line stays up, which
is what a paged Thai line divides between its pages. Device-bound text is
folded to ASCII on the PC, because the OLED fonts do not carry the full
Unicode range. The exception is Thai in `main`, which goes as raw UTF-8
rather than `\u` escapes: three bytes a character on the wire, not six.

## Known issues

Things that work but are not right yet.

**Thai lyric timing is off.** A Thai line too long for two rows pages through
it, and the pages currently split the line's `hold_ms` evenly. Syllables are
not spread evenly through a sung line, so the second page tends to arrive late
and linger. It needs weighting by page length at least, and the ETL faces cost
enough draw time per frame that some of the drift may be render lag rather
than the split.

**Words still get cut.** Thai wrapping has no dictionary, so it breaks at the
places that are certainly safe (before the leading vowels, after the trailing
ones) and guesses in between. A guess lands mid-word often enough to notice.
The Latin path can clip the last glyph on a line too, since the width estimate
and the clip window are computed separately.

**Animation needs another pass.** The lyric slide does not run between pages of
the same Thai line, only between lines, so a paged line changes in a jump. The
cat's bob also drifts for a bar or two after a tempo change before the grid
settles again.

## Troubleshooting

**Nothing on the display at all.** Check RES on pin 8 and that the panel has
power. This is not the `DISPLAY_VARIANT` setting — a wrong variant produces a
poor image, not no image.

**The image is upside down, or the panel blanks after a few seconds.** The SPI
bus is running faster than the wiring can carry. Lower `DISPLAY_BUS_HZ`.

**Lyrics mode shows the title instead of lyrics.** No synced lyrics exist for
that track on lrclib, or the track's reported duration is too far from any
match. The control panel shows which of those it is. A line in a script the
display has no font for (Chinese, Japanese, Korean and so on) also falls back
to the title card. Thai is the exception, and is drawn as is.

**Lyrics run early or late.** Set `lyric_offset_ms` in `server/config.json`,
or pass `--offset-ms`. Positive pushes later. Different players report
position with different lag.

**Nothing playing, but something is.** Not every app reports to SMTC. Check
whether Windows itself shows it in the volume flyout's media control.

**CPU temp is `--`.** LibreHardwareMonitor is not running elevated with its
web server on. See above.

**Upload fails with "serial port busy" or "no device found".** The service is
holding the port. Stop the *scheduled task*, not just the process — it is
configured to restart on failure, so killing the process alone makes Windows
start it straight back and grab the port again:

```powershell
Stop-ScheduledTask -TaskName catsole
```

## Layout

```
arduino/catsole/   firmware
arduino/oled_probe/     display diagnostic sketch
server/                 PC service, tests, autostart script
docs/superpowers/       design spec and implementation plan
```

Tests: `cd server && python -m pytest`. They cover the parsing and
frame-shaping seams and need neither the board nor the network.
