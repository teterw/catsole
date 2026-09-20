# desk-console Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a USB-tethered Arduino desk display that shows synced lyrics or live PC hardware stats, switched by NFC tag taps, with a matching local web control panel and logon autostart.

**Architecture:** The Arduino is a pure renderer — it owns animation, link-state detection and tap classification, and no content logic. A Python process on the PC owns all content and timing (SMTC polling, lyric sync math, sensor reads) and pushes newline-delimited JSON frames down USB CDC. The device pushes tap events back up the same link.

**Tech Stack:** Arduino UNO R4 WiFi (Renesas core), U8g2, Elechouse PN532, ArduinoJson v7; Python 3.13 with winsdk, pyserial, requests, psutil, Flask, pytest.

**Spec:** `docs/superpowers/specs/2026-09-20-desk-console-design.md`

## Global Constraints

- **No WiFi anywhere.** The board supports it; the project does not use it. All PC communication is Serial over USB CDC.
- **Pin assignments are fixed:** OLED CS=10, DC=9, RES=8, SCK=13, MOSI=11 (hardware SPI). PN532 on I2C via its 4-pin header.
- **Parts list is closed:** UNO R4 WiFi, SSD1309 OLED, PN532. No buzzer, no additional sensors.
- **No lyric content in the repository.** Runtime fetches are cached to a gitignored directory. All test fixtures use invented placeholder text.
- **No Windows service.** SMTC is per-user-session; autostart is a logon-triggered Scheduled Task running as the user.
- **Per-field degradation.** A missing sensor renders `--` for that field only; it never blanks a whole mode.
- **Device-bound text is ASCII-folded** before transmission.
- Serial framing is NDJSON at 115200; unparseable lines are dropped, not resynchronised.

---

### Task 1: Repo skeleton and dependencies

**Files:**
- Create: `.gitignore`, `server/requirements.txt`, `server/pytest.ini`, `server/desk_console/__init__.py`

**Interfaces:**
- Produces: installable dev environment; `pytest` runs from `server/`.

- [ ] **Step 1:** Write `.gitignore` covering `__pycache__/`, `*.pyc`, `.venv/`, `server/cache/`, `build/`, `*.log`, `.pytest_cache/`.
- [ ] **Step 2:** Write `server/requirements.txt`: `pyserial`, `requests`, `psutil`, `flask`, `winsdk`, `pytest`.
- [ ] **Step 3:** Install: `python -m pip install -r server/requirements.txt`. Expected: all resolve on Python 3.13.
- [ ] **Step 4:** Verify `pytest` collects zero tests without error from `server/`.
- [ ] **Step 5:** Commit `chore: scaffold repo and dependencies`.

---

### Task 2: Protocol — ASCII folding and NDJSON frames

**Files:**
- Create: `server/desk_console/protocol.py`
- Test: `server/tests/test_protocol.py`

**Interfaces:**
- Produces:
  - `fold_ascii(text: str) -> str`
  - `encode_frame(obj: dict) -> bytes` (folds all string values, appends `\n`)
  - `decode_line(line: str) -> dict | None` (returns `None` on malformed input)

- [ ] **Step 1: Write failing tests.**

```python
def test_fold_ascii_strips_accents():
    assert fold_ascii("Beyoncé") == "Beyonce"

def test_fold_ascii_normalises_punctuation():
    assert fold_ascii("don’t — stop") == "don't - stop"

def test_fold_ascii_drops_unmappable():
    assert fold_ascii("hello 你好") == "hello"

def test_encode_frame_folds_and_terminates():
    out = encode_frame({"t": "frame", "meta": "Sigur Rós"})
    assert out.endswith(b"\n")
    assert b"Sigur Ros" in out

def test_decode_line_returns_none_on_garbage():
    assert decode_line("{not json") is None

def test_decode_roundtrip():
    assert decode_line(encode_frame({"t": "tap"}).decode()) == {"t": "tap"}
```

- [ ] **Step 2:** Run `pytest tests/test_protocol.py -v`. Expected: FAIL, module not found.
- [ ] **Step 3:** Implement using `unicodedata.normalize("NFKD", ...)`, an explicit punctuation substitution map (curly quotes, en/em dash, ellipsis), then `encode("ascii", "ignore")`. Collapse resulting double spaces.
- [ ] **Step 4:** Run tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: add serial protocol framing and ASCII folding`.

---

### Task 3: Lyrics — LRC parsing, line selection, lrclib client

**Files:**
- Create: `server/desk_console/lyrics.py`
- Test: `server/tests/test_lyrics.py`

**Interfaces:**
- Produces:
  - `parse_lrc(text: str) -> list[tuple[int, str]]` — (ms, line), sorted, blank lines preserved as `""`
  - `select_line(lines, position_ms) -> tuple[str, int]` — (line text, hold_ms until next line)
  - `LyricsProvider(cache_dir, user_agent)` with `.fetch(artist, title, album, duration_s) -> Lyrics`
  - `Lyrics` dataclass: `.kind` in `{"synced","plain","none"}`, `.synced`, `.plain`

- [ ] **Step 1: Write failing tests.** All fixture text is invented placeholder content, never real lyrics.

```python
SAMPLE = "[00:01.00]placeholder line one\n[00:04.50]placeholder line two\n[00:09.00]placeholder line three\n"

def test_parse_lrc_reads_timestamps():
    assert parse_lrc(SAMPLE)[0] == (1000, "placeholder line one")
    assert parse_lrc(SAMPLE)[1] == (4500, "placeholder line two")

def test_parse_lrc_handles_multiple_tags_per_line():
    out = parse_lrc("[00:01.00][00:05.00]repeated\n")
    assert out == [(1000, "repeated"), (5000, "repeated")]

def test_parse_lrc_ignores_metadata_tags():
    assert parse_lrc("[ar:Someone]\n[00:02.00]real\n") == [(2000, "real")]

def test_select_line_before_first_returns_empty_with_hold():
    line, hold = select_line(parse_lrc(SAMPLE), 0)
    assert line == ""
    assert hold == 1000

def test_select_line_returns_current_and_hold():
    line, hold = select_line(parse_lrc(SAMPLE), 5000)
    assert line == "placeholder line two"
    assert hold == 4000

def test_select_line_past_last_holds_open():
    line, hold = select_line(parse_lrc(SAMPLE), 99000)
    assert line == "placeholder line three"
    assert hold > 0
```

- [ ] **Step 2:** Run tests. Expected: FAIL.
- [ ] **Step 3:** Implement `parse_lrc` with regex `\[(\d+):(\d+)(?:[.:](\d+))?\]`, skipping `[xx:yy]` metadata tags that are non-numeric. Implement `select_line` via `bisect`. Implement `LyricsProvider.fetch` calling `/api/get` with exact params first, falling back to `/api/search`, picking the best duration match within 3s, then plain lyrics, then `kind="none"`. Cache keyed by a slug of artist+title into `cache_dir` as JSON.
- [ ] **Step 4:** Run tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: add lrclib client and LRC sync`.

---

### Task 4: Media — SMTC session and position extrapolation

**Files:**
- Create: `server/desk_console/media.py`
- Test: `server/tests/test_media.py`

**Interfaces:**
- Produces:
  - `extrapolate_position(position_ms, last_updated, now, rate, is_playing, duration_ms) -> int` — pure, testable without Windows
  - `MediaReader()` with `.poll() -> NowPlaying | None`
  - `NowPlaying` dataclass: `artist`, `title`, `album`, `duration_ms`, `position_ms`, `is_playing`

- [ ] **Step 1: Write failing tests** for the pure function only — the WinRT half is verified live.

```python
def test_extrapolate_advances_while_playing():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = t0 + timedelta(seconds=3)
    assert extrapolate_position(10_000, t0, now, 1.0, True, 200_000) == 13_000

def test_extrapolate_frozen_while_paused():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = t0 + timedelta(seconds=3)
    assert extrapolate_position(10_000, t0, now, 1.0, False, 200_000) == 10_000

def test_extrapolate_respects_playback_rate():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = t0 + timedelta(seconds=10)
    assert extrapolate_position(0, t0, now, 1.5, True, 200_000) == 15_000

def test_extrapolate_clamps_to_duration():
    t0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
    now = t0 + timedelta(seconds=600)
    assert extrapolate_position(0, t0, now, 1.0, True, 200_000) == 200_000
```

- [ ] **Step 2:** Run tests. Expected: FAIL.
- [ ] **Step 3:** Implement the pure function, then `MediaReader` wrapping `GlobalSystemMediaTransportControlsSessionManager.request_async()`, running the coroutines on a private event loop so the caller stays synchronous. Return `None` when no session exists.
- [ ] **Step 4:** Run tests. Expected: PASS. Then run a live smoke check printing the current session.
- [ ] **Step 5:** Commit `feat: read now-playing from Windows SMTC`.

---

### Task 5: Hardware — LHM, nvidia-smi and psutil with per-field degradation

**Files:**
- Create: `server/desk_console/hardware.py`
- Test: `server/tests/test_hardware.py`

**Interfaces:**
- Produces:
  - `flatten_lhm(node: dict) -> list[tuple[str, str, str]]` — (path, sensor text, value text)
  - `parse_nvidia_smi(csv_line: str) -> dict`
  - `HardwareReader()` with `.poll() -> dict` shaped `{"cpu": {...}, "gpu": {...}}`, any field possibly `None`

- [ ] **Step 1: Write failing tests** against a small synthetic LHM tree and an `nvidia-smi` CSV line.

```python
TREE = {"Text": "Root", "Children": [
    {"Text": "AMD Ryzen 5 7500F", "Children": [
        {"Text": "Temperatures", "Children": [
            {"Text": "Core (Tctl/Tdie)", "Value": "61.4 °C", "Children": []}]}]}]}

def test_flatten_lhm_walks_tree():
    rows = flatten_lhm(TREE)
    assert ("Root/AMD Ryzen 5 7500F/Temperatures", "Core (Tctl/Tdie)", "61.4 °C") in rows

def test_parse_nvidia_smi_reads_all_fields():
    got = parse_nvidia_smi("68, 99, 4211, 8188")
    assert got == {"temp": 68.0, "load": 99.0, "vram_used": 4211.0, "vram_total": 8188.0}

def test_parse_nvidia_smi_returns_empty_on_garbage():
    assert parse_nvidia_smi("N/A") == {}
```

- [ ] **Step 2:** Run tests. Expected: FAIL.
- [ ] **Step 3:** Implement. `HardwareReader.poll` tries LHM on `http://localhost:8085/data.json` with a 0.4s timeout, matching CPU temp by searching flattened rows for `Tctl/Tdie` then `CPU Package`; falls back to `nvidia-smi` via subprocess with `CREATE_NO_WINDOW`; fills CPU load/clock from `psutil`. Each source is wrapped so one failing never raises. Cache the LHM-unavailable verdict for 30s to avoid hammering a closed port.
- [ ] **Step 4:** Run tests. Expected: PASS. Then run a live poll and confirm real GPU numbers appear.
- [ ] **Step 5:** Commit `feat: add hardware stats with layered fallback`.

---

### Task 6: Link — serial autodetect, reconnect, NDJSON transport

**Files:**
- Create: `server/desk_console/link.py`
- Test: `server/tests/test_link.py`

**Interfaces:**
- Produces:
  - `find_port(preferred: str | None = None) -> str | None` — matches VID 0x2341 / PID 0x1002 first
  - `SerialLink(port=None, baud=115200, on_event=callable)` with `.start()`, `.stop()`, `.send(dict)`, `.connected -> bool`
  - Runs its reader on a daemon thread; `on_event` is called with each decoded inbound dict.

- [ ] **Step 1: Write failing tests** using a fake port object, so no hardware is needed.

```python
class FakePort:
    def __init__(self, inbound=b""): self.written = b""; self._in = inbound; self.is_open = True
    def write(self, b): self.written += b
    def readline(self): 
        line, _, self._in = self._in.partition(b"\n")
        return line + b"\n" if line else b""
    def close(self): self.is_open = False

def test_send_writes_ndjson():
    link = SerialLink(); link._port = FakePort()
    link.send({"t": "frame", "mode": "stats"})
    assert link._port.written.endswith(b"\n")
    assert b'"mode"' in link._port.written

def test_inbound_line_dispatches_event():
    seen = []
    link = SerialLink(on_event=seen.append)
    link._handle_line('{"t":"tap","kind":"short"}')
    assert seen == [{"t": "tap", "kind": "short"}]

def test_inbound_garbage_is_dropped_not_raised():
    seen = []
    link = SerialLink(on_event=seen.append)
    link._handle_line("<<noise>>")
    assert seen == []
```

- [ ] **Step 2:** Run tests. Expected: FAIL.
- [ ] **Step 3:** Implement using `serial.tools.list_ports.comports()`, matching `vid == 0x2341 and pid == 0x1002`, falling back to any port whose description contains "USB Serial". The reader thread reconnects with a 2s backoff; `send` is a no-op returning `False` while disconnected so callers never crash on unplug.
- [ ] **Step 4:** Run tests. Expected: PASS.
- [ ] **Step 5:** Commit `feat: add serial link with autodetect and reconnect`.

---

### Task 7: App — mode state machine, poll loop, entry point

**Files:**
- Create: `server/desk_console/app.py`, `server/run.py`, `server/desk_console/config.py`
- Test: `server/tests/test_app.py`

**Interfaces:**
- Consumes: everything from Tasks 2-6.
- Produces:
  - `MODES = ("lyrics", "stats")`
  - `next_mode(current: str) -> str`
  - `DeskConsole(config)` with `.handle_event(dict)`, `.build_frame() -> dict`, `.tick()`, `.run()`
  - `run.py` flags: `--no-serial`, `--no-web`, `--port COM3`, `--once`

- [ ] **Step 1: Write failing tests** for the state machine and frame shaping.

```python
def test_next_mode_cycles_and_wraps():
    assert next_mode("lyrics") == "stats"
    assert next_mode("stats") == "lyrics"

def test_next_mode_recovers_from_unknown():
    assert next_mode("bogus") == "lyrics"

def test_short_tap_advances_mode(console):
    console.mode = "lyrics"
    console.handle_event({"t": "tap", "kind": "short"})
    assert console.mode == "stats"

def test_hold_forces_refresh_without_changing_mode(console):
    console.mode = "stats"
    console.handle_event({"t": "tap", "kind": "hold"})
    assert console.mode == "stats"
    assert console.refresh_requested is True

def test_stats_frame_emits_dashes_for_missing_fields(console):
    console.mode = "stats"
    console.stats = {"cpu": {"temp": None, "load": 12, "clock": 4200}, "gpu": {}}
    frame = console.build_frame()
    assert frame["cpu"]["temp"] is None
    assert frame["mode"] == "stats"
```

- [ ] **Step 2:** Run tests. Expected: FAIL.
- [ ] **Step 3:** Implement. The loop polls media every 250ms, hardware every 1s, and pushes a frame every 250ms (or immediately on mode change / hold refresh). Lyrics are fetched on track change only, in a background thread so an lrclib call never stalls rendering. `--no-serial` swaps the link for a stub that prints frames.
- [ ] **Step 4:** Run tests, then `python run.py --no-serial --once` and confirm a real frame prints.
- [ ] **Step 5:** Commit `feat: add mode state machine and main loop`.

---

### Task 8: Web control panel

**Files:**
- Create: `server/desk_console/web.py`, `server/desk_console/static/panel.html`

**Interfaces:**
- Consumes: `DeskConsole` instance.
- Produces: `create_app(console) -> Flask`, serving `GET /`, `GET /api/state`, `POST /api/mode`, `POST /api/refresh`. Bound to `127.0.0.1:8730`, threaded, run as a daemon thread.

- [ ] **Step 1:** Implement the Flask app and routes; `/api/state` returns mode, link status, now-playing, lyric kind and stats.
- [ ] **Step 2:** Build the panel UI — dark background, monospace, minimal, matching the device's character rather than a generic dashboard. Use the frontend-design skill for the visual pass.
- [ ] **Step 3:** Verify: start with `--no-serial`, fetch `/api/state`, POST a mode change, confirm the mode actually changes.
- [ ] **Step 4:** Commit `feat: add local control panel`.

---

### Task 9: Arduino sketch

**Files:**
- Create: `arduino/desk_console/desk_console.ino`

**Interfaces:**
- Consumes: the NDJSON protocol from Task 2.
- Produces: firmware emitting `hello` and `tap` frames; consuming `frame` and `ping`.

- [ ] **Step 1:** Install toolchain: download `arduino-cli`, `core install arduino:renesas_uno`, `lib install "U8g2" "ArduinoJson"`, and clone the Elechouse PN532 library into the sketchbook `libraries/` folder (two directories: `PN532` and `PN532_I2C`).
- [ ] **Step 2:** Write the sketch: both U8g2 constructors behind `#define DISPLAY_VARIANT`, boot self-test, link state machine (`BOOT`/`WAITING`/`LIVE`/`STALE` at a 4s threshold), marquee with gap-wrap, 12-bar equalizer, stats rows with fill bars, XOR-dither dim for the stale state, PN532 polled at 50ms with edge-detected short/hold classification at a 1.5s threshold and a filling feedback ring.
- [ ] **Step 3:** Compile: `arduino-cli compile --fqbn arduino:renesas_uno:unor4wifi arduino/desk_console`. Expected: success, with flash/RAM usage reported.
- [ ] **Step 4:** Fix any compile errors and recompile until clean. A sketch that has never compiled is not delivered.
- [ ] **Step 5:** Commit `feat: add Arduino firmware`.

---

### Task 10: Autostart

**Files:**
- Create: `server/autostart.ps1`

**Interfaces:**
- Produces: `-Install`, `-Uninstall`, `-Status` switches registering a logon-triggered Scheduled Task.

- [ ] **Step 1:** Implement using `Register-ScheduledTask` with a logon trigger, 20s delay, `pythonw.exe run.py`, restart-on-failure, and `-ExecutionTimeLimit 0`. Resolve `pythonw.exe` from the running interpreter rather than assuming a path.
- [ ] **Step 2:** Add rotating file logging to `%LOCALAPPDATA%\desk-console\` in `run.py`, since `pythonw` has no console.
- [ ] **Step 3:** Verify `-Status` runs clean on a machine where the task is not installed (must report "not installed", not throw). Do not register the task — that is the user's call.
- [ ] **Step 4:** Commit `feat: add logon autostart script`.

---

### Task 11: README and publish

**Files:**
- Create: `README.md`

- [ ] **Step 1:** Write the README: wiring table, parts list, library install steps (including the manual Elechouse clone), the `DISPLAY_VARIANT` note, PC setup, autostart instructions, optional LHM step, protocol reference, troubleshooting.
- [ ] **Step 2:** Run the full test suite one final time and record the actual result.
- [ ] **Step 3:** `gh repo create teterw/desk-console --public --source . --remote origin --push`.
- [ ] **Step 4:** Verify the repo exists and the push landed.
- [ ] **Step 5:** Commit and push any remaining changes.
