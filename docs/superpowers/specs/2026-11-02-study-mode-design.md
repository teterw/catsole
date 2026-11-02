# catsole: study mode, Claude screen and extras — Design

**Date:** 2026-11-02
**Status:** Agreed in conversation, not built.

Mockups the user reacted to live in `.superpowers/brainstorm/2033-1793623545/content/`
(`study-mockups.html`, `bar-styles.html`; open them in a browser). Where this
document and a mockup disagree, this document wins.

## Why

The user studies at the desk with catsole beside them. They want it to time
study sessions, keep a record of what they studied, and show them how far
they have come — a GitHub-style heatmap plus progress numbers. They also use
Claude Code a lot and want catsole to show their Claude usage while Claude
is running. A handful of smaller extras came out of the same conversation.

The board has no buttons. Everything is driven from the PC: the web panel at
`http://127.0.0.1:8730/`, and two global keyboard shortcuts that act as the
buttons the board doesn't have.

## Scope

In, in build order:

1. **Study mode** — timer, OLED study screen, panel controls, session log.
2. **Study page** — `/study`, reads the log: heatmap, progress, subjects.
3. **Two-key shortcuts** — an on-OLED menu driven by two global hotkeys.
4. **Distraction side-eye** — the cat reacts to video sites during study.
5. **Cat mood** — the idle cat's mood follows the study streak.
6. **Claude screen** — 5-hour and weekly limits, today's activity.
7. **Weather** on the clock screen.

Each numbered part should land as its own working, committed step.

Out (offered, not picked): break/pomodoro timer, exam countdown, a quick
timer that isn't logged, ping/net speed. Don't build them.

Non-goals: editing or deleting logged sessions from the UI (the log is a
plain CSV the user can edit by hand); syncing study data anywhere off the
PC; global hotkeys on Linux (the Linux install keeps working, it just has
no shortcuts); any WiFi use on the board.

Anti-goals — it fails even if it works when:

- the study timer loses or double-counts time (a restart, sleep, or flash
  mid-session must not corrupt the log);
- a shortcut steals a key combo the user relies on in another app;
- anything from this work slows Claude Code down or makes it error
  (the status-line and hook scripts must be fast and silent when catsole
  is not running);
- the existing screens regress: lyrics, stats, clock, sleep/night clock,
  Thai text, and the hardware settings listed below.

## Constraints carried over (do not "fix")

From the existing project and its notes:

- `DISPLAY_BUS_HZ = 1000000`, `DISPLAY_VARIANT 0` (SSD1309 NONAME0). Do not
  change either.
- OLED sleep after 3 min without frames (`SLEEP_AFTER_MS`) and the night
  clock stay as they are. A running study session sends frames, so it
  keeps the panel awake by itself.
- To flash the board, stop the **scheduled task** first
  (`Stop-ScheduledTask -TaskName catsole`), not just the Python process —
  the task restarts the process and it retakes COM3. Start it again after.
  `arduino-cli` lives at `%TEMP%\claude\arduino-cli\` (re-download from
  downloads.arduino.cc if missing). Pinned: renesas_uno@1.6.0,
  U8g2@2.35.30, ArduinoJson@7.4.2.
- The service runs as the scheduled task `catsole`: `pythonw.exe
  server\run.py` from the Desktop checkout, Interactive logon, not
  elevated. Interactive logon is what makes global hotkeys possible.
- Strings to the board are folded to ASCII except Thai in `main`
  (`protocol.py`). Thai text in the top strip goes as a PC-rendered bitmap
  (`mbmp`, 11px tall) — subjects may be Thai, so reuse that path.
- The user checks visuals by looking, not by reading descriptions. Every new
  screen gets a scene in the panel's **lab** section (canvas mirror of the
  firmware) so they can see it without the board.
- Data folder is the one `run.py` already uses for logs:
  `%LOCALAPPDATA%\catsole` on Windows, `$XDG_STATE_HOME/catsole` on Linux.

## 1. Study mode

### Starting a session

On the panel's main page, a new **study** block under the mode buttons
(mockup part 2, "setup"):

- a text box "what are you studying?", with the last few subjects as quick
  picks underneath (click to fill);
- timer type: **countdown** or **count up**;
- for countdown, length presets 15 / 25 / 45 / 60 plus a free minutes box;
- **start**, with "today Xh Ym · N day streak" beside it.

While running (mockup "running"): subject + timer type, a big live time,
**pause/resume**, **+5 min** (countdown only), **stop**, and an optional
note box ("what did you get done?") that is saved with the session.

A session can also be started from the shortcut menu (part 3).

### On the OLED — screen A with paw prints to the fish

Picked from the mockups: **screen A** with **paw prints walking to a fish**.

- Top strip: subject on the left, `25m` (countdown length) or `goal 2h`
  (count up) on the right, rule underneath — same as the other screens'
  strip.
- Big timer: countdown shows time left, count up shows time so far. Under an
  hour `mm:ss`; an hour or more shows `h:mm` big with seconds small beside
  it, like the clock screen.
- Cat on the right wearing study glasses: new pose `(=o-o=)`, with the usual
  irregular blinks.
- Bottom row: paw prints step left to right toward a small fish at the right
  end. Steps still to come are faint dots, the current step blinks.
  Countdown: the walk spans the session. Count up: the walk spans today's
  progress toward the daily goal.
- In the last minute of a countdown the fish wiggles.
- **Paused:** the timer blinks and the strip's right side reads `paused`.

The study screen stays up for the whole session — no idle rotation, no jump
to lyrics when music starts, no jump to stats under load. Music keeps
playing; lyrics just aren't shown until the session ends.

### Time's up (countdown)

The screen flashes (inverts) a few times, then the cat eats the fish and
cheers: `(=^.^=)` / `(=^o^=)` with "done!", "25m of math" and the streak.
Holds ~7 s, then normal screens resume. The PC plays a short chime at the
same moment (on by default, a toggle on the panel). Count up has no end;
it runs until stopped.

### Session rules

- Time while paused doesn't count. Time the PC was asleep doesn't count.
- *(Builder-check call)* Gaps are detected simply, with no special clocks.
  The active session's state, including a "last tick" wall time, is written
  to a small state file in the data folder on every change and every few
  seconds. While running, a wall-clock jump between loop ticks of more than
  ~30 s means the machine was suspended. On start, a saved session whose
  last tick is **≤ 2 min** old counts as a service restart (flash or crash)
  and keeps running, with the gap counted. Older means sleep, shutdown or
  long downtime: the session comes back **paused from the last tick**, the
  OLED shows `paused` blinking, and the user resumes or stops it. Coming
  back paused can lose time if they forget to resume, but never counts
  time nobody studied.
- Sessions under 1 minute of studied time are discarded, not logged.
- Only one session at a time.
- **Forgotten sessions** (user's pick: ask, then stop):
  - A count up that reaches **3 h** asks "still studying?" on the OLED and
    the panel. Answering yes (key 2 or the panel) carries on and asks
    again 3 h later. With no answer in **10 min**, the session stops and
    is logged up to the moment it asked.
  - A session paused for **30 min** ends by itself and logs what was
    studied before the pause.
  - A countdown ends on its own, so this doesn't apply to it.

### The log

`study.csv` in the data folder, one row per finished session, with a header
row. It must open cleanly in Excel (UTF-8 with BOM, since subjects may be
Thai). Columns:

`date, start, end, subject, timer, planned_min, studied_min, paused_min,
distractions, distraction_min, note`

- `date` is the local date the session started (YYYY-MM-DD); `start`/`end`
  local times (HH:MM:SS).
- `timer` is `countdown` or `countup`; `planned_min` empty for count up.
- `studied_min` is the number everything else (page, streak, goal) uses.
  Store with one decimal.
- Rows are appended, so a hand edit is safe while the service runs (the
  page re-reads the file).
- *(Builder-check call)* The note can be written after the session ends.
  The row is written as soon as the session ends, so the heatmap, streak
  and mood update straight away. After time's up or stop, the panel keeps
  a note box for that session until the next session starts. A late note
  rewrites only the file's last row, and only if that row is still exactly
  the one the service wrote. If it isn't, the panel says the note couldn't
  be saved.
- *(Builder-check call)* If the file can't be written (Excel locks a CSV
  it has open), finished rows wait in the state file and are retried every
  minute. Meanwhile the panel says "study.csv is open in another app; it
  will save when that's closed". A session is never lost to a failed
  write.
- *(Builder-check call)* On Windows the data folder is also the install
  folder, and `install.ps1 -Uninstall` deletes it. Change the uninstaller
  to keep `study.csv`, the study state file and the settings JSON. Check
  `install.sh` for the same problem.

### Settings

Daily goal (default 1h), chime on/off, recent subjects, shortcut keys,
weather city: kept in a small JSON next to the log, edited from the panel
(not `config.json`, which is for hand-tuned service settings).

## 2. Study page (`/study`)

Approved as mocked in `study-mockups.html` part 3, reading `study.csv`:

- tiles: total (and session count), this week (vs last week), streak (and
  best), today (vs daily goal);
- heatmap of the last 12 months, GitHub style: weeks as columns, 5 levels
  of one colour (the panel's phosphor blue), hover a square for
  "1h 10m on tue 14 oct", legend "less → more";
- "how far you've come": total against the next milestone (10, 25, 50, 100,
  250, 500, 1000 h), reached milestones ticked, and "next: 100h in about N
  weeks at this pace" (pace = this week's hours);
- hours per week, last 12 weeks;
- time per subject, all time;
- recent sessions (date, subject, timer, length).

Same look as the panel (graphite page, phosphor accent, monospace). A nav
link between **panel** and **study** on both pages. Works with an empty or
missing log (shows zeros, no errors).

Streak = consecutive days with any logged study, counting back from today;
if today has none yet, it counts back from yesterday (a streak isn't broken
until a whole day passes without study).

## 3. Two-key shortcuts and the OLED menu

Two global hotkeys (Windows `RegisterHotKey`, on its own thread in the
service). Each is a **two-key combo**.

- **Key 1 — menu/next.** Opens a small menu on the OLED; pressing again
  moves the highlight down (wrapping).
- **Key 2 — pick.** Runs the highlighted item and closes the menu.
- The menu closes by itself after ~6 s without a press.

Menu items:

- Not studying: the last 3 subjects, each with its last timer setting
  (`math 25m`, `thai up`), then `close`.
- Studying: `pause`/`resume`, `+5 min` (countdown), `stop`, `close`.

On the OLED the menu is a list over the current screen with an inverted
highlight bar (the 1-bit selection look the panel's buttons copy). Starting
a new subject still needs the panel — the menu only offers recent ones.

The panel shows the current shortcuts next to the study block, and lets the
user change them (click a box, press the combo). If Windows refuses a combo
because another app holds it, the panel says so.

**Default keys (user's pick): Ctrl+F11 = menu/next, Ctrl+F12 = pick.** The
user was told these take Code::Blocks' Rebuild (Ctrl+F11) and VS Code's Go
to Implementations (Ctrl+F12) and chose them anyway. `RegisterHotKey` only
catches combos another program has registered *globally*; it can't see
shortcuts inside an app. So the panel's "taken" message covers only the
global case, and the panel should say that when keys are changed.

While the "still studying?" question (part 1) is up, key 2 answers yes.

## 4. Distraction side-eye

During a session, if a video site is **playing** — Netflix, YouTube
(a video, not music: the existing `is_youtube_video` test), Facebook or
Instagram, as the service already detects — the cat on the study screen
switches to a side-eye pose until it stops. Each time it starts counts as
one distraction; its duration adds to `distraction_min`. Brief interruptions
already absorbed by the 15 s hold in `app.py` don't count. The timer keeps
running — it's a nudge, not a penalty.

## 5. Cat mood follows the streak

The cat's resting pose on every screen it appears on (when not playing music
and not studying) follows the study streak:

- streak broken (no study yesterday or today, and there was study before):
  sulky pose;
- 1–2 days: normal, as now;
- 3–6 days: content — happy face more often between blinks;
- 7+ days: proud — happy face by default.

The PC works out the mood and sends it in the frame; the board picks poses
from it. New poses (study glasses, side-eye, sulky, eating/cheer) are drawn
as 2-row ASCII like the existing five; exact art is the builder's to propose
in the lab for the user to look at.

## 6. Claude screen

Approved as drafted in `bar-styles.html` part 2: laid out like the stats
screen.

- Strip: `claude`, then `working...` (animated) or `your turn`.
- `5h  42%  reset 1h12` with a bar; `wk  18%  reset fri` with a bar.
- `today 214 msgs 3.1M tok`.
- Cat on the right, paws tapping while Claude works, `?` while it's the
  user's turn, worried face at high usage (≥ 90% on either limit).

Where the numbers come from (verified in the installed Claude Code on
2026-11-02):

- **Limits.** Claude Code passes the status-line command a JSON on stdin
  that includes `rate_limits.five_hour.used_percentage`,
  `rate_limits.five_hour.resets_at`, and the same under `seven_day`. A small
  status-line script POSTs that to the service (e.g. `/api/claude`) and
  prints a short line for the terminal (`5h 42% · wk 18%`) as a bonus. The
  user has no status line configured today. `rate_limits` can be absent
  (e.g. before the first reply) — keep the last known values then.
- **Working / your turn.** Claude Code hooks: `UserPromptSubmit` → working,
  `Stop` → your turn, `SessionEnd` → that session gone. Several sessions
  can run at once (the user does this); working if any session is working.
  The user already has `UserPromptSubmit` and `PreToolUse` hooks
  (`coord_guard.py`) — add alongside, don't replace.
- **Today's messages and tokens.** Count assistant messages dated today in
  `~/.claude/projects/**/*.jsonl` (each has `message.usage`). Tokens =
  input + output + cache writes; cache reads are excluded (they dwarf
  everything and aren't "usage" in a felt sense). Only scan files modified
  today; remember offsets so it isn't a full re-read every time.

All scripts that Claude Code runs must time out within ~0.3 s and exit 0
silently when the service isn't up. They live in the repo (e.g.
`server/claude/`), and the settings change to `~/.claude/settings.json` is
made by the builder after showing the user the exact diff.

When it shows: the Claude screen joins the idle rotation while Claude has
reported in within the last 10 minutes, and has a panel mode button like the
others. It never interrupts a study session or playing music.

## 7. Weather on the clock screen

From Open-Meteo (free, no account): current temperature, a condition icon
(clear / cloud / rain / storm, small 1-bit bitmaps), and today's rain
chance. City set on the panel (looked up with Open-Meteo's geocoding);
until one is set, nothing changes. Poll every 15 min; on failure keep the
last reading for up to an hour, then hide it.

On the clock screen, weather takes the right-hand column where the board's
own info (`mcu %`, `up`, `fps`) sits now; the board info shows instead when
there is no weather. Exact layout is proposed in the lab for the user to
look at.

## Testing

- Python: pytest under `server/tests/`, matching the existing style.
  Timer arithmetic (pause, sleep gaps, restart resume, +5 min, under-1-min
  discard), CSV writing/reading (Thai subjects, BOM, a hand-edited file),
  streak/heatmap aggregation, menu state machine, Claude JSON parsing and
  token counting, weather parsing. Use a fake clock rather than sleeping.
- Firmware: must compile with the pinned toolchain; the new screens are
  checked by the user through the lab mirror and then on the real panel.
- The existing test suite stays green.

## Open questions for the user

None. The shortcut defaults and the forgotten-session rule were settled
after the builder check.
