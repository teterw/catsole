# Study mode, Claude screen and extras — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a logged study timer (OLED screen, panel controls, `/study` page), a two-hotkey OLED menu, distraction side-eye, streak-driven cat mood, a Claude usage screen and weather on the clock screen.

**Architecture:** All decisions stay on the PC (`server/catsole/`); the board only renders finished strings and numbers sent in NDJSON frames, as today. New pure-logic modules (`settings`, `studylog`, `study`, `studystats`, `menu`, `claude`, `weather`) are unit-tested with fake clocks; `app.py` wires them into `DeskConsole`, `web.py` exposes them, `panel.html` mirrors every new firmware screen in JS for the live preview and the lab.

**Tech Stack:** Python 3.13 (Flask, requests, Pillow — no new dependencies), Arduino UNO R4 WiFi + U8g2 + ArduinoJson, plain HTML/JS.

**Spec:** `docs/superpowers/specs/2026-11-02-study-mode-design.md` — read it first; this plan argues from it. Mockups: `.superpowers/brainstorm/2033-1793623545/content/study-mockups.html` and `bar-styles.html`.

## Global Constraints

- No new pip dependencies. Python runs as `pythonw.exe` 3.13 from the scheduled task `catsole` (Interactive, not elevated), working dir `server/`.
- Never change `DISPLAY_BUS_HZ = 1000000`, `DISPLAY_VARIANT 0`, `SLEEP_AFTER_MS = 180000`, or the equalizer tuning.
- Firmware toolchain is pinned (renesas_uno@1.6.0, U8g2@2.35.30, ArduinoJson@7.4.2). Compile: `"$TEMP/claude/arduino-cli/arduino-cli.exe" compile --fqbn arduino:renesas_uno:unor4wifi arduino/catsole`. Baseline: 116200 B flash (44%), 15640 B RAM (47%). Keep RAM ≤ 75%.
- Flash: `Stop-ScheduledTask -TaskName catsole`, then `arduino-cli upload -p COM3 --fqbn arduino:renesas_uno:unor4wifi arduino/catsole`, then `Start-ScheduledTask -TaskName catsole`. Restarting the task is also how Python changes go live (it runs this checkout).
- Strings to the board are ASCII-folded except `THAI_FIELDS` (`protocol.py`). Thai in the top strip goes as an `mbmp` bitmap (`textbitmap.render`, 11 px tall).
- Data folder = `config.DEFAULT_DATA_DIR` (`%LOCALAPPDATA%\catsole` / `$XDG_STATE_HOME/catsole`). When `Config.data_dir is None` (all tests), nothing is read from or written to disk.
- Dev runs never touch real data or COM3: `python server/run.py --no-serial --web-port 8731 --data-dir "$TEMP/catsole-dev"`.
- Every new OLED screen is drawn twice: in the firmware and in `panel.html`'s JS mirror (live preview `render()` + a lab scene). The user judges visuals in the lab, then on the real panel.
- Scripts Claude Code runs (`server/claude/*.py`): stdlib only, HTTP timeout 0.3 s, always exit 0, print nothing on failure.
- `~/.claude/settings.json` is changed only after showing the user the exact diff and getting a yes.
- Tests: `cd server && python -m pytest -q` (baseline 203 passed, 3 skipped). Use fake clocks, never `sleep`.
- Commits: one per task, plain-sentence subject like the existing log ("Give Instagram videos a card like Facebook's"), ending with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- Bash-tool gotcha here: backslashes inside Python literals in heredocs get mangled — write files with the Write tool.

## Review Focus

1. **Thai, emoji or very long subjects** — must show correctly in the strip (bitmap), the menu (item bitmap), the CSV (Excel opens it) and the page; emoji fold away without producing an empty label. Tests: Task 2 (CSV round-trip), Task 5 (frame meta + bitmap attached for Thai), Task 9 (menu label `""` + `ibmp` sent).
2. **A hand-mangled `study.csv`** (blank lines, a missing column, `studied_min` = "abc", no trailing newline, re-saved by Excel without BOM) — page and streak must skip bad rows, never error. Tests: Task 2 `test_read_skips_bad_rows`, Task 8 `test_summary_with_mangled_log`.
3. **The system clock jumping** (NTP correction backwards, a small forward step) — no negative time, no false "suspend" under 30 s. Test: Task 3 `test_clock_backwards_counts_nothing`, `test_small_jump_still_counts`.
4. **A Claude turn interrupted with Esc** (no `Stop` hook fires) and two sessions at once — the screen must not stick on "working". Test: Task 12 `test_working_goes_stale_without_stop`, `test_any_working_session_wins`.
5. **Nothing external available** (no board, no network for weather, Claude not installed, catsole not running when Claude's scripts fire) — everything degrades silently. Tests: Task 12 `test_statusline_script_without_service`, Task 14 `test_fetch_failure_keeps_last_then_expires`.

---

### Task 1: Data folder, user settings, `--data-dir`

**Files:**
- Create: `server/catsole/settings.py`
- Modify: `server/catsole/config.py`, `server/run.py`
- Test: `server/tests/test_settings.py`

**Interfaces:**
- Produces:
  - `config.DEFAULT_DATA_DIR: Path` — the platform logic moved out of `run.py` (`run.LOG_DIR = DEFAULT_DATA_DIR`).
  - `Config.data_dir: Path | None = None` — `Config.load` converts a string to `Path`.
  - `run.py --data-dir PATH`; after parsing, `config.data_dir = args.data_dir or DEFAULT_DATA_DIR`.
  - `@dataclass UserSettings` with fields `daily_goal_min: int = 60`, `chime: bool = True`, `recent: list[dict]` (each `{"subject": str, "timer": "countdown"|"countup", "minutes": int|None}`), `hotkey_menu: str = "ctrl+f11"`, `hotkey_pick: str = "ctrl+f12"`, `weather: dict | None = None` (`{"city", "lat", "lon"}`).
  - `UserSettings.load(path: Path | None) -> UserSettings` — missing or corrupt file gives defaults; unknown keys ignored.
  - `UserSettings.save(path: Path | None) -> None` — UTF-8 JSON (`ensure_ascii=False`), written to `*.tmp` then `os.replace`; `None` is a no-op.
  - `UserSettings.touch_recent(subject: str, timer: str, minutes: int | None) -> None` — moves it to the front, de-duplicates by subject case-insensitively (the newest setting wins), keeps at most 6.

- [ ] **Step 1: Write the failing tests**

```python
def test_defaults_when_missing(tmp_path):
    s = UserSettings.load(tmp_path / "settings.json")
    assert (s.daily_goal_min, s.chime, s.hotkey_menu, s.hotkey_pick) == (60, True, "ctrl+f11", "ctrl+f12")

def test_round_trip_keeps_thai(tmp_path):
    p = tmp_path / "settings.json"
    s = UserSettings.load(p); s.touch_recent("ฟิสิกส์", "countdown", 25); s.save(p)
    assert UserSettings.load(p).recent[0] == {"subject": "ฟิสิกส์", "timer": "countdown", "minutes": 25}

def test_corrupt_file_gives_defaults(tmp_path):
    p = tmp_path / "settings.json"; p.write_text("{nope", encoding="utf-8")
    assert UserSettings.load(p).daily_goal_min == 60

def test_touch_recent_dedupes_and_caps():
    s = UserSettings()
    for name in ["a", "b", "c", "d", "e", "f", "g"]: s.touch_recent(name, "countup", None)
    s.touch_recent("C", "countdown", 45)
    assert [r["subject"] for r in s.recent] == ["C", "g", "f", "e", "d", "b"]
    assert s.recent[0]["minutes"] == 45

def test_save_none_path_is_noop():
    UserSettings().save(None)  # no exception, nothing written

def test_config_data_dir_from_json(tmp_path):
    (tmp_path / "config.json").write_text('{"data_dir": "C:/x"}', encoding="utf-8")
    assert Config.load(tmp_path / "config.json").data_dir == Path("C:/x")
```

- [ ] **Step 2: Run** `cd server && python -m pytest tests/test_settings.py -q` — expected: FAIL (`ModuleNotFoundError: catsole.settings`).
- [ ] **Step 3: Implement** `settings.py`, the `config.py` field and `DEFAULT_DATA_DIR`, and `run.py --data-dir` per the Interfaces block.
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass (203 + new).
- [ ] **Step 5: Commit** — "Keep user settings and a data folder for study mode".

---

### Task 2: The study log (CSV) and the state file

**Files:**
- Create: `server/catsole/studylog.py`
- Test: `server/tests/test_studylog.py`

**Interfaces:**
- Produces:
  - `COLUMNS = ["date", "start", "end", "subject", "timer", "planned_min", "studied_min", "paused_min", "distractions", "distraction_min", "note"]`.
  - `@dataclass(frozen=True) LoggedSession(date: datetime.date, start: str, end: str, subject: str, timer: str, planned_min: float | None, studied_min: float, paused_min: float, distractions: int, distraction_min: float, note: str = "")`
    - `to_row() -> dict[str, str]` — minutes with one decimal (`"25.0"`), `planned_min` is `""` when None, date ISO.
    - `from_row(row: dict) -> LoggedSession | None` — None when `date` or `studied_min` won't parse; other missing or bad fields fall back to `""`/`0`.
  - `class StudyLog(path: Path | None)`
    - `append(session: LoggedSession) -> bool` — if the file is missing or empty, write a UTF-8 BOM and the header first. If the file doesn't end with a newline, write one first. Return False on any `OSError` (Excel's lock is a `PermissionError`). With `path=None`, keep rows in memory and return True.
    - `read() -> list[LoggedSession]` — reads with `utf-8-sig` and skips rows `from_row` rejects.
    - `replace_last_note(expected: LoggedSession, note: str) -> bool` — rewrites the file (tmp + `os.replace`) only if the last parsed row `== expected`. Otherwise, or on `OSError`, return False.
  - `class StudyStore(path: Path | None)` — `load() -> dict` gives `{"active": dict | None, "pending": list[dict], "last": dict | None}` (defaults if missing or corrupt); `save(state: dict) -> None` writes atomically. With `None`, it keeps state in memory.

- [ ] **Step 1: Write the failing tests**

```python
S = LoggedSession(date(2026, 11, 2), "14:05:00", "14:30:00", "ฟิสิกส์", "countdown", 25.0, 25.0, 0.0, 0, 0.0)

def test_first_append_writes_bom_and_header(tmp_path):
    log = StudyLog(tmp_path / "study.csv"); assert log.append(S)
    raw = (tmp_path / "study.csv").read_bytes()
    assert raw.startswith(b"\xef\xbb\xbfdate,start,end,subject") and raw.count(b"date,start") == 1

def test_round_trip_thai(tmp_path):
    log = StudyLog(tmp_path / "study.csv"); log.append(S); log.append(S)
    assert log.read() == [S, S]

def test_append_after_hand_edit_without_newline(tmp_path):
    p = tmp_path / "study.csv"; StudyLog(p).append(S)
    p.write_bytes(p.read_bytes().rstrip(b"\r\n"))
    StudyLog(p).append(S); assert len(StudyLog(p).read()) == 2

def test_read_skips_bad_rows(tmp_path):
    p = tmp_path / "study.csv"
    p.write_text("date,start,end,subject,timer,planned_min,studied_min\n\n"
                 "2026-11-01,,,math,countup,,abc\nnot-a-date,,,x,countup,,5\n"
                 "2026-11-01,,,math,countup,,30\n", encoding="utf-8")   # no BOM, short header
    rows = StudyLog(p).read()
    assert [(r.subject, r.studied_min) for r in rows] == [("math", 30.0)]

def test_locked_file_returns_false(tmp_path, monkeypatch):
    log = StudyLog(tmp_path / "study.csv")
    monkeypatch.setattr("builtins.open", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert log.append(S) is False

def test_replace_last_note(tmp_path):
    log = StudyLog(tmp_path / "study.csv"); log.append(S)
    assert log.replace_last_note(S, "finished ch.3")
    assert log.read()[-1].note == "finished ch.3"

def test_replace_last_note_refuses_after_hand_edit(tmp_path):
    p = tmp_path / "study.csv"; log = StudyLog(p); log.append(S)
    p.write_text(p.read_text(encoding="utf-8-sig").replace("25.0,0.0", "26.0,0.0"), encoding="utf-8-sig")
    assert log.replace_last_note(S, "x") is False

def test_store_round_trip_and_none(tmp_path):
    st = StudyStore(tmp_path / "s.json"); st.save({"active": {"a": 1}, "pending": [], "last": None})
    assert StudyStore(tmp_path / "s.json").load()["active"] == {"a": 1}
    assert StudyStore(None).load() == {"active": None, "pending": [], "last": None}
```

- [ ] **Step 2: Run** `python -m pytest tests/test_studylog.py -q` — expected: FAIL (module missing).
- [ ] **Step 3: Implement** `studylog.py` per the Interfaces block. Use the `csv` module with `newline=""`.
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Commit** — "Log study sessions to a CSV Excel can open".

---

### Task 3: The study timer

**Files:**
- Create: `server/catsole/study.py`
- Test: `server/tests/test_study.py`

**Interfaces:**
- Consumes: `StudyLog`, `StudyStore`, `LoggedSession` (Task 2).
- Produces:
  - Constants: `GAP_SUSPEND_S = 30`, `RESTART_GRACE_S = 120`, `ASK_AFTER_S = 3 * 3600`, `ASK_TIMEOUT_S = 600`, `PAUSE_LIMIT_S = 1800`, `MIN_LOG_S = 60`, `DISTRACTION_MIN_S = 15`, `DISTRACTION_END_S = 3`, `SAVE_EVERY_S = 5`, `RETRY_PENDING_S = 60`, `MAX_COUNTDOWN_MIN = 600`.
  - `@dataclass Finished(session: LoggedSession | None, reason: str, studied_s: float)`. `reason` is one of `"done"`, `"stopped"`, `"no_answer"` or `"pause_limit"`. `session` is None when the session was discarded for being under `MIN_LOG_S`.
  - `class StudyTimer(log: StudyLog, store: StudyStore, clock: Callable[[], float] = time.time)`:
    - `restore() -> None`
    - `start(subject: str, timer: str, minutes: int | None) -> bool` — False if a session is already active, the subject is blank after strip, or a countdown's minutes are outside 1..600.
    - `pause() -> bool`, `resume() -> bool`, `add_minutes(n: int = 5) -> bool` (countdown only), `answer_yes() -> bool`.
    - `stop() -> Finished | None`
    - `set_note(text: str) -> bool` — while active, it is stored on the session. Otherwise it goes to the last finished session via `log.replace_last_note`. The last finished session stays note-editable until the next `start`.
    - `tick(distracted: bool = False) -> Finished | None`
    - `view() -> dict | None` — `{"subject", "timer", "planned_s", "studied_s", "left_s", "state": "running"|"paused"|"asking", "distracted": bool, "started": "HH:MM"}`. `left_s` is None for count up.
    - Properties: `active: bool`, `last: Finished | None`, `pending_count: int`, `save_error: bool` (True while any row is pending).
    - Public attributes `log: StudyLog`, `store: StudyStore` and `clock`, which Task 5 reads.
- Rules (spec part 1, *Session rules*):
  - Studied time grows by clock deltas while running or asking.
  - A negative delta adds 0.
  - A delta over `GAP_SUSPEND_S` adds 0, and the session becomes paused from the previous tick.
  - Countdown: `studied_s >= planned_s` finishes as `"done"` with exactly the planned minutes.
  - Count up: when `studied_s` crosses `next_ask` (starts at 3 h), the state becomes `"asking"` and the timer records `asked_at` and `studied_at_ask`.
    - `answer_yes` returns it to running, with `next_ask += ASK_AFTER_S`.
    - With no answer for `ASK_TIMEOUT_S`, it finishes as `"no_answer"` with `studied_at_ask`, and `end` = `asked_at`.
  - Paused for `PAUSE_LIMIT_S`: finish as `"pause_limit"`, with `end` = the moment the pause began.
  - Distraction:
    - A stretch counts once `distracted` has been continuously True for `DISTRACTION_MIN_S`. From then on, `view()["distracted"]` is True, and its whole length (including the first 15 s) adds to `distraction_min`.
    - It ends after `DISTRACTION_END_S` of False.
  - Persistence:
    - `store.save` runs on every state change and every `SAVE_EVERY_S` while running, with a `last_tick` epoch.
    - `restore()` with `now - last_tick <= RESTART_GRACE_S` keeps running and counts the gap. Otherwise the session comes back paused from `last_tick`.
  - Finished rows that `log.append` refuses go into `pending`. Pending rows are retried every `RETRY_PENDING_S` in `tick`.

- [ ] **Step 1: Write the failing tests** (fake clock: `class Clock: t = 1_800_000_000.0; __call__ returns t`; helper `run(timer, clock, seconds, step=1)` ticks once per step)

```python
def test_countdown_finishes_and_logs(timer, clock, log):
    timer.start("math", "countdown", 25); f = run(timer, clock, 25 * 60)
    assert f.reason == "done" and f.session.studied_min == 25.0 and f.session.planned_min == 25.0
    assert log.read()[-1].subject == "math" and not timer.active

def test_pause_time_not_counted(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 600); timer.pause(); run(timer, clock, 300)
    timer.resume(); run(timer, clock, 60)
    assert round(timer.view()["studied_s"]) == 660

def test_add_minutes_extends_countdown(timer, clock):
    timer.start("math", "countdown", 25); timer.add_minutes(5)
    assert timer.view()["planned_s"] == 30 * 60

def test_under_a_minute_is_discarded(timer, clock, log):
    timer.start("math", "countup", None); run(timer, clock, 59)
    assert timer.stop().session is None and log.read() == []

def test_suspend_gap_pauses_at_last_tick(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 120)
    clock.t += 3600; timer.tick()
    v = timer.view(); assert v["state"] == "paused" and round(v["studied_s"]) == 120

def test_clock_backwards_counts_nothing(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 100); clock.t -= 50; timer.tick()
    assert round(timer.view()["studied_s"]) == 100

def test_small_jump_still_counts(timer, clock):
    timer.start("math", "countup", None); clock.t += 20; timer.tick()
    assert timer.view()["state"] == "running" and round(timer.view()["studied_s"]) == 20

def test_restore_after_short_restart_keeps_running(store, log, clock):
    a = StudyTimer(log, store, clock); a.start("math", "countup", None); run(a, clock, 100)
    clock.t += 60; b = StudyTimer(log, store, clock); b.restore()
    assert b.view()["state"] == "running" and round(b.view()["studied_s"]) == 160

def test_restore_after_long_gap_comes_back_paused(store, log, clock):
    a = StudyTimer(log, store, clock); a.start("math", "countup", None); run(a, clock, 100)
    clock.t += 3 * 3600; b = StudyTimer(log, store, clock); b.restore()
    assert b.view()["state"] == "paused" and round(b.view()["studied_s"]) == 100

def test_countup_asks_at_3h_and_stops_without_answer(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 3 * 3600, step=10)
    assert timer.view()["state"] == "asking"
    f = run(timer, clock, 600, step=10)
    assert f.reason == "no_answer" and f.session.studied_min == 180.0

def test_answer_yes_keeps_going_and_asks_again(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 3 * 3600, step=10); timer.answer_yes()
    run(timer, clock, 3 * 3600 - 20, step=10); assert timer.view()["state"] == "running"
    run(timer, clock, 30, step=10); assert timer.view()["state"] == "asking"

def test_pause_limit_ends_session(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 1200); timer.pause()
    f = run(timer, clock, 1800, step=10)
    assert f.reason == "pause_limit" and f.session.studied_min == 20.0

def test_distraction_counted_after_15s(timer, clock):
    timer.start("math", "countup", None)
    for _ in range(20): clock.t += 1; timer.tick(distracted=True)
    assert timer.view()["distracted"]
    for _ in range(5): clock.t += 1; timer.tick(distracted=False)
    f = timer.stop(); assert f.session.distractions == 1 and 0.3 <= f.session.distraction_min <= 0.4

def test_short_distraction_not_counted(timer, clock):
    timer.start("math", "countup", None); run(timer, clock, 120)
    for _ in range(10): clock.t += 1; timer.tick(distracted=True)
    run(timer, clock, 10); assert timer.stop().session.distractions == 0

def test_failed_write_kept_pending_and_retried(timer, clock, log, monkeypatch):
    ok = {"v": False}; real = log.append
    monkeypatch.setattr(log, "append", lambda s: ok["v"] and real(s))
    timer.start("math", "countup", None); run(timer, clock, 120); timer.stop()
    assert timer.pending_count == 1 and timer.save_error
    ok["v"] = True; run(timer, clock, 61)
    assert timer.pending_count == 0 and len(log.read()) == 1

def test_late_note_updates_last_row(timer, clock, log):
    timer.start("math", "countup", None); run(timer, clock, 120); timer.stop()
    assert timer.set_note("did ch.3") and log.read()[-1].note == "did ch.3"

def test_cannot_start_twice_or_blank(timer):
    assert timer.start("math", "countup", None) and not timer.start("x", "countup", None)
    timer.stop(); assert not timer.start("  ", "countup", None)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_study.py -q` — expected: FAIL (module missing).
- [ ] **Step 3: Implement** `study.py` per the Interfaces and Rules. Guard every public method with one `threading.Lock`, because the web threads, the hotkey thread and the main loop all call in.
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Commit** — "Time study sessions that survive restarts and sleep".

---

### Task 4: Study statistics

**Files:**
- Create: `server/catsole/studystats.py`
- Test: `server/tests/test_studystats.py`

**Interfaces:**
- Consumes: `LoggedSession` (Task 2).
- Produces:
  - `daily_minutes(sessions: list[LoggedSession]) -> dict[date, float]`
  - `streak(days: dict[date, float], today: date) -> int` — counts consecutive days with minutes > 0 back from today, or from yesterday if today has none.
  - `best_streak(days: dict[date, float]) -> int`
  - `mood(days: dict[date, float], today: date) -> int` — 0 when the streak is 0 and there is any earlier study, 1 for a streak of 1–2 or no history, 2 for 3–6, 3 for 7+.
  - `MILESTONES_H = (10, 25, 50, 100, 250, 500, 1000)`
  - `summarize(sessions: list[LoggedSession], today: date, goal_min: int) -> dict`. The keys are:
    - `total_min`, `sessions`, `today_min`, `goal_min`.
    - `week_min`, `last_week_min` — rolling 7-day windows ending today and the 7 days before.
    - `streak`, `best_streak`.
    - `days` — `{"YYYY-MM-DD": minutes}` for non-zero days in the 371 days ending today.
    - `weeks` — 12 rolling 7-day totals, oldest first.
    - `subjects` — `[[name, minutes]]`, largest first, the top 7 plus `"other"`. Grouping is case-insensitive and shows the most recent spelling.
    - `milestone` — `{"reached": [h...], "next": h | None, "eta_weeks": int | None}`. ETA is `ceil((next - total_h) / (week_min / 60))`, or None when `week_min` is 0.
    - `recent` — the last 5 sessions, newest first, as `to_row()` dicts.

- [ ] **Step 1: Write the failing tests**

```python
T = date(2026, 11, 2)
def days_of(*offsets): return {T - timedelta(d): 30.0 for d in offsets}

def test_streak_counts_from_yesterday_when_today_empty():
    assert streak(days_of(1, 2, 3), T) == 3 and streak(days_of(0, 1), T) == 2 and streak(days_of(2, 3), T) == 0

def test_best_streak(): assert best_streak(days_of(0, 5, 6, 7, 8)) == 4

def test_mood_thresholds():
    assert mood({}, T) == 1 and mood(days_of(2, 3), T) == 0
    assert mood(days_of(0, 1), T) == 1 and mood(days_of(*range(3)), T) == 2 and mood(days_of(*range(7)), T) == 3

def test_summarize_empty():
    s = summarize([], T, 60)
    assert s["total_min"] == 0 and s["days"] == {} and s["weeks"] == [0] * 12 and s["milestone"]["next"] == 10

def test_summarize_numbers():
    rows = [mk(T, "Math", 90), mk(T - timedelta(1), "math", 30), mk(T - timedelta(8), "thai", 60)]
    s = summarize(rows, T, 60)
    assert s["today_min"] == 90 and s["week_min"] == 120 and s["last_week_min"] == 60
    assert s["subjects"][0] == ["Math", 120] and s["recent"][0]["subject"] == "Math"
    assert s["milestone"] == {"reached": [], "next": 10, "eta_weeks": 4}
```

- [ ] **Step 2: Run** `python -m pytest tests/test_studystats.py -q` — expected: FAIL.
- [ ] **Step 3: Implement** `studystats.py`.
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Commit** — "Work out streaks, milestones and the heatmap from the study log".

---

### Task 5: Study mode in the service, its API and the chime

**Files:**
- Create: `server/catsole/chime.py`
- Modify: `server/catsole/app.py`, `server/catsole/web.py`, `server/run.py`
- Test: `server/tests/test_app.py` (new tests), `server/tests/test_web.py` (new)

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `chime.play() -> None`:
    - Windows: `winsound.PlaySound("SystemNotification", SND_ALIAS | SND_ASYNC)`.
    - Linux: `paplay /usr/share/sounds/freedesktop/stereo/complete.oga` if that file exists.
    - Any exception is swallowed.
  - `DeskConsole.__init__(..., settings: UserSettings | None = None)` builds:
    - `self.settings`, loaded from `data_dir/"settings.json"`.
    - `self.study = StudyTimer(StudyLog(data_dir/"study.csv"), StudyStore(data_dir/"study_state.json"))`, with `None` paths when `config.data_dir is None`.
    - It calls `self.study.restore()`.
  - `DeskConsole.study_start(subject: str, timer: str, minutes: int | None) -> bool` — on success also `settings.touch_recent` + save.
  - `DeskConsole.study_control(action: str) -> bool` — `action` is one of `pause`, `resume`, `add5`, `stop` or `yes`.
  - `DeskConsole.study_note(text: str) -> bool`
  - `DeskConsole.study_summary() -> dict` — `studystats.summarize(self.study.log.read(), date.today(), settings.daily_goal_min)`.
  - `DONE_HOLD_S = 7.0`, measured with the timer's own clock (`self.study.clock`) so tests can fake it. While `study.active` or the done screen holds, `build_frame()` returns `_study_frame()`. These keys are fixed, and later tasks and the firmware rely on them:
    - `"t": "frame"`, `"mode": "study"`, `"meta"` (the subject).
    - `"rt"` — `"25m"` (countdown length), `"goal 2h"` / `"goal 90m"` (count up), `"paused"` or `"still?"`.
    - `"time"` and `"sec"` — under 1 h, `"mm:ss"` and `""`; from 1 h, `"h:mm"` and `"ss"`. Countdown shows time left, count up shows time so far.
    - `"pg"` — 0..1000. Countdown: studied ÷ planned. Count up: (today's logged minutes + studied) ÷ goal, capped.
    - `"st"` — `"run"`, `"pause"`, `"ask"` or `"done"`.
    - `"eye"` — 0 or 1, from `view()["distracted"]`.
    - `"wig"` — 1 when a countdown has ≤ 60 s left.
    - `"l1"`, `"l2"` — done: `"25 min"` and `"streak 7d"`. Asking: `"still studying?"` and `"<pick combo> = yes"`. Otherwise `""`.
  - `_attach_title_bitmap` also runs for `mode == "study"`.
  - `snapshot()["study"]` = `{"active": view | None, "last": {"subject", "studied_min", "reason", "note_editable": bool} | None, "discarded": bool, "today_min", "streak", "goal_min", "recent", "pending", "save_error", "chime"}`.
  - Routes:
    - `POST /api/study/start {subject, timer, minutes}` returns 200 `{"ok": true}`, or 400 `{"error"}`.
    - `POST /api/study/control {action}`, `POST /api/study/note {note}`, `GET /api/study/summary`.
    - `POST /api/settings {daily_goal_min?, chime?}` returns the settings.
- `tick()` behaviour:
  - It calls `self.study.tick()` every loop.
  - On a `Finished` with reason `"done"`, it plays the chime (if `settings.chime`) and starts the done hold.
  - While the study screen shows, it skips the load→stats jump, the playback→lyrics jump, the idle rotation and the `eq` messages.

- [ ] **Step 1: Write the failing tests** (fixture `console` gets `Config(cache_dir=tmp_path, data_dir=tmp_path)` and a fake clock injected via `console.study = StudyTimer(..., clock)`)

```python
def test_study_frame_countdown(console):
    assert console.study_start("math", "countdown", 25)
    f = console.build_frame()
    assert (f["mode"], f["time"], f["sec"], f["rt"], f["pg"], f["st"]) == ("study", "25:00", "", "25m", 0, "run")

def test_study_frame_over_an_hour(console, clock):
    console.study_start("math", "countup", None); advance(console, clock, 3725)
    f = console.build_frame(); assert (f["time"], f["sec"]) == ("1:02", "05")

def test_thai_subject_gets_strip_bitmap(console):
    console.study_start("ฟิสิกส์", "countup", None); console.push_frame()
    assert any(m.get("t") == "mbmp" for m in console.link.sent)   # NullLink records sends

def test_done_screen_then_back(console, clock):
    console.mode = "clock"; console.study_start("math", "countdown", 1); advance(console, clock, 60)
    assert console.build_frame()["st"] == "done"
    advance(console, clock, 8); assert console.build_frame()["mode"] == "clock"

def test_music_does_not_take_over_during_study(console):
    console.study_start("math", "countup", None); console.media.now_playing = playing_song()
    console.tick(); assert console.build_frame()["mode"] == "study"

def test_api_start_and_control(console):
    c = create_app(console).test_client()
    assert c.post("/api/study/start", json={"subject": "math", "timer": "countdown", "minutes": 25}).status_code == 200
    assert c.post("/api/study/start", json={"subject": "", "timer": "countup"}).status_code == 400
    assert c.post("/api/study/control", json={"action": "pause"}).json["ok"]
    assert c.get("/api/state").json["study"]["active"]["state"] == "paused"

def test_summary_endpoint_empty(console):
    assert create_app(console).test_client().get("/api/study/summary").json["total_min"] == 0
```

- If `NullLink` doesn't record sends, give it `sent: list` when `echo=False` as part of this step.
- `advance(console, clock, s)` is a test helper: `s` times, `clock.t += 1; console.tick()`.
- `playing_song()` builds a playing `NowPlaying` with an artist and a 200 s duration.

- [ ] **Step 2: Run** `python -m pytest tests/test_app.py tests/test_web.py -q` — expected: the new tests FAIL.
- [ ] **Step 3: Implement** per the Interfaces block. `run.py` passes `config.data_dir` (already set in Task 1).
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Smoke test.** Run `python server/run.py --no-serial --web-port 8731 --data-dir "$TEMP/catsole-dev"` in the background. Then `curl -s -XPOST localhost:8731/api/study/start -H "Content-Type: application/json" -d '{"subject":"math","timer":"countdown","minutes":1}'`. After 65 s, `curl -s localhost:8731/api/study/summary` shows `"total_min": 1.0`. Then stop the dev server.
- [ ] **Step 6: Commit** — "Run study sessions in the service and expose them to the panel".

---

### Task 6: The study screen on the board, mirrored in the panel

**Files:**
- Modify: `arduino/catsole/catsole.ino`, `server/catsole/static/panel.html`

**Interfaces:**
- Consumes: the study frame keys from Task 5 (exact names: `meta`, `mb`, `rt`, `time`, `sec`, `pg`, `st`, `eye`, `wig`, `l1`, `l2`).
- Produces:
  - Firmware:
    - `Frame` gains `rt[12]`, `pg` (uint16), `st[6]`, `eye`, `wig`, `l1[24]`, `l2[24]`. `time`/`sec` reuse `timeText`/`secText`.
    - New poses at the end of the enum: `CAT_STUDY` `"(=o-o=)"`, `CAT_SIDEEYE`, `CAT_SULKY`, `CAT_CHEER` `"(=^o^=)"`, `CAT_WORRIED` `"(=;.;=)"`. Propose the side-eye and sulky art in the lab, e.g. `"(=>.>=)"` and `"(=-_-=)"`.
    - `render()` dispatches `mode == "study"` to `drawStudy()`.
  - Panel: `drawStudy(c, frame)` in the JS mirror. Lab scenes `study`, `study up`, `ask`, `done`, and the new poses on `poses`.
- Layout (mockup A + paw prints):
  - Strip: the subject, via `drawTitleBitmap(boxW)` when `frame.metaBmp >= 0`, else the 5x7 marquee, with `boxW` leaving room for `rt`. `rt` is right-aligned to x = 126 in 5x7, with the rule at `RULE_Y`.
  - Big `time` in `u8g2_font_logisoso24_tn` at x = 3, baseline 40, with `sec` in 5x7 beside it.
  - `CAT_STUDY` (blinking as usual) at the right, `eye` → `CAT_SIDEEYE`.
  - Paws:
    - 15 paws at x = 3 + round(i × 8.4), alternating y = 52 and 57. Each is a 5×4 glyph (toes at (0,0), (2,−1), (4,0); pad 3×2 at (1,2)).
    - Steps done = `pg × 15 / 1000`. The current one blinks at 400 ms, and future ones are a single pixel.
    - A 9×5 fish at x = 117, y = 51, which wiggles ±1 px every 120 ms when `wig`.
  - `st == "pause"`: the time blinks at 500 ms.
  - `st == "ask"`: the body is `l1` / `l2` in 5x7, and the time is small in the strip.
  - `st == "done"`:
    - For 1.8 s after `st` changes, alternate a full invert every 300 ms.
    - Then the cat in 6x12 at the left alternates `CAT_CHEER`/`CAT_HAPPY` every 500 ms, with "done!" in `helvB10` at x = 56, y = 30, `l1` at y = 44 and `l2` at y = 56 (5x7).

- [ ] **Step 1: Firmware.** Add the frame fields and parsing, the poses and `drawStudy()` per the Layout.
- [ ] **Step 2: Compile.** Expected: success, with RAM ≤ 75%.
- [ ] **Step 3: Panel mirror.** Add `drawStudy` in `render()` for `frame.mode === "study"`, plus the lab scenes and poses, using the same coordinates.
- [ ] **Step 4: Check it in a browser.** Open the panel on the dev server (8731) and play every new lab scene. In the live preview, start a 1-minute session through the API and watch it run to the done screen. Fix overlaps you can see.
- [ ] **Step 5: Show the user.** Point them at the lab. After they're happy, flash (Global Constraints) and have them look at the real panel.
- [ ] **Step 6: Commit** — "Draw the study screen: timer, study-glasses cat and paws walking to the fish".

---

### Task 7: The study block on the panel

**Files:**
- Modify: `server/catsole/static/panel.html`, `README.md`

**Interfaces:**
- Consumes: `snapshot()["study"]` and the routes from Task 5.
- Produces: the panel UI (mockup part 2).
  - Setup:
    - The subject box (placeholder "what are you studying?") with quick picks for the recent subjects.
    - Countdown / count up toggle, presets 15/25/45/60 plus a minutes box.
    - **start**, with "today Xh Ym · N day streak" beside it.
  - Running: subject · timer type, the big live time, pause/resume, +5 min (countdown only) and stop.
  - Note box ("what did you get done? (optional, saved with the session)"). It is shown while running, and after the end while `last.note_editable`. It POSTs `/api/study/note` 600 ms after typing stops.
  - Settings line: daily goal (minutes) and the chime on/off.
  - A "study" nav link in the header, to `/study` (Task 8).
  - Exact messages:
    - `save_error`: "study.csv is open in another app; it will save when that's closed."
    - `discarded`: "Under a minute, so it wasn't saved."
    - Note refused: "Couldn't save the note: the last row in study.csv was changed by hand."
  - README gets a "Study mode" section: how to start a session, where the log lives, the columns.

- [ ] **Step 1: Build the block** in `panel.html`, matching the existing style tokens and button look (`aria-pressed`).
- [ ] **Step 2: Check it in a browser** on the dev server. Start, pause, +5 and stop a session, type a note after stop, and confirm the row in `$TEMP/catsole-dev/study.csv` has the note. Lock the file (open it in Excel, or `python -c` holding an exclusive handle) and confirm the save-error message appears, then clears after it is unlocked.
- [ ] **Step 3: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 4: Commit** — "Start and steer study sessions from the panel".

---

### Task 8: The study page

**Files:**
- Create: `server/catsole/static/study.html`
- Modify: `server/catsole/web.py` (`GET /study`), `server/tests/test_web.py`

**Interfaces:**
- Consumes: `GET /api/study/summary` (Task 4 keys).
- Produces: the page approved in `study-mockups.html` part 3, built from the real summary:
  - The four tiles (total and session count, this week vs last, streak and best, today vs goal).
  - The 53-week heatmap, Sunday-top rows. Its levels are 0, under 40, 40–79, 80–129 and 130+ minutes, coloured `--h0`..`--h4` from the mockup. Hovering a cell shows "1h 10m on tue 14 oct", and there is a legend.
  - Milestone bar and chips with "next: …h in about N weeks at this pace".
  - The 12-week bars, the subjects list and the recent-sessions table.
  - Panel/study nav. Polls every 30 s.

- [ ] **Step 1: Write the failing tests**

```python
def test_study_page_served(console):
    r = create_app(console).test_client().get("/study"); assert r.status_code == 200 and b"heat" in r.data

def test_summary_with_mangled_log(console, tmp_path):
    (tmp_path / "study.csv").write_text("date,studied_min\n2026-11-01,abc\n\n2026-11-01,30\n", encoding="utf-8")
    assert create_app(console).test_client().get("/api/study/summary").json["total_min"] == 30
```

- [ ] **Step 2: Run** them. Expected: FAIL (no route).
- [ ] **Step 3: Implement** the route and `study.html`. Reuse the mockup's markup and CSS, with data from the API and an empty state that shows zeros.
- [ ] **Step 4: Check it in a browser** with a dev data dir seeded by 40 sessions over 3 months (a throwaway script in the scratchpad). Hover the cells, then check the empty data dir too.
- [ ] **Step 5: Run** `python -m pytest -q`, then **Commit** — "Show study history as a heatmap with progress".

---

### Task 9: Two-key shortcuts and the OLED menu

**Files:**
- Create: `server/catsole/hotkeys.py`, `server/catsole/menu.py`
- Modify: `server/catsole/app.py`, `server/catsole/web.py`, `server/run.py`, `server/catsole/static/panel.html`, `arduino/catsole/catsole.ino`, `README.md`
- Test: `server/tests/test_menu.py`, `server/tests/test_hotkeys.py`

**Interfaces:**
- Consumes: `UserSettings.hotkey_menu/hotkey_pick/recent` (Task 1), `DeskConsole.study_start/study_control` (Task 5).
- Produces:
  - `hotkeys.parse_combo(text: str) -> tuple[int, int]` — returns `(mods, vk)`. Modifiers are `ctrl`, `alt`, `shift` and `win` (`MOD_CONTROL=0x2`, `MOD_ALT=0x1`, `MOD_SHIFT=0x4`, `MOD_WIN=0x8`). Keys are `a`–`z`, `0`–`9`, `f1`–`f24`, `pause`, `scrolllock`, `home`, `end`, `insert`, `delete`, `pageup`, `pagedown` and `numpad0`–`numpad9`. Anything else raises `ValueError`.
  - `hotkeys.format_combo(mods: int, vk: int) -> str` — canonical lower-case form, e.g. `"ctrl+f11"`.
  - `class Hotkeys(on_press: Callable[[str], None])`:
    - `start()`, `stop()`
    - `bind(bindings: dict[str, str]) -> dict[str, str]` — status per name is `"ok"`, `"taken"`, `"invalid"` or `"unsupported"`.
    - On Windows, one thread owns `RegisterHotKey(None, id, mods | 0x4000 /*MOD_NOREPEAT*/, vk)` and a `GetMessageW` loop. Rebinding is done on that thread through `PostThreadMessageW(WM_APP)`, and the result comes back through an Event. Elsewhere everything is `"unsupported"`.
  - `@dataclass MenuItem(label: str, action: str, subject: str = "", timer: str = "", minutes: int | None = None)` — `action` is one of `start`, `pause`, `resume`, `add5`, `stop` or `close`.
  - `menu.build_items(view: dict | None, recent: list[dict]) -> list[MenuItem]`:
    - Idle: up to 3 recent subjects, labelled `"math 25m"` / `"thai up"`, then `close`.
    - Active: `pause` or `resume`, `+5 min` (countdown only), `stop`, `close`.
  - `class StudyMenu(clock=time.monotonic)` with `MENU_IDLE_S = 6.0`:
    - `press_next(items)` opens the menu at sel 0, or moves the highlight down, wrapping.
    - `press_pick() -> MenuItem | None` closes the menu.
    - `visible() -> bool` is False after 6 s without a press.
    - Attributes `items` and `sel`.
  - `DeskConsole.menu_press(name: str)`:
    - `"menu"` calls `press_next`.
    - `"pick"` answers yes when the study is asking and the menu isn't visible. Otherwise it runs the picked item.
  - `DeskConsole.rebind_hotkeys() -> dict[str, str]`
  - Frames: while the menu is visible, every frame also carries `"mi"` (labels, folded; `""` where `needs_bitmap`) and `"ms"` (sel).
  - Item bitmaps are sent as `{"t": "ibmp", "i": k, "w": w, "h": 11, "d": hex}`, cropped to 104 px wide. They go out when the menu opens or its items change, and again every 2 s while it is visible.
  - The `/api/settings` route accepts `hotkey_menu` and `hotkey_pick`, and returns `"hotkeys": {name: status}`.
  - `snapshot()["study"]["hotkeys"]` = `{"menu": combo, "pick": combo, "status": {...}}`.
  - Firmware: parses `mi`/`ms` (absent = menu hidden) for up to 4 items of `char[20]`, and stores up to 4 item bitmaps (4 × 13 × 11 bytes) from `ibmp`. `drawMenu()` is drawn after any screen:
    - A box from x = 14 to 114.
    - 12 px rows, the inverted highlight bar on `ms`, and at most 4 rows, scrolled to keep `ms` visible.
    - An item whose label is `""` is drawn from its bitmap.
  - Panel: "shortcuts: ctrl+f11 menu · ctrl+f12 pick" beside the study block. Two click-to-record boxes build the combo from `KeyboardEvent` and POST it to `/api/settings`. The panel shows the status ("taken by another program" / "not a key catsole can use"). It also always shows: "Shortcuts inside apps (VS Code, Code::Blocks…) can't be detected, so this one now belongs to catsole."
  - README: a "Shortcuts" section.
  - `run.py` starts the hotkeys after the console is built, unless `--no-serial --once`.

- [ ] **Step 1: Write the failing tests**

```python
def test_parse_and_format():
    assert parse_combo("Ctrl+F11") == (0x2, 0x7A) and format_combo(0x2, 0x7B) == "ctrl+f12"
    with pytest.raises(ValueError): parse_combo("ctrl+")
    with pytest.raises(ValueError): parse_combo("hyper+q")

def test_items_idle_and_active():
    recent = [{"subject": "math", "timer": "countdown", "minutes": 25}, {"subject": "thai", "timer": "countup", "minutes": None}]
    assert [i.label for i in build_items(None, recent)] == ["math 25m", "thai up", "close"]
    view = {"timer": "countdown", "state": "running"}
    assert [i.label for i in build_items(view, recent)] == ["pause", "+5 min", "stop", "close"]
    assert build_items({"timer": "countup", "state": "paused"}, recent)[0].label == "resume"

def test_menu_wraps_and_times_out(clock):
    m = StudyMenu(clock); items = build_items(None, [])
    m.press_next(items); m.press_next(items); assert m.sel == 0          # 1 item wraps
    clock.t += 6.1; assert not m.visible()

def test_pick_closes_and_returns(clock):
    m = StudyMenu(clock); m.press_next(build_items(None, [{"subject": "math", "timer": "countup", "minutes": None}]))
    assert m.press_pick().subject == "math" and not m.visible()

def test_pick_answers_still_studying(console, clock):
    console.study_start("math", "countup", None); advance(console, clock, 3 * 3600)
    console.menu_press("pick"); assert console.study.view()["state"] == "running"

def test_menu_with_thai_subject_sends_item_bitmap(console):
    console.settings.touch_recent("ฟิสิกส์", "countup", None); console.menu_press("menu"); console.push_frame()
    frame = [m for m in console.link.sent if m.get("t") == "frame"][-1]
    assert frame["mi"][0] == "" and any(m.get("t") == "ibmp" and m["i"] == 0 for m in console.link.sent)
```

- [ ] **Step 2: Run** `python -m pytest tests/test_menu.py tests/test_hotkeys.py -q` — expected: FAIL.
- [ ] **Step 3: Implement** the Python side (`menu.py`, `hotkeys.py`, console, web, run.py).
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Firmware, mirror and lab.** Add `drawMenu()` and the `ibmp` parsing, the JS mirror overlay and a lab scene `menu`. Compile (RAM ≤ 75%).
- [ ] **Step 6: Try it for real.** Stop the task, flash, start the task. Press Ctrl+F11 twice and Ctrl+F12 to start a recent subject, then use the menu to pause and stop. Confirm the panel shows `ok` for both keys.
- [ ] **Step 7: Commit** — "Drive study mode from two shortcuts and a menu on the OLED".

---

### Task 10: Distraction side-eye

**Files:**
- Modify: `server/catsole/media.py`, `server/catsole/app.py`
- Test: `server/tests/test_media.py`, `server/tests/test_app.py`

**Interfaces:**
- Consumes: `StudyTimer.tick(distracted)` (Task 3), the `eye` key and `CAT_SIDEEYE` (Task 6).
- Produces:
  - `media.is_distraction(np: NowPlaying | None, artwork: tuple[int, int] | None) -> bool` — True only while it is playing and is one of: `netflix_kind == "watch"`, `tab_site(np)` is not None, or `is_youtube_video(np, artwork)`.
  - `DeskConsole` keeps the raw poll result (`self._raw_media`), so Instagram reels that `_worth_showing` drops still count.
  - It passes `is_distraction(raw, self._artwork if raw is the shown track else None)` into `study.tick`.

- [ ] **Step 1: Write the failing tests** — `is_distraction` is True for a playing Instagram, Facebook, Netflix-watch and YouTube-video session, and False for the same session paused, for a song, and for None. A console test plays Instagram for 20 s during study, then stops: the logged row has `distractions == 1` and the frame had `eye == 1`.
- [ ] **Step 2: Run** — expected: FAIL. **Step 3: Implement.** **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Commit** — "Have the cat side-eye video sites during study".

---

### Task 11: Cat mood follows the streak

**Files:**
- Modify: `server/catsole/app.py`, `arduino/catsole/catsole.ino`, `server/catsole/static/panel.html`
- Test: `server/tests/test_app.py`

**Interfaces:**
- Consumes: `studystats.mood` (Task 4), the poses from Task 6.
- Produces:
  - Every frame carries `"md": 0..3`. It is cached and recomputed when a session finishes and when the date changes.
  - Firmware: a global `catMood` (default 1) is set from `md` on any frame. `catPose()` uses it when not playing music and not studying:
    - 0: `CAT_SULKY` as the base pose.
    - 1: as today.
    - 2: `CAT_HAPPY` for 2 s of every 6 s.
    - 3: `CAT_HAPPY` as the base pose.
    - Blinks and squints stay on top of all of these.
  - JS mirror `labPose` gets the same rules, and a lab scene `moods` shows all four.

- [ ] **Step 1: Write the failing test** — a console with a log of 7 consecutive days ending yesterday gives `build_frame()["md"] == 3`. An empty log gives 1. A log whose last study was 3 days ago gives 0.
- [ ] **Step 2: Run** — expected: FAIL. **Step 3: Implement** (Python, firmware, mirror). **Step 4:** run pytest, compile, show the user the `moods` lab scene.
- [ ] **Step 5: Commit** — "Let the cat's mood follow the study streak".

---

### Task 12: Claude usage — monitor, API and the scripts Claude Code runs

**Files:**
- Create: `server/catsole/claude.py`, `server/claude/statusline.py`, `server/claude/hook.py`
- Modify: `server/catsole/app.py`, `server/catsole/web.py`, `README.md`
- Test: `server/tests/test_claude.py`

**Interfaces:**
- Produces:
  - `class ClaudeMonitor(projects_dir: Path | None, clock=time.time)`, with constants `ACTIVE_S = 600`, `WORK_STALE_S = 600`, `SCAN_EVERY_S = 60`:
    - `on_status(payload: dict) -> None`:
      - Reads `session_id` and `rate_limits.{five_hour,seven_day}.{used_percentage,resets_at}`.
      - `resets_at` may be epoch seconds or an ISO string.
      - When `rate_limits` is absent, the last known values are kept.
      - The call marks the session as seen.
    - `on_event(payload: dict) -> None` — reads `session_id` and `hook_event_name`. `UserPromptSubmit` marks the session working, `Stop` marks it waiting, `SessionEnd` removes it.
    - `state() -> str`:
      - `"work"` if any session is working and its last event is under `WORK_STALE_S` old.
      - Otherwise `"wait"` if any session was seen within `ACTIVE_S`.
      - Otherwise `"idle"`.
    - `active() -> bool`
    - `limits() -> dict` — `{"p5": int | None, "r5": float | None, "p7": int | None, "r7": float | None}`.
    - `scan_today(today: date | None = None) -> tuple[int, int]`:
      - Returns (messages, tokens) for today across `projects_dir/**/*.jsonl` files modified today.
      - It reads incrementally per file (byte offset, and a partial last line held back).
      - Only `type == "assistant"` lines count, with the timestamp converted to a local date, de-duplicated by `message.id`.
      - Tokens = `input_tokens + output_tokens + cache_creation_input_tokens`.
      - Everything resets when the date changes.
    - `start()` — a daemon thread that scans every `SCAN_EVERY_S`.
  - `MODES = ("lyrics", "stats", "clock", "claude")`. `next_mode(current, available=None)` skips `"claude"` unless `"claude"` is in `available`. `tick` passes `{"claude"}` when `claude.active()`.
  - `_claude_frame()` keys:
    - `"t": "frame"`, `"mode": "claude"`, `"cs"`, `"p5"`, `"p7"` (int or null).
    - `"r5"`, `"r7"` — under 1 h `"42m"`, under 24 h `"1h12"`, else a lower-case weekday `"fri"`; `"--"` if unknown.
    - `"td"` — `"214 msgs 3.1M tok"`. Tokens are written as `999`, `12k` or `3.1M`.
  - Routes: `POST /api/claude/status` (the status-line JSON as is) and `POST /api/claude/event` (the hook stdin JSON as is).
  - `server/claude/statusline.py`:
    - Reads the stdin JSON and POSTs it to `http://127.0.0.1:{CATSOLE_PORT or 8730}/api/claude/status` with a 0.3 s timeout.
    - Prints `5h 42% · wk 18%` (only the parts it has), or nothing.
    - Exits 0.
  - `server/claude/hook.py` — the same POST to `/api/claude/event`. It prints nothing and exits 0.
  - The panel's mode buttons get a `claude` button.
  - README gets a "Claude usage" section with the settings snippet.

- [ ] **Step 1: Write the failing tests**

```python
def test_status_keeps_last_limits(mon):
    mon.on_status({"session_id": "a", "rate_limits": {"five_hour": {"used_percentage": 42.4, "resets_at": 1_800_004_320}}})
    mon.on_status({"session_id": "a"})
    assert mon.limits()["p5"] == 42 and mon.limits()["r5"] == 1_800_004_320

def test_iso_reset_time(mon):
    mon.on_status({"session_id": "a", "rate_limits": {"seven_day": {"used_percentage": 18, "resets_at": "2027-01-15T08:00:00Z"}}})
    assert mon.limits()["r7"] == datetime(2027, 1, 15, 8, tzinfo=timezone.utc).timestamp()

def test_working_goes_stale_without_stop(mon, clock):
    mon.on_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"}); assert mon.state() == "work"
    clock.t += 601; assert mon.state() == "idle"

def test_any_working_session_wins(mon):
    mon.on_event({"session_id": "a", "hook_event_name": "UserPromptSubmit"})
    mon.on_event({"session_id": "b", "hook_event_name": "Stop"}); assert mon.state() == "work"
    mon.on_event({"session_id": "a", "hook_event_name": "SessionEnd"}); assert mon.state() == "wait"

def test_scan_today_dedupes_and_filters(tmp_path):
    write_jsonl(tmp_path / "p" / "s.jsonl", [
        asst("m1", today_iso(), inp=10, out=5, cw=100, cr=9999), asst("m1", today_iso(), inp=10, out=5, cw=100, cr=9999),
        asst("m2", yesterday_iso(), inp=1, out=1, cw=1, cr=1), {"type": "user"}])
    assert ClaudeMonitor(tmp_path).scan_today() == (1, 115)

def test_scan_holds_back_partial_line(tmp_path): ...   # second scan after the line is completed counts it once

def test_claude_frame_and_rotation(console):
    console.claude.on_event({"session_id": "a", "hook_event_name": "Stop"})
    assert "claude" in console._available_modes()
    console.mode = "claude"; f = console.build_frame(); assert f["mode"] == "claude" and f["cs"] == "wait"

def test_statusline_script_without_service():
    r = subprocess.run([sys.executable, STATUSLINE], input=json.dumps(SAMPLE), capture_output=True, text=True,
                       timeout=5, env={**os.environ, "CATSOLE_PORT": "1"})
    assert r.returncode == 0 and r.stdout.strip() == "5h 42% · wk 18%"
```

- [ ] **Step 2: Run** `python -m pytest tests/test_claude.py -q` — expected: FAIL.
- [ ] **Step 3: Implement** per the Interfaces block.
  - `DeskConsole.__init__(..., claude: ClaudeMonitor | None = None)` falls back to `ClaudeMonitor(None)`, which never scans, so tests never read the real `~/.claude`. Only `run.py` passes `ClaudeMonitor(Path.home() / ".claude" / "projects")`.
  - Add `DeskConsole._available_modes() -> set[str]`.
  - The `claude` block in `snapshot()` is `{"state", **limits(), "msgs", "tokens"}`, or None when idle.
  - `run()` calls `claude.start()`.
- [ ] **Step 4: Run** `python -m pytest -q` — expected: all pass.
- [ ] **Step 5: Hook it into Claude Code (needs the user's yes).**
  - Show the user the exact `~/.claude/settings.json` diff:
    - Add `"statusLine": {"type": "command", "command": "python \"C:/Users/USER/Desktop/Projects/desk-console/server/claude/statusline.py\""}`.
    - Add `hook.py` entries under `UserPromptSubmit`, `Stop` and `SessionEnd`, next to the existing `coord_guard.py` hooks.
  - Apply it only after they say yes. Then restart the catsole task and send one message in Claude.
  - Expected: `curl -s localhost:8730/api/state` has a non-null `claude` block with `p5`, and the terminal status line shows `5h …%`.
- [ ] **Step 6: Commit** — "Track Claude usage from its status line and hooks".

---

### Task 13: The Claude screen on the board

**Files:**
- Modify: `arduino/catsole/catsole.ino`, `server/catsole/static/panel.html`

**Interfaces:**
- Consumes: the claude frame keys (Task 12), `CAT_WORRIED` (Task 6).
- Produces:
  - `drawClaude()`, laid out like `drawStats` and matching `bar-styles.html` part 2:
    - Strip: `claude`, plus `working` with an animated 0–3 dots, or `your turn`.
    - `drawStatRow(22, "5h", "42%  reset 1h12", p5)` and `drawStatRow(40, "wk", "18%  reset fri", p7)`, with a bar width of 92.
    - `"today " + td` at baseline 61.
    - The cat at x = 99, y = 30. While working, paws tap above a line at y = 44, alternating every 140 ms. While waiting, a `?` at (122, 26). `CAT_WORRIED` when `p5` or `p7` ≥ 90.
  - JS mirror `drawClaude` and a lab scene `claude` (states working / waiting / near the limit).

- [ ] **Step 1: Firmware + mirror + lab.** **Step 2: Compile** (RAM ≤ 75%). **Step 3:** The user looks at the lab, then flash. **Step 4: Commit** — "Show Claude usage on the board".

---

### Task 14: Weather on the clock screen

**Files:**
- Create: `server/catsole/weather.py`
- Modify: `server/catsole/app.py`, `server/catsole/web.py`, `server/catsole/static/panel.html`, `arduino/catsole/catsole.ino`, `README.md`
- Test: `server/tests/test_weather.py`

**Interfaces:**
- Consumes: `UserSettings.weather` (Task 1).
- Produces:
  - `icon_for(code: int) -> int`:
    - 1 clear: 0, 1.
    - 2 cloud: 2, 3, 45, 48, 71–77, 85, 86.
    - 3 rain: 51–67, 80–82.
    - 4 storm: 95–99.
    - Anything else: 2.
  - `geocode(city: str) -> dict | None` — `https://geocoding-api.open-meteo.com/v1/search?name=<city>&count=1`, returning `{"city": name, "lat", "lon"}`.
  - `parse_forecast(data: dict) -> dict` — returns `{"temp": round(current.temperature_2m), "icon": icon_for(current.weather_code), "rain": daily.precipitation_probability_max[0] or None}`.
  - `class WeatherReader(user_agent: str, clock=time.monotonic)`, with `POLL_S = 900` and `STALE_S = 3600`:
    - `set_location(loc: dict | None)`, `start()`.
    - `latest() -> dict | None` — None when there's no location, or the last success is older than `STALE_S`.
    - It fetches `https://api.open-meteo.com/v1/forecast?latitude=..&longitude=..&current=temperature_2m,weather_code&daily=precipitation_probability_max&timezone=auto&forecast_days=1` with `requests`, a 10 s timeout and the config user agent.
  - The clock frame adds `"wt"` (int), `"wi"` (1–4) and `"wr"` (int or null) when `latest()` is not None.
  - `/api/settings` accepts `weather_city`. It geocodes, saves and returns the matched name, or 400 "city not found". An empty string clears it.
  - Panel: a weather city field with the matched name shown.
  - Firmware `drawClock()`: when `wi > 0`, the right column (x ≥ 86, beside the existing `drawVLine(82, 28, 30)`) shows a 12×12 icon at (86, 28), `"31C"` in 5x7 at (101, 37) and `"rain 60%"` in 4x6 at (86, 50). Otherwise it shows today's mcu/up/fps lines.
  - The JS clock mirror is updated to the firmware's current layout (cat at 84,10 in 6x12, seconds beside the date) plus the weather, and a lab scene `clock wx` cycles through the 4 icons.

- [ ] **Step 1: Write the failing tests** — the `icon_for` table above, `parse_forecast` on a saved Open-Meteo JSON fixture, `geocode` parsing a fixture through a stubbed `requests.get`, and `test_fetch_failure_keeps_last_then_expires`: one success, then failures, keeps `latest()` for 3599 s and gives None at 3601 s. A console clock frame has `wt`/`wi`/`wr` only when the reader has data.
- [ ] **Step 2: Run** — expected: FAIL. **Step 3: Implement** (Python, panel field, firmware, mirror). **Step 4:** pytest, compile, the user looks at `clock wx`, then flash.
- [ ] **Step 5: Commit** — "Put the weather on the clock screen".

---

### Task 15: Keep study data when uninstalling

**Files:**
- Modify: `install.ps1`; check `install.sh`

**Interfaces:**
- Produces: `install.ps1 -Uninstall` removes `%LOCALAPPDATA%\catsole` **except** `study.csv`, `study_state.json` and `settings.json`. If those exist, it says "Kept your study log and settings in <path>". Any update or reinstall path that wipes that folder (see around line 140) keeps the same three files. `install.sh`: confirm the app dir (`$DIR`) and the data dir (`$XDG_STATE_HOME/catsole`) differ, and change nothing if they do.

- [ ] **Step 1: Implement** a function `Remove-CatsoleDir -Path -Keep <names>` in `install.ps1` that deletes every other child (long-path safe, like the existing `rmdir \\?\` helper) and removes the folder only if it is empty. Use it in both places.
- [ ] **Step 2: Test it in isolation.** Never run `-Uninstall` on this PC, because it would remove the user's scheduled task. Copy the function into a scratch script, run it against a temp folder holding the three kept files plus `catsole.log`, `app\x.py` and a >260-character nested path, and confirm only the three files remain.
- [ ] **Step 3: Commit** — "Keep the study log and settings when uninstalling".

---

## After the last task

- Full `python -m pytest -q`, firmware compile, and the final flash. Restart the task, then look at `%LOCALAPPDATA%\catsole\catsole.log` for errors.
- Update the project memory (`desk-console-project.md`): study mode, the data files, the shortcut defaults, and the Claude status line/hooks living in `~/.claude/settings.json`.
