/*
 * catsole firmware
 * Arduino UNO R4 WiFi + SSD1309 128x64 OLED (hardware SPI)
 *
 * This board is a renderer, not a decision-maker. It owns animation and
 * link state. Everything about what to show -- track
 * metadata, lyric timing, sensor values -- arrives from the PC as finished
 * strings and numbers over USB CDC.
 *
 * No WiFi is used. The radio stays off; all communication is Serial.
 *
 * Protocol (newline-delimited JSON):
 *   in   {"t":"frame","mode":"lyrics","meta":"...","main":"...","hold_ms":3200,"eq":1,"state":"playing","lyr":"synced"}
 *        (every string is ASCII except "main", which may carry Thai as UTF-8)
 *        {"t":"frame","mode":"stats","cpu":{...},"gpu":{...}}
 *        {"t":"ping"}
 *   out  {"t":"hello","fw":"1.0.0","variant":0}
 */

#include <Arduino.h>
#include <SPI.h>
#include <U8g2lib.h>
#include <ArduinoJson.h>
#include <RTC.h>

#define FIRMWARE_VERSION "1.0.0"

/* ------------------------------------------------------------------ *
 * Display variant
 *
 * SSD1309 panels ship with two common init sequences and the board does
 * not report which one it wants. Start at 0. If the boot self-test looks
 * washed out, shifted by a few columns, or inverted, change this to 1 and
 * re-upload. A wrong choice here does not produce a blank screen, so if
 * nothing lights up at all, check wiring rather than this value.
 * ------------------------------------------------------------------ */
#define DISPLAY_VARIANT 0

/* Pins are fixed by the build. */
#define PIN_CS 10
#define PIN_DC 9
#define PIN_RES 8
/* SCK = 13 and MOSI = 11 are the hardware SPI pins and are not named here. */

#if DISPLAY_VARIANT == 0
U8G2_SSD1309_128X64_NONAME0_F_4W_HW_SPI u8g2(U8G2_R0, PIN_CS, PIN_DC, PIN_RES);
#else
U8G2_SSD1309_128X64_NONAME2_F_4W_HW_SPI u8g2(U8G2_R0, PIN_CS, PIN_DC, PIN_RES);
#endif

/*
 * Hardware SPI clock.
 *
 * The R4 clocks SPI far faster by default than typical dupont wiring
 * carries cleanly. On this build the default speed produced corrupted
 * init commands -- the panel came up inverted about half the time -- and
 * the display dropped its state seconds after each init. 1MHz was verified
 * stable on the bench and is ample: a 1KB frame buffer at 30fps needs
 * roughly 250kbit/s, so this leaves headroom to spare.
 *
 * If you shorten the wiring, 2MHz and 4MHz are worth trying, in that
 * order. Symptoms of running too fast are an upside-down image or a panel
 * that blanks and needs a reset.
 */
static const uint32_t DISPLAY_BUS_HZ = 1000000;

/* ---- timing ------------------------------------------------------- */
static const uint16_t FRAME_INTERVAL_MS = 33;   /* ~30fps */
static const uint32_t STALE_AFTER_MS = 4000;    /* link considered dead */
static const uint32_t HELLO_INTERVAL_MS = 1500; /* until first frame lands */

/* Blank the panel entirely once the PC has been gone this long.
 *
 * The microcontroller is happy running indefinitely, but an OLED is not:
 * brightness decays with hours lit, and static content burns in
 * permanently. The stats labels and the "no link" badge sit in fixed
 * pixels, so a board left powered overnight -- which happens when the USB
 * port keeps supplying power in soft-off -- would slowly etch them into
 * the panel. Sleeping costs nothing and wakes instantly on the next frame. */
static const uint32_t SLEEP_AFTER_MS = 180000; /* 3 minutes */

/* ---- screen geometry ---------------------------------------------- */
static const uint8_t SCREEN_W = 128;
static const uint8_t SCREEN_H = 64;
static const uint8_t META_BASELINE = 7;
static const uint8_t RULE_Y = 10;
/* 43 bars at 2px with 1px gaps comes to exactly 128px, so the row spans
   the full width with no margin left over. */
static const uint8_t EQ_BARS = 43;
static const uint8_t EQ_BAR_W = 2;
static const uint8_t EQ_GAP = 1;
static const uint8_t EQ_ROW_H = 12; /* y 52..63 */
/* Where the mascot perches on the lyrics screen, in front of the bars. */
static const uint8_t CAT_PERCH_X = 96;
/* Stats bars stop short of the mascot's column. */
static const uint8_t STAT_BAR_W = 100;

/* ---- link state ---------------------------------------------------- */
enum LinkState { LINK_BOOT, LINK_WAITING, LINK_LIVE, LINK_STALE };

static LinkState linkState = LINK_BOOT;
static uint32_t lastFrameMs = 0;
static uint32_t lastHelloMs = 0;
static bool everReceived = false;
static bool displayAsleep = false;

/* ==================================================================== *
 * onboard clock
 *
 * The RA4M1 has a real-time clock, so once the PC has set it the board
 * knows the time on its own. That matters here because the USB port keeps
 * power when the PC shuts down -- the board runs all night either way, so
 * it may as well be a clock rather than a dark panel.
 * ==================================================================== */
static bool rtcReady = false;

/* Overnight the panel is dimmed and the digits are walked around the
   screen, so no pixel is lit for hours on end. Same reason televisions
   do it. */
static const uint8_t NIGHT_CONTRAST = 24;
static bool nightMode = false;

/* Frame timing, so headroom questions can be answered with numbers rather
   than estimates. Reported by the "perf" command, which also resets. */
static uint32_t perfRenderSum = 0;
static uint32_t perfSendSum = 0;
static uint32_t perfFrames = 0;
static uint32_t perfSince = 0;

/* Rolling frame rate for the clock screen. Kept apart from the perf
   counters, which reset when queried and so cannot also feed a
   display. */
static uint16_t fpsFrames = 0;
static uint32_t fpsWindowMs = 0;
static uint8_t fpsValue = 0;
/* Share of wall-clock time spent drawing: the board's own workload,
   which is the honest answer to how hard it is having to work. */
static uint32_t fpsBusyUs = 0;
static uint8_t mcuLoad = 0;


/* ---- current frame -------------------------------------------------- */
struct Frame {
  char mode[8];
  char meta[72];
  /* Room for a long Thai line: each Thai character is three bytes of
     UTF-8, and its vowels and tone marks are characters of their own. */
  /* A long Thai line with its word breaks marked came to 226 bytes in real
     lyrics: three a character, and three more for each break. */
  char mainText[288];
  uint32_t holdMs;  /* how long the main line stays up, 0 when open-ended */
  char lyr[8];
  char state[10];
  uint8_t eq;
  uint32_t receivedAtMs;
  uint32_t posMs;   /* track position when this frame was built */
  char timeText[8];  /* clock screen; the board has no RTC of its own */
  char secText[4];
  char dateText[20];
  char fanName[3][14];
  int16_t fanRpm[3];
  int16_t fanPct[3];  /* GPU fans report a percentage, not RPM */
  uint8_t fanCount;
  uint32_t durMs;   /* track length, 0 when unknown */

  float cpuTemp, cpuLoad, cpuClock;
  float gpuTemp, gpuLoad, vramUsed, vramTotal;
  float ramUsed, ramTotal, ramPercent;
};

static Frame frame;

/* ---- animation ------------------------------------------------------ */
static uint32_t lastDrawMs = 0;
static int16_t marqueeOffset = 0;
static uint32_t lastMarqueeMs = 0;
static float eqHeight[EQ_BARS];
static uint16_t eqPhase[EQ_BARS];

/* Spectrum from the PC: sixteen bands, one hex digit each, pushed far
   faster than full frames because a meter that lags is worse than one
   that is merely approximate. If nothing arrives the bars fall back to
   the synthetic wave, so an old PC-side build still looks alive. */
static const uint8_t EQ_BANDS = 16;
static const uint32_t EQ_FRESH_MS = 600;
static uint8_t eqBand[EQ_BANDS];
static uint32_t lastEqMs = 0;
/* Where we are within the current beat, 0 on the beat rising to 99.
   Estimated on the PC, which can afford the tempo tracking. */
static uint8_t beatPhase = 0;
static uint32_t beatPhaseAtMs = 0;
static uint16_t beatPeriodMs = 0;
/* The bob runs off its own continuous phase rather than the reported
   one. Messages land 20 times a second, and snapping onto each of them
   jolted the cat mid-arc -- twenty small corrections a second, which is
   what read as a stutter. This free-runs and is eased toward the
   reported phase instead. */
static float catPhase = 0.0f;
static uint32_t catPhaseMs = 0;

/* A lyric line that swaps instantly is jarring at this size, so the
   outgoing line is kept around long enough to slide it out while the new
   one slides in beneath it. */
static const uint16_t TRANSITION_MS = 260;
static uint32_t transitionStartMs = 0;
/* When the current layout's line arrived, which is what its pages count
   from. Kept apart from transitionStartMs, which moves on first. */
static uint32_t wrapShownMs = 0;

/* Set when the main line changes; the layout is rebuilt on the next draw
   rather than inside the serial handler, which keeps the text-layout types
   out of the parser's scope. */
static bool wrapDirty = true;

#define MAX_WRAP_LINES 6
/* Bytes, not characters: a Thai row of twelve letters with their marks
   and word breaks came to 78 in real lyrics. */
#define MAX_WRAP_CHARS 96

/* A finished layout: which lines, at which size.
 *
 * Choosing a font and wrapping to it costs several milliseconds, because
 * it measures the string repeatedly against up to three faces. Doing that
 * every frame for a line that changes every few seconds was the single
 * biggest cost in the render loop, so layout happens once when the text
 * arrives and drawing just replays the result. */
struct WrappedText {
  char lines[MAX_WRAP_LINES][MAX_WRAP_CHARS];
  uint8_t count;
  uint8_t styleIndex;
  uint32_t holdMs;
  /* Letters on each row, marks not counted: what a paged line's time is
     shared out by. */
  uint8_t weight[MAX_WRAP_LINES];
  /* When each page comes up, in ms after the line arrived. */
  uint16_t pageAt[MAX_WRAP_LINES];
};

static WrappedText wrapCurrent;
static WrappedText wrapPrev;
/* The page the outgoing line was on when it was replaced, so it slides
   out from where it was rather than jumping back to its first page. */
static uint8_t wrapPrevPage = 0;
/* The page on screen and the one it replaced, so turning a page slides the
   way changing a line does instead of jumping. */
static uint8_t wrapPageShown = 0;
static uint8_t wrapPageFrom = 0;
static uint32_t pageSlideStartMs = 0;

/* ---- serial receive -------------------------------------------------- */
static char rxBuf[896];
static uint16_t rxLen = 0;
static bool rxOverflow = false;

/* ==================================================================== *
 * helpers
 * ==================================================================== */

static void resetFrame() {
  memset(&frame, 0, sizeof(frame));
  strcpy(frame.mode, "lyrics");
  strcpy(frame.state, "idle");
  strcpy(frame.lyr, "none");
  frame.cpuTemp = frame.cpuLoad = frame.cpuClock = NAN;
  frame.gpuTemp = frame.gpuLoad = frame.vramUsed = frame.vramTotal = NAN;
  frame.ramUsed = frame.ramTotal = frame.ramPercent = NAN;
}

static void copyField(char *dest, size_t size, const char *src) {
  if (src == NULL) {
    dest[0] = 0;
    return;
  }
  strncpy(dest, src, size - 1);
  dest[size - 1] = 0;
  /* A cut through the middle of a multi-byte character would leave a
     broken sequence at the end, so back off to the character's start. */
  size_t len = strlen(dest);
  if (len == size - 1 && ((uint8_t)src[len] & 0xC0) == 0x80) {
    while (len > 0 && ((uint8_t)dest[len] & 0xC0) == 0x80) len--;
    dest[len] = 0;
  }
}

/* Try to bring the reader up. Called at boot and retried while it is
   absent, so fixing the wiring does not require a re-flash. */
/* Format a float that may be absent. */
static void fmtNum(char *out, size_t n, float value, uint8_t decimals,
                   const char *suffix) {
  if (isnan(value)) {
    snprintf(out, n, "--%s", suffix);
    return;
  }
  char number[16];
  dtostrf(value, 0, decimals, number);
  snprintf(out, n, "%s%s", number, suffix);
}

/* ==================================================================== *
 * outbound messages
 * ==================================================================== */

static void sendHello() {
  Serial.print(F("{\"t\":\"hello\",\"fw\":\""));
  Serial.print(F(FIRMWARE_VERSION));
  Serial.print(F("\",\"variant\":"));
  Serial.print(DISPLAY_VARIANT);
  Serial.print(F(",\"rtc\":"));
  Serial.print(rtcReady ? F("true") : F("false"));
  Serial.println(F("}"));
}

/* Announced on both edges, so the panel's power state is observable from
   the PC rather than only visible by looking at the desk. */
static void sendSleepState(bool asleep) {
  Serial.print(F("{\"t\":\"display\",\"asleep\":"));
  Serial.print(asleep ? F("true") : F("false"));
  Serial.println(F("}"));
}

/* ==================================================================== *
 * inbound frames
 * ==================================================================== */

static void handleLine(const char *line) {
  if (line[0] == 0) return;

  JsonDocument doc;
  DeserializationError err = deserializeJson(doc, line);
  if (err) return;  /* Drop it. Resynchronising is worse than missing a frame. */

  const char *type = doc["t"] | "";

  if (strcmp(type, "ping") == 0) {
    lastFrameMs = millis();
    everReceived = true;
    return;
  }

  if (strcmp(type, "rtc") == 0) {
    /* The PC owns the wall clock; the board just keeps counting once it
       has been told. Resent periodically so drift cannot accumulate. */
    RTCTime set(doc["d"] | 1, (Month)((int)(doc["mo"] | 1) - 1),
                doc["y"] | 2026, doc["h"] | 0, doc["mi"] | 0, doc["s"] | 0,
                (DayOfWeek)((int)(doc["dow"] | 0)), SaveLight::SAVING_TIME_INACTIVE);
    if (RTC.setTime(set)) rtcReady = true;
    lastFrameMs = millis();
    everReceived = true;
    return;
  }

  if (strcmp(type, "eq") == 0) {
    const char *bands = doc["b"] | "";
    for (uint8_t i = 0; i < EQ_BANDS && bands[i]; i++) {
      char c = bands[i];
      uint8_t v = 0;
      if (c >= '0' && c <= '9') v = (uint8_t)(c - '0');
      else if (c >= 'a' && c <= 'f') v = (uint8_t)(c - 'a' + 10);
      else if (c >= 'A' && c <= 'F') v = (uint8_t)(c - 'A' + 10);
      eqBand[i] = v;
    }
    beatPhase = doc["p"] | 0;
    beatPhaseAtMs = millis();
    beatPeriodMs = doc["ms"] | 0;
    lastEqMs = millis();
    /* Spectrum counts as traffic, so a quiet passage does not look like a
       dropped link. */
    lastFrameMs = millis();
    everReceived = true;
    return;
  }

  if (strcmp(type, "perf") == 0) {
    uint32_t elapsed = millis() - perfSince;
    Serial.print(F("{\"t\":\"perf\",\"frames\":"));
    Serial.print(perfFrames);
    Serial.print(F(",\"window_ms\":"));
    Serial.print(elapsed);
    Serial.print(F(",\"fps\":"));
    Serial.print(elapsed ? (perfFrames * 1000.0f) / elapsed : 0.0f, 1);
    Serial.print(F(",\"draw_us\":"));
    Serial.print(perfFrames ? perfRenderSum / perfFrames : 0);
    Serial.print(F(",\"send_us\":"));
    Serial.print(perfFrames ? perfSendSum / perfFrames : 0);
    Serial.print(F(",\"rtc\":"));
    Serial.print(rtcReady ? F("true") : F("false"));
    Serial.print(F(",\"night\":"));
    Serial.print(nightMode ? F("true") : F("false"));
    Serial.println(F("}"));
    perfRenderSum = perfSendSum = perfFrames = 0;
    perfSince = millis();
    return;
  }

  if (strcmp(type, "frame") != 0) return;

  copyField(frame.mode, sizeof(frame.mode), doc["mode"] | "lyrics");
  copyField(frame.state, sizeof(frame.state), doc["state"] | "idle");
  copyField(frame.lyr, sizeof(frame.lyr), doc["lyr"] | "none");
  copyField(frame.meta, sizeof(frame.meta), doc["meta"] | "");

  /* Frames arrive several times a second carrying the same line, so the
     transition must start only on a genuine change. */
  char incoming[sizeof(frame.mainText)];
  copyField(incoming, sizeof(incoming), doc["main"] | "");
  if (strcmp(incoming, frame.mainText) != 0) {
    transitionStartMs = millis();
    wrapDirty = true;
    /* Read once, on the line's first frame, when it is the whole span. */
    frame.holdMs = doc["hold_ms"] | 0UL;
  }
  copyField(frame.mainText, sizeof(frame.mainText), incoming);
  frame.eq = doc["eq"] | 0;
  frame.posMs = doc["pos"] | 0UL;
  copyField(frame.timeText, sizeof(frame.timeText), doc["time"] | "");
  copyField(frame.secText, sizeof(frame.secText), doc["sec"] | "");
  copyField(frame.dateText, sizeof(frame.dateText), doc["date"] | "");

  frame.fanCount = 0;
  for (JsonObject fan : doc["fans"].as<JsonArray>()) {
    if (frame.fanCount >= 3) break;
    copyField(frame.fanName[frame.fanCount], 14, fan["name"] | "fan");
    frame.fanRpm[frame.fanCount] = fan["rpm"] | -1;
    frame.fanPct[frame.fanCount] = fan["pct"] | -1;
    frame.fanCount++;
  }
  frame.durMs = doc["dur"] | 0UL;
  frame.receivedAtMs = millis();

  /* Absent sensors arrive as JSON null and must stay absent, not become 0. */
  frame.cpuTemp = doc["cpu"]["temp"].isNull() ? NAN : doc["cpu"]["temp"].as<float>();
  frame.cpuLoad = doc["cpu"]["load"].isNull() ? NAN : doc["cpu"]["load"].as<float>();
  frame.cpuClock = doc["cpu"]["clock"].isNull() ? NAN : doc["cpu"]["clock"].as<float>();
  frame.gpuTemp = doc["gpu"]["temp"].isNull() ? NAN : doc["gpu"]["temp"].as<float>();
  frame.gpuLoad = doc["gpu"]["load"].isNull() ? NAN : doc["gpu"]["load"].as<float>();
  frame.vramUsed = doc["gpu"]["vram_used"].isNull() ? NAN : doc["gpu"]["vram_used"].as<float>();
  frame.vramTotal = doc["gpu"]["vram_total"].isNull() ? NAN : doc["gpu"]["vram_total"].as<float>();
  frame.ramUsed = doc["ram"]["used"].isNull() ? NAN : doc["ram"]["used"].as<float>();
  frame.ramTotal = doc["ram"]["total"].isNull() ? NAN : doc["ram"]["total"].as<float>();
  frame.ramPercent = doc["ram"]["percent"].isNull() ? NAN : doc["ram"]["percent"].as<float>();

  lastFrameMs = millis();
  everReceived = true;
}

static void pollSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      if (rxLen > 0) {
        rxBuf[rxLen] = 0;
        if (!rxOverflow) handleLine(rxBuf);
        rxLen = 0;
        rxOverflow = false;
      }
    } else if (rxLen < sizeof(rxBuf) - 1) {
      rxBuf[rxLen++] = c;
    } else {
      /* Line too long: discard the whole thing rather than a truncated half. */
      rxOverflow = true;
    }
  }
}

/* ==================================================================== *
 * drawing
 * ==================================================================== */

/* Halve apparent brightness by masking the frame buffer to a checkerboard.
   Operating on the buffer directly is far cheaper than 8192 drawPixel
   calls, and unlike XOR it never lights a pixel that was dark. */
static void dimBuffer() {
  uint8_t *buf = u8g2.getBufferPtr();
  uint16_t len = (uint16_t)u8g2.getBufferTileWidth() *
                 (uint16_t)u8g2.getBufferTileHeight() * 8;
  for (uint16_t i = 0; i < len; i++) {
    buf[i] &= (i & 1) ? 0xAA : 0x55;
  }
}

/* above and below are how far the current face reaches from its baseline,
   which the clip has to allow; the defaults fit the 5x7 meta strip. */
static void drawMarquee(const char *text, uint8_t baseline, uint8_t boxW,
                        uint8_t above = 8, uint8_t below = 3) {
  uint16_t width = u8g2.getUTF8Width(text);
  if (width <= boxW) {
    u8g2.drawUTF8(2, baseline, text);
    return;
  }
  const uint8_t gap = 14;
  uint16_t span = width + gap;
  int16_t offset = marqueeOffset % (int16_t)span;

  /* Clip coordinates are unsigned, so a baseline near the top of the
     screen must not be allowed to go negative here: it wraps to a huge
     value, the clip window stops meaning anything, and the off-screen
     copy of the text gets drawn straight across the rest of the panel. */
  uint8_t top = (baseline >= above) ? (uint8_t)(baseline - above) : 0;
  uint8_t bottom = (uint8_t)min((int)baseline + below, (int)SCREEN_H);

  u8g2.setClipWindow(0, top, boxW + 2, bottom);
  u8g2.drawUTF8(2 - offset, baseline, text);
  u8g2.drawUTF8(2 - offset + span, baseline, text);
  u8g2.setMaxClipWindow();
}

/* ---- Thai ----------------------------------------------------------
 *
 * The ETL Thai faces are typewriter fonts: every glyph, vowel and tone
 * mark alike, has a full cell's advance, and the marks are drawn to land
 * on the letter before them when struck in the same cell. So marks are
 * drawn at the previous letter's position without advancing, which is how
 * a Thai typewriter composed them too. U8g2's own string routines advance
 * after every glyph, so Thai text goes through the helpers below instead.
 * Text with no Thai in it still takes U8g2's own path, unchanged.
 */

/* Decode one UTF-8 character and step past it. A malformed byte is
   passed through as itself, so bad input costs one glyph, not the line. */
static uint16_t nextCodepoint(const char *&p) {
  const uint8_t *s = (const uint8_t *)p;
  if (s[0] < 0x80) {
    p += 1;
    return s[0];
  }
  if ((s[0] & 0xE0) == 0xC0 && (s[1] & 0xC0) == 0x80) {
    p += 2;
    return (uint16_t)(((s[0] & 0x1F) << 6) | (s[1] & 0x3F));
  }
  if ((s[0] & 0xF0) == 0xE0 && (s[1] & 0xC0) == 0x80 &&
      (s[2] & 0xC0) == 0x80) {
    p += 3;
    return (uint16_t)(((s[0] & 0x0F) << 12) | ((s[1] & 0x3F) << 6) |
                      (s[2] & 0x3F));
  }
  p += 1;
  return s[0];
}

/* A zero-width space. The PC puts one at every Thai word boundary, which
   it can find with a dictionary and the board cannot (server/catsole/
   thai.py). It is a place a row may end, and is never drawn. */
static const uint16_t WORD_BREAK = 0x200B;

/* Whether the line being laid out carries word breaks. When it does, they
   are the only places a Thai phrase is split; the syllable guesses below
   are left for a single word too wide for a row. */
static bool wordBreaksKnown = false;

/* Vowels above and below the letter, and the tone marks. */
static bool isThaiMark(uint16_t c) {
  return c == 0x0E31 || (c >= 0x0E34 && c <= 0x0E3A) ||
         (c >= 0x0E47 && c <= 0x0E4E);
}

/* Thai is U+0E00..U+0E7F, which UTF-8 encodes as E0 B8 xx or E0 B9 xx. */
static bool hasThai(const char *s, uint16_t len = 0xFFFF) {
  for (uint16_t i = 0; i + 1 < len && s[i] && s[i + 1]; i++) {
    if ((uint8_t)s[i] == 0xE0 &&
        ((uint8_t)s[i + 1] == 0xB8 || (uint8_t)s[i + 1] == 0xB9)) {
      return true;
    }
  }
  return false;
}

static uint16_t textWidth(const char *s) {
  if (!hasThai(s)) return u8g2.getUTF8Width(s);
  uint16_t w = 0;
  while (*s) {
    uint16_t c = nextCodepoint(s);
    if (c == WORD_BREAK) continue;
    /* The C++ wrapper in U8g2 2.35.30 has no getGlyphWidth, but the
       C function behind it does, and the handle is public. */
    if (!isThaiMark(c)) {
      int8_t gw = u8g2_GetGlyphWidth(u8g2.getU8g2(), c);
      if (gw > 0) w += (uint16_t)gw;
    }
  }
  return w;
}

static void drawText(int16_t x, int16_t y, const char *s) {
  if (!hasThai(s)) {
    u8g2.drawUTF8(x, y, s);
    return;
  }
  int16_t pen = x;
  int16_t cell = x;
  while (*s) {
    uint16_t c = nextCodepoint(s);
    /* Skipped outright: as a glyph it would move the cell the next mark
       stacks on. */
    if (c == WORD_BREAK) continue;
    if (isThaiMark(c)) {
      u8g2.drawGlyph(cell, y, c);
    } else {
      cell = pen;
      pen += u8g2.drawGlyph(pen, y, c);
    }
  }
}

/* Whether a line may end just before byte i of a word.
 *
 * Thai writes a phrase without spaces, so a phrase wider than the panel
 * has to be split inside itself. Without a dictionary the syllables are
 * unknown, but the worst breaks are cheap to rule out: never between a
 * letter and its marks, never after a vowel that is written before its
 * letter, and never before a vowel that trails one. */
static bool canBreakBefore(const char *word, uint16_t i) {
  if (i == 0) return false;
  if (((uint8_t)word[i] & 0xC0) == 0x80) return false;  /* mid-character */

  const char *here = word + i;
  uint16_t next = nextCodepoint(here);
  if (isThaiMark(next)) return false;
  if (next == 0x0E30 || next == 0x0E32 || next == 0x0E33 ||
      next == 0x0E45 || next == 0x0E46 || next == 0x0E2F) {
    return false;  /* sara a, sara aa, sara am, lakkhangyao, mai yamok, paiyannoi */
  }

  uint16_t back = i - 1;
  while (back > 0 && ((uint8_t)word[back] & 0xC0) == 0x80) back--;
  const char *prevStart = word + back;
  uint16_t prev = nextCodepoint(prevStart);
  if (prev >= 0x0E40 && prev <= 0x0E44) return false;  /* leading vowels */
  return true;
}

/* Whether byte i of a word sits on a marked word break: just before one,
   or just after. */
static bool atWordBreak(const char *word, uint16_t i) {
  const char *here = word + i;
  if (nextCodepoint(here) == WORD_BREAK) return true;
  return i >= 3 && (uint8_t)word[i - 3] == 0xE2 &&
         (uint8_t)word[i - 2] == 0x80 && (uint8_t)word[i - 1] == 0x8B;
}

/* Whether byte i of a word is certainly a syllable edge: before a vowel
   written ahead of its letter, or after one that closes a syllable. A
   break anywhere else may land mid-syllable. With word breaks marked,
   only those count: a syllable edge inside a word is still inside it, and
   "after sara aa" is not even that in a word like "ngaan". */
static bool isSyllableEdge(const char *word, uint16_t i) {
  if (wordBreaksKnown) return atWordBreak(word, i);
  const char *here = word + i;
  uint16_t next = nextCodepoint(here);
  if (next >= 0x0E40 && next <= 0x0E44) return true;

  uint16_t back = i - 1;
  while (back > 0 && ((uint8_t)word[back] & 0xC0) == 0x80) back--;
  const char *prevStart = word + back;
  uint16_t prev = nextCodepoint(prevStart);
  return prev == 0x0E30 || prev == 0x0E32 || prev == 0x0E33 || prev == 0x0E46;
}

/* How many bytes of word can follow line[0..lineLen) and still fit in
   boxW, cutting only where canBreakBefore allows. 0 if none can. A
   syllable edge is preferred when it keeps at least half the fill, since
   a line that ends a little short reads far better than a split word;
   edgesOnly refuses anything else. */
static uint16_t fitPrefix(char *line, uint16_t lineLen, const char *word,
                          uint16_t wordLen, uint8_t boxW,
                          bool edgesOnly = false) {
  uint16_t fit = 0;
  uint16_t fitEdge = 0;
  for (uint16_t i = 1; i <= wordLen && lineLen + i < MAX_WRAP_CHARS; i++) {
    if (i < wordLen && !canBreakBefore(word, i)) continue;
    memcpy(line + lineLen, word, i);
    line[lineLen + i] = 0;
    if (textWidth(line) > boxW) break;
    fit = i;
    if (i == wordLen || isSyllableEdge(word, i)) fitEdge = i;
  }
  line[lineLen] = 0;
  /* A marked word break beats a split word however short it leaves the
     row; a guessed edge only when it keeps half the fill. */
  bool takeEdge = edgesOnly || fitEdge * 2 >= fit || (wordBreaksKnown && fitEdge);
  return takeEdge ? fitEdge : fit;
}

/* ---- text wrapping --------------------------------------------------
 *
 * A lyric line is whatever length the song makes it, so the band has to
 * adapt rather than assume two lines will do. Wrap greedily by word at
 * the current font; if the text still will not fit in the lines
 * available, step down to a smaller font rather than cut the tail off.
 */

static char wrapBuf[MAX_WRAP_LINES][MAX_WRAP_CHARS];
static uint8_t wrapCount = 0;

/* Fills wrapBuf. Returns true only if the whole string was consumed. */
static bool wrapText(const char *text, uint8_t boxW, uint8_t maxLines) {
  wrapCount = 0;
  const char *p = text;
  if (maxLines > MAX_WRAP_LINES) maxLines = MAX_WRAP_LINES;

  char cur[MAX_WRAP_CHARS];
  char cand[MAX_WRAP_CHARS];

  while (*p && wrapCount < maxLines) {
    while (*p == ' ') p++;
    if (!*p) break;

    cur[0] = 0;
    uint16_t curLen = 0;

    while (*p) {
      const char *wordStart = p;
      while (*p && *p != ' ') p++;
      uint16_t wordLen = (uint16_t)(p - wordStart);

      uint16_t pos = curLen;
      memcpy(cand, cur, curLen);
      if (curLen) cand[pos++] = ' ';

      if (pos + wordLen < MAX_WRAP_CHARS) {
        memcpy(cand + pos, wordStart, wordLen);
        cand[pos + wordLen] = 0;
        if (textWidth(cand) <= boxW) {
          memcpy(cur, cand, pos + wordLen + 1);
          curLen = pos + wordLen;
          while (*p == ' ') p++;
          continue;
        }
      }

      /* Does not fit. A Thai phrase is split to fill the rest of the
         line, since it may be most of a sentence, but only at a syllable
         edge: a line that already has text on it can afford to end
         short. Anything else starts the next line whole. */
      p = wordStart;
      if (curLen && hasThai(wordStart, wordLen)) {
        uint16_t fit = fitPrefix(cand, pos, wordStart, wordLen, boxW, true);
        if (fit) {
          memcpy(cand + pos, wordStart, fit);
          cand[pos + fit] = 0;
          memcpy(cur, cand, pos + fit + 1);
          curLen = pos + fit;
          p = wordStart + fit;
        }
      }
      break;
    }

    if (curLen == 0) {
      /* A single word wider than the panel. Break it mid-word, otherwise
         the loop cannot advance and nothing would ever be drawn. */
      const char *wordStart = p;
      uint16_t wordLen = 0;
      while (wordStart[wordLen] && wordStart[wordLen] != ' ') wordLen++;
      cur[0] = 0;
      uint16_t fit = fitPrefix(cur, 0, wordStart, wordLen, boxW);
      if (fit == 0) {
        /* Not even one character fits; take one anyway to make progress. */
        fit = 1;
        while (fit < wordLen && fit < MAX_WRAP_CHARS - 1 &&
               !canBreakBefore(wordStart, fit)) {
          fit++;
        }
      }
      memcpy(cur, wordStart, fit);
      cur[fit] = 0;
      curLen = fit;
      p = wordStart + fit;
    }

    memcpy(wrapBuf[wrapCount], cur, curLen + 1);
    wrapCount++;
  }

  while (*p == ' ') p++;
  return (*p == 0);
}

struct TextStyle {
  const uint8_t *font;
  uint8_t maxLines;   /* how many lines a layout may run to */
  uint8_t lineH;
  uint8_t pageLines;  /* how many of them the band shows at once */
};

/* Largest first. The first style that fits the whole line wins. Latin
   and Thai each have their own run, chosen by whether the line has any
   Thai in it; the Thai faces carry ASCII too, so mixed lines are fine. */
static const TextStyle MAIN_STYLES[] = {
    {u8g2_font_helvB10_tf, 2, 13, 2},
    {u8g2_font_6x12_tf, 3, 11, 3},
    {u8g2_font_5x7_tf, 4, 8, 4},
    /* Thai stacks marks above and below the letters, so a line needs 17px
       even at 14px, and there is no smaller Thai face. Two lines fill the
       band; a longer line pages through two at a time instead of being
       cut off. */
    {u8g2_font_etl16thai_t, 1, 19, 1},
    {u8g2_font_etl14thai_t, 2, 17, 2},
    {u8g2_font_etl14thai_t, 6, 17, 2},
};
static const uint8_t LATIN_STYLE_FIRST = 0;
static const uint8_t THAI_STYLE_FIRST = 3;
static const uint8_t MAIN_STYLE_COUNT = 6;

/* How a paged line's time is shared out. Each page gets a share of the
   line's hold in proportion to the letters on it, since a page with twice
   the words takes about twice as long to sing. Splitting evenly with a
   minimum per page ran past the end of short lines, so their last page
   was never seen. A page still gets a readable floor where the line is
   long enough to afford one, and each page after the first starts a
   little early, so it has slid into place by its first word. */
static const uint16_t PAGE_MAX_MS = 3500;   /* average per page, at most */
static const uint16_t PAGE_FLOOR_MS = 900;
static const uint16_t PAGE_SLIDE_MS = 220;
static const uint16_t PAGE_OPEN_MS = 2500;  /* no hold: cycle at this rate */

/* The band between the rule and the equalizer row. */
static const uint8_t BAND_TOP = 14;
static const uint8_t BAND_BOTTOM = 50;


/* Letters on a row: what is read and sung. Marks ride on their letter,
   and spaces and word breaks are not read at all. */
static uint8_t rowWeight(const char *row) {
  uint8_t n = 0;
  while (*row) {
    uint16_t c = nextCodepoint(row);
    if (c != ' ' && c != WORD_BREAK && !isThaiMark(c) && n < 255) n++;
  }
  return n;
}

/* Work out when each page of a finished layout comes up. */
static void schedulePages(WrappedText &w) {
  uint8_t per = MAIN_STYLES[w.styleIndex].pageLines;
  uint8_t pages = (w.count + per - 1) / per;
  w.pageAt[0] = 0;
  if (pages <= 1) return;

  uint16_t weight[MAX_WRAP_LINES];
  uint32_t total = 0;
  for (uint8_t p = 0; p < pages; p++) {
    weight[p] = 0;
    for (uint8_t r = p * per; r < (p + 1) * per && r < w.count; r++) {
      weight[p] += w.weight[r];
    }
    total += weight[p];
  }

  uint32_t span = w.holdMs;
  if (span > (uint32_t)pages * PAGE_MAX_MS) span = (uint32_t)pages * PAGE_MAX_MS;
  uint32_t share[MAX_WRAP_LINES];
  for (uint8_t p = 0; p < pages; p++) {
    share[p] = total ? span * weight[p] / total : span / pages;
  }

  /* Lift any page below the floor to it, taking the difference from the
     pages above it in proportion to how far above they are. */
  uint32_t floorMs = span / pages;
  if (floorMs > PAGE_FLOOR_MS) floorMs = PAGE_FLOOR_MS;
  uint32_t deficit = 0, pool = 0;
  for (uint8_t p = 0; p < pages; p++) {
    if (share[p] < floorMs) deficit += floorMs - share[p];
    else pool += share[p] - floorMs;
  }
  if (deficit && pool) {
    for (uint8_t p = 0; p < pages; p++) {
      share[p] = share[p] <= floorMs
                     ? floorMs
                     : share[p] - (uint32_t)((uint64_t)deficit *
                                             (share[p] - floorMs) / pool);
    }
  }

  uint32_t t = share[0];
  for (uint8_t p = 1; p < pages; p++) {
    uint32_t lead = share[p - 1] / 4;
    if (lead > PAGE_SLIDE_MS) lead = PAGE_SLIDE_MS;
    uint32_t at = t > lead ? t - lead : 0;
    if (at <= w.pageAt[p - 1]) at = w.pageAt[p - 1] + 1;
    w.pageAt[p] = (uint16_t)(at < 65535UL ? at : 65535UL);
    t += share[p];
  }
}

/* Lay text out once. Call when the text changes, not when drawing. */
static void prepareWrap(const char *text, uint8_t boxW, uint32_t holdMs,
                        WrappedText &out) {
  out.count = 0;
  out.styleIndex = 0;
  out.holdMs = holdMs;
  if (text == NULL || text[0] == 0) return;
  wordBreaksKnown = strstr(text, "\xE2\x80\x8B") != NULL;

  bool thai = hasThai(text);
  uint8_t first = thai ? THAI_STYLE_FIRST : LATIN_STYLE_FIRST;
  uint8_t last = thai ? MAIN_STYLE_COUNT : THAI_STYLE_FIRST;
  uint8_t chosen = last - 1;
  for (uint8_t i = first; i < last; i++) {
    u8g2.setFont(MAIN_STYLES[i].font);
    if (wrapText(text, boxW, MAIN_STYLES[i].maxLines)) {
      chosen = i;
      break;
    }
  }
  /* If nothing fit, wrapBuf holds the smallest font's attempt, which is
     the best available answer. */
  out.styleIndex = chosen;
  out.count = wrapCount;
  for (uint8_t i = 0; i < wrapCount && i < MAX_WRAP_LINES; i++) {
    memcpy(out.lines[i], wrapBuf[i], MAX_WRAP_CHARS);
    out.weight[i] = rowWeight(out.lines[i]);
  }
  schedulePages(out);
}

/* Which page of a layout is showing, sinceMs after the line arrived.
   With a known hold the pages share it and the last one stays put; an
   open-ended line, such as a title card, cycles. */
static uint8_t currentPage(const WrappedText &wrapped, uint32_t sinceMs) {
  const TextStyle &style = MAIN_STYLES[wrapped.styleIndex];
  if (wrapped.count <= style.pageLines) return 0;
  uint8_t pages = (wrapped.count + style.pageLines - 1) / style.pageLines;

  if (wrapped.holdMs == 0) return (uint8_t)((sinceMs / PAGE_OPEN_MS) % pages);

  uint8_t page = 0;
  while (page + 1 < pages && sinceMs >= wrapped.pageAt[page + 1]) page++;
  return page;
}

/* Draws one page of a prepared layout, vertically centred. yShift moves
   the whole block, which is what the slide transition uses; the caller
   clips to the band so shifted text cannot escape it. */
static void drawWrapped(const WrappedText &wrapped, int16_t yShift,
                        uint8_t page) {
  if (wrapped.count == 0) return;

  const TextStyle style = MAIN_STYLES[wrapped.styleIndex];
  u8g2.setFont(style.font);

  uint8_t first = page * style.pageLines;
  if (first >= wrapped.count) first = 0;
  uint8_t shown = wrapped.count - first;
  if (shown > style.pageLines) shown = style.pageLines;

  uint8_t total = shown * style.lineH;
  uint8_t bandH = BAND_BOTTOM - BAND_TOP;
  uint8_t top = BAND_TOP + (bandH > total ? (uint8_t)((bandH - total) / 2) : 0);

  for (uint8_t i = 0; i < shown; i++) {
    int16_t y = (int16_t)(top + (i + 1) * style.lineH - 3) + yShift;
    /* Skip lines that have travelled clear of the band. */
    if (y < (int16_t)BAND_TOP - 20 || y > (int16_t)BAND_BOTTOM + 20) continue;
    drawText(2, y, wrapped.lines[first + i]);
  }
}

/* ==================================================================== *
 * mascot
 *
 * An original character, drawn as text glyphs rather than a bitmap so it
 * keeps the look of something typed. Three rows, seven columns; at the
 * 4x6 face that is 28x18px, small enough to sit in a corner of any
 * screen, and at 6x12 it is 42x36px, which carries the boot screen.
 * ==================================================================== */

enum CatPose {
  CAT_IDLE,
  CAT_BLINK,
  CAT_HAPPY,
  CAT_SLEEP,
  CAT_SQUINT,
  CAT_POSE_COUNT
};

/* Two rows, five columns. Three rows at the 4x6 face came to 28x18px
   and read as a scribble on the real panel; fewer, larger characters
   survive the pixel budget far better. */
static const char *const CAT_ART[CAT_POSE_COUNT][2] = {
    {" /\\_/\\ ", "(=o.o=)"},  /* idle   */
    {" /\\_/\\ ", "(=-.-=)"},  /* blink  */
    {" /\\_/\\ ", "(=^.^=)"},  /* music  */
    {" /\\_/\\ ", "(=u.u=)"},  /* asleep */
    {" /\\_/\\ ", "(=-.o=)"},  /* squint */
};

static void drawCat(int16_t x, int16_t y, uint8_t pose, const uint8_t *font,
                    uint8_t lineH) {
  if (pose >= CAT_POSE_COUNT) pose = CAT_IDLE;
  u8g2.setFont(font);
  for (uint8_t row = 0; row < 2; row++) {
    u8g2.drawUTF8(x, y + row * lineH, CAT_ART[pose][row]);
  }
}

/* Choose a pose from the clock, so the cat is never quite still.
 *
 * Blinks are short and irregular, which is what stops it reading as a
 * loop. A cat that blinks on a tidy two-second beat looks mechanical. */
static uint8_t catPose(bool playing, bool resting) {
  if (resting) return CAT_SLEEP;

  uint32_t t = millis();
  /* Two offset cycles give an uneven blink rhythm without any randomness
     to store between frames. */
  if ((t % 4300) < 160) return CAT_BLINK;
  if ((t % 7100) < 130) return CAT_BLINK;
  if ((t % 11300) < 900) return CAT_SQUINT;
  return playing ? CAT_HAPPY : CAT_IDLE;
}

static void drawEqualizer(bool active) {
  for (uint8_t i = 0; i < EQ_BARS; i++) {
    float target;
    bool live = (millis() - lastEqMs) < EQ_FRESH_MS;

    if (active && live) {
      /* Real spectrum. Sixteen bands stretched across forty-three bars,
         interpolated so neighbours stay smooth instead of stepping in
         blocks of three. */
      float pos = (i * (float)(EQ_BANDS - 1)) / (float)(EQ_BARS - 1);
      uint8_t lo = (uint8_t)pos;
      uint8_t hi = (lo + 1 < EQ_BANDS) ? (uint8_t)(lo + 1) : lo;
      float frac = pos - lo;
      float v = eqBand[lo] * (1.0f - frac) + eqBand[hi] * frac;
      target = 1.0f + (v / (float)(EQ_BANDS - 1)) * (EQ_ROW_H - 1);
    } else if (active) {
      /* No spectrum arriving, so fall back to a travelling wave. Two sines
         at different rates, offset by bar index, keep neighbours related
         so it reads as something sweeping the screen rather than noise. */
      float t = millis() / 240.0f;
      float x = i * 0.34f + (eqPhase[i] / 900.0f);
      float v = fabs(sin(t + x)) * 0.62f + fabs(sin(t * 0.47f + x * 1.9f)) * 0.38f;
      target = 1.0f + v * (EQ_ROW_H - 1);
    } else {
      target = 1.0f;  /* decay to a flat line when idle or paused */
    }
    eqHeight[i] += (target - eqHeight[i]) * (active ? 0.28f : 0.12f);

    uint8_t h = (uint8_t)max(1.0f, eqHeight[i]);
    if (h > EQ_ROW_H) h = EQ_ROW_H;
    u8g2.drawBox(i * (EQ_BAR_W + EQ_GAP), SCREEN_H - h, EQ_BAR_W, h);
  }
}

/* Progress through the track, drawn just under the title.
 *
 * Frames arrive four times a second, which would make this step visibly.
 * Playback advances in real time, so the position is carried forward
 * locally between frames and corrected whenever a new one lands. */
/* Where playback is now: the last reported position, moved on by the time
   since unless paused, so it runs smoothly between frames. */
static uint32_t playbackPosition() {
  uint32_t pos = frame.posMs;
  if (strcmp(frame.state, "playing") == 0) {
    pos += (millis() - frame.receivedAtMs);
  }
  if (frame.durMs && pos > frame.durMs) pos = frame.durMs;
  return pos;
}

static void drawProgress() {
  if (frame.durMs == 0) return;
  uint32_t pos = playbackPosition();
  uint8_t w = (uint8_t)(((uint64_t)pos * (SCREEN_W - 4)) / frame.durMs);
  if (w > 0) u8g2.drawBox(2, RULE_Y + 2, w, 2);
}

/* m:ss, or h:mm:ss for anything an hour or longer. */
static void fmtClock(char *out, size_t n, uint32_t ms) {
  unsigned long s = ms / 1000UL;
  if (s >= 3600UL) {
    snprintf(out, n, "%lu:%02lu:%02lu", s / 3600UL, (s / 60UL) % 60UL, s % 60UL);
  } else {
    snprintf(out, n, "%lu:%02lu", s / 60UL, s % 60UL);
  }
}

/* A show in place of a song: its name, or the Netflix name when that is
   all the PC knows, and how far through it is. Brave passes on only the
   page's title, which for Netflix's player is just "Netflix", so the
   wordmark is the usual case. */
static void drawVideoCard() {
  const uint8_t boxW = CAT_PERCH_X - 6;
  if (frame.mainText[0] == 0) {
    u8g2.setFont(u8g2_font_helvB10_tf);
    u8g2.drawStr(2, 30, "NETFLIX");
  } else if (hasThai(frame.mainText)) {
    u8g2.setFont(u8g2_font_etl14thai_t);
    u8g2.setClipWindow(0, BAND_TOP, boxW + 2, BAND_BOTTOM);
    drawText(2, 30, frame.mainText);
    u8g2.setMaxClipWindow();
  } else {
    u8g2.setFont(u8g2_font_helvB10_tf);
    drawMarquee(frame.mainText, 30, boxW, 12, 3);
  }

  char elapsed[12], total[12], line[28];
  fmtClock(elapsed, sizeof(elapsed), playbackPosition());
  if (frame.durMs) {
    fmtClock(total, sizeof(total), frame.durMs);
    snprintf(line, sizeof(line), "%s / %s", elapsed, total);
  } else {
    snprintf(line, sizeof(line), "%s", elapsed);
  }
  /* An hour-long film runs to h:mm:ss twice, too wide at 6x12. */
  u8g2.setFont(u8g2_font_6x12_tf);
  if (u8g2.getStrWidth(line) > boxW) u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawStr(2, 45, line);
}

static void drawLyrics() {
  u8g2.setFont(u8g2_font_5x7_tf);
  drawMarquee(frame.meta, META_BASELINE, SCREEN_W - 4);
  u8g2.drawHLine(0, RULE_Y, SCREEN_W);
  drawProgress();

  bool idle = strcmp(frame.state, "idle") == 0;
  bool video = strcmp(frame.lyr, "video") == 0;

  if (idle) {
    /* Nothing to show, so the mascot gets the space. */
    drawCat(6, 24, catPose(false, false), u8g2_font_6x12_tf, 11);
    u8g2.setFont(u8g2_font_5x7_tf);
    u8g2.drawUTF8(56, 34, "nothing");
    u8g2.drawUTF8(56, 44, "playing");
  } else if (video) {
    /* Drop the last song's layout, so the next song slides in on its own
       rather than chasing out a line from before the show. */
    if (wrapDirty) {
      wrapCurrent.count = 0;
      wrapDirty = false;
    }
    drawVideoCard();
  } else if (frame.mainText[0] != 0) {
    /* Rebuild the layout only when the line actually changed. The outgoing
       line's layout is already finished, so it is copied rather than
       recomputed. */
    if (wrapDirty) {
      wrapPrevPage = currentPage(wrapCurrent, millis() - wrapShownMs);
      wrapPrev = wrapCurrent;
      /* Wrap clear of the mascot's column. Masking over the text
         afterwards blanked the end of any line that reached it. */
      prepareWrap(frame.mainText, CAT_PERCH_X - 6, frame.holdMs, wrapCurrent);
      wrapShownMs = transitionStartMs;
      wrapDirty = false;
      wrapPageShown = 0;
      pageSlideStartMs = 0;
    }

    uint32_t since = millis() - transitionStartMs;
    if (since < TRANSITION_MS) {
      /* Ease out, so the incoming line decelerates into place instead of
         stopping dead. The band is clipped for the duration so neither
         line can bleed into the meta strip or the equalizer row. */
      float p = (float)since / (float)TRANSITION_MS;
      float q = 1.0f - p;
      float eased = 1.0f - (q * q * q);
      int16_t travel = (int16_t)(BAND_BOTTOM - BAND_TOP);

      u8g2.setClipWindow(0, BAND_TOP, SCREEN_W, BAND_BOTTOM);
      drawWrapped(wrapPrev, (int16_t)(-eased * travel), wrapPrevPage);
      drawWrapped(wrapCurrent, (int16_t)((1.0f - eased) * travel), 0);
      u8g2.setMaxClipWindow();
    } else {
      /* Turning a page slides it up and out as the next comes in beneath,
         the same motion as a new line, only quicker. */
      uint8_t page = currentPage(wrapCurrent, since);
      if (page != wrapPageShown) {
        wrapPageFrom = wrapPageShown;
        wrapPageShown = page;
        pageSlideStartMs = millis();
      }
      uint32_t turning = millis() - pageSlideStartMs;
      if (turning < PAGE_SLIDE_MS) {
        float p = (float)turning / (float)PAGE_SLIDE_MS;
        float q = 1.0f - p;
        float eased = 1.0f - (q * q * q);
        int16_t travel = (int16_t)(BAND_BOTTOM - BAND_TOP);
        u8g2.setClipWindow(0, BAND_TOP, SCREEN_W, BAND_BOTTOM);
        drawWrapped(wrapCurrent, (int16_t)(-eased * travel), wrapPageFrom);
        drawWrapped(wrapCurrent, (int16_t)((1.0f - eased) * travel), page);
        u8g2.setMaxClipWindow();
      } else {
        drawWrapped(wrapCurrent, 0, page);
      }
    }

    /* A Thai line is 17px tall, so even one leaves no room for a label
       under a second. */
    uint8_t labelRoom = wrapCurrent.styleIndex >= THAI_STYLE_FIRST ? 1 : 2;
    if (strcmp(frame.lyr, "synced") == 0) {
    } else if (wrapCount <= labelRoom) {
      /* Only label the fallback when the title left room for it. */
      u8g2.setFont(u8g2_font_4x6_tf);
      u8g2.drawUTF8(2, BAND_BOTTOM, strcmp(frame.lyr, "plain") == 0
                                        ? "lyrics not timed"
                                        : "no lyrics found");
    }
  }

  bool playing = strcmp(frame.state, "playing") == 0;
  drawEqualizer(frame.eq == 1 && playing);

  /* The cat stands in front of the bars rather than beside them. The
     equalizer is decoration, so occluding its right end costs nothing,
     where taking layout space from the lyric would cost the screen's
     whole point. Centred text never reaches past y45, so the two do not
     collide even on a four-line lyric. */

  /* Bob on the tracked beat grid.
   *
   * Watching for bass spikes here looked erratic, because detection is
   * never perfect and a missed or doubled hit shows up immediately. The
   * PC estimates the tempo instead and sends the phase, so this only has
   * to shape it into a dip that lands on the beat. */
  bool liveEq = (millis() - lastEqMs) < EQ_FRESH_MS;
  float lift_f = 0.0f;
  /* A show's soundtrack is mostly talk, so the cat sits and watches. */
  if (playing && liveEq && !video) {
    /* Advance the local phase on this frame's own elapsed time, then
       ease it toward what the PC reports. Easing rather than snapping is
       what keeps the arc continuous: the cat completes every rise and
       fall instead of being yanked back part way through one. */
    uint32_t nowMs = millis();
    float dt = (float)(nowMs - catPhaseMs) / 1000.0f;
    catPhaseMs = nowMs;
    if (dt > 0.2f) dt = 0.2f;  /* a long gap means frames were missed */

    if (beatPeriodMs > 0) {
      catPhase += dt * 1000.0f / (float)beatPeriodMs;
      while (catPhase >= 1.0f) catPhase -= 1.0f;

      /* Shortest way round to the reported phase, so a wrap does not send
         it the long way about. */
      float err = (beatPhase / 100.0f) - catPhase;
      if (err > 0.5f) err -= 1.0f;
      if (err < -0.5f) err += 1.0f;
      catPhase += err * 0.07f;
      if (catPhase < 0.0f) catPhase += 1.0f;
      if (catPhase >= 1.0f) catPhase -= 1.0f;
    }

    /* Lowest on the beat, rising between: a head-bob dips on the beat
       rather than peaking on it, which is what made the old shape feel
       out of time even when the tempo was right. */
    lift_f = sin(catPhase * 3.14159f);
    if (lift_f < 0.0f) lift_f = 0.0f;
  }
  /* Rounded, not truncated: truncation capped the peak a pixel short,
     so the cat never reached the top of its own arc. */
  int16_t lift = (int16_t)(lift_f * 8.0f + 0.5f);

  /* The idle screen already gives the mascot the stage, so the perched
     one is skipped there rather than putting two cats on one screen. */
  if (!idle) {
    u8g2.setDrawColor(0);
    u8g2.drawBox(CAT_PERCH_X - 3, 40, SCREEN_W - CAT_PERCH_X + 3,
                 SCREEN_H - 40);
    u8g2.setDrawColor(1);

    /* Eyes widen on the landing, which reads as reacting to the beat. */
    uint8_t pose = video ? catPose(false, false)
                   : (playing && liveEq && lift_f < 0.25f)
                       ? CAT_HAPPY
                       : catPose(playing, false);
    drawCat(CAT_PERCH_X, 55 - lift, pose, u8g2_font_4x6_tf, 7);
  }
}

/* Whole gigabytes, for rows that have to share their line with the cat. */
static void fmtPairGBWhole(char *out, size_t n, float usedMB, float totalMB) {
  if (isnan(usedMB) || isnan(totalMB)) {
    snprintf(out, n, "--");
    return;
  }
  snprintf(out, n, "%d/%dGB", (int)(usedMB / 1024.0f + 0.5f),
           (int)(totalMB / 1024.0f + 0.5f));
}

/* Format a used/total pair held in MB as GB, or "--" if either is absent. */
static void fmtPairGB(char *out, size_t n, float usedMB, float totalMB) {
  if (isnan(usedMB) || isnan(totalMB)) {
    snprintf(out, n, "--");
    return;
  }
  char used[10], total[10];
  dtostrf(usedMB / 1024.0f, 0, 1, used);
  dtostrf(totalMB / 1024.0f, 0, 1, total);
  snprintf(out, n, "%s/%sGB", used, total);
}

/* One labelled row: name, a preformatted value, and a usage bar.
   Three rows share the band, so the pitch is 16 and bars are 5 tall. */
static void drawStatRow(uint8_t baseline, const char *label, const char *value,
                        float load) {
  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, baseline, label);
  u8g2.drawUTF8(24, baseline, value);

  /* An unknown load still draws the empty frame, so the row reads as a row
     rather than vanishing. */
  /* The fill is inset on every side. At a 3px fill inside a 5px frame it
     touched both borders, so the bar read as one solid blob instead of a
     level inside a track. */
  const uint8_t barY = baseline + 2;
  u8g2.drawFrame(2, barY, STAT_BAR_W, 6);
  if (!isnan(load)) {
    float pct = load;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    uint8_t w = (uint8_t)((pct / 100.0f) * (STAT_BAR_W - 4));
    if (w > 0) u8g2.drawBox(4, barY + 2, w, 2);
  }
}

/* Clock screen.
 *
 * The board has no RTC, so this is only meaningful while the PC is
 * feeding it -- which is exactly why the time is sent as finished strings
 * rather than a timestamp the device would have to keep running itself.
 * When the link goes stale the whole frame dims like any other screen,
 * which is the honest thing to show: a clock that might be wrong. */
static void drawClock() {
  if (frame.timeText[0] == 0) return;

  /* The cat gets the top right at the larger face. The seconds move down
     to the date line rather than sitting beside the hours, which is what
     left no room for it before. */
  drawCat(84, 10, catPose(strcmp(frame.state, "playing") == 0, false),
          u8g2_font_6x12_tf, 12);

  u8g2.setFont(u8g2_font_logisoso24_tn);
  int16_t x = 4;
  u8g2.drawUTF8(x, 48, frame.timeText);

  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(x + 1, 60, frame.dateText);
  uint16_t dw = u8g2.getUTF8Width(frame.dateText);
  u8g2.drawUTF8(x + dw + 8, 60, frame.secText);

  /* The board's own state, in the space beside the time. Nothing else
     reports this, and a display that can describe itself is worth the
     three lines it costs. */
  char info[20];
  u8g2.setFont(u8g2_font_4x6_tf);
  u8g2.drawVLine(82, 28, 30);

  snprintf(info, sizeof(info), "mcu %d%%", mcuLoad);
  u8g2.drawUTF8(86, 34, info);

  uint32_t up = millis() / 1000UL;
  if (up >= 3600UL) {
    snprintf(info, sizeof(info), "up %luh%02lu", up / 3600UL,
             (up % 3600UL) / 60UL);
  } else {
    snprintf(info, sizeof(info), "up %lum%02lus", up / 60UL, up % 60UL);
  }
  u8g2.drawUTF8(86, 42, info);

  snprintf(info, sizeof(info), "%dfps", fpsValue);
  u8g2.drawUTF8(86, 50, info);
}

/* Half a fan spins in from the right edge.
   Drawn centred on x=128 so exactly half of it falls off the panel; u8g2
   clips the rest, which costs nothing and reads as a fan tucked behind
   the bezel. Rate follows the fastest fan, idling slowly with no data so
   it never looks seized. */
static void drawHalfFan(int16_t rpm) {
  const int16_t cx = SCREEN_W;
  const int16_t cy = 38;
  const int16_t r = 21;

  static float angle = 0.0f;
  angle += (rpm > 0) ? (0.02f + (rpm / 2200.0f) * 0.22f) : 0.012f;
  if (angle > 6.2832f) angle -= 6.2832f;

  u8g2.drawCircle(cx, cy, r, U8G2_DRAW_ALL);
  u8g2.drawCircle(cx, cy, r - 4, U8G2_DRAW_ALL);
  u8g2.drawDisc(cx, cy, 3, U8G2_DRAW_ALL);
  for (uint8_t i = 0; i < 5; i++) {
    float a = angle + i * 1.2566f;
    u8g2.drawLine(cx + (int16_t)(cos(a) * 5), cy + (int16_t)(sin(a) * 5),
                  cx + (int16_t)(cos(a + 0.5f) * (r - 3)),
                  cy + (int16_t)(sin(a + 0.5f) * (r - 3)));
  }
}

static void drawStats() {
  char value[40];
  char tempStr[12];
  char pair[20];

  /* Spin rate comes from whichever unit the source reports. A percentage
     is scaled to the same range so the disc turns comparably either way. */
  int16_t fastest = 0;
  for (uint8_t i = 0; i < frame.fanCount; i++) {
    if (frame.fanRpm[i] > fastest) fastest = frame.fanRpm[i];
    if (frame.fanPct[i] > 0 && (frame.fanPct[i] * 22) > fastest) {
      fastest = frame.fanPct[i] * 22;
    }
  }

  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, META_BASELINE, "system");

  /* Fan speed rides in the header rather than taking a row of its own.
     A bar would imply a percentage, which RPM is not, and the spinning
     disc already carries the same reading visually. */
  if (frame.fanCount == 0) {
    u8g2.drawUTF8(52, META_BASELINE, "fan --");
  } else if (frame.fanRpm[0] >= 0) {
    snprintf(value, sizeof(value), "fan %drpm", fastest);
    u8g2.drawUTF8(52, META_BASELINE, value);
  } else {
    /* A GPU reports its fan as a percentage. Zero is a real reading:
       modern cards stop the fan entirely when they are cool. */
    snprintf(value, sizeof(value), "%s fan %d%%", frame.fanName[0],
             frame.fanPct[0]);
    u8g2.drawUTF8(52, META_BASELINE, value);
  }
  u8g2.drawHLine(0, RULE_Y, SCREEN_W);

  /* Three rows on a 16px pitch. Dropping the fourth row bought back the
     breathing space the screen was missing. */
  fmtNum(tempStr, sizeof(tempStr), frame.cpuTemp, 0, "C");
  if (isnan(frame.cpuClock)) {
    snprintf(value, sizeof(value), "%s  --", tempStr);
  } else {
    char ghz[10];
    dtostrf(frame.cpuClock / 1000.0f, 0, 2, ghz);
    snprintf(value, sizeof(value), "%s  %sGHz", tempStr, ghz);
  }
  drawStatRow(24, "cpu", value, frame.cpuLoad);

  fmtNum(tempStr, sizeof(tempStr), frame.gpuTemp, 0, "C");
  fmtPairGBWhole(pair, sizeof(pair), frame.vramUsed, frame.vramTotal);
  snprintf(value, sizeof(value), "%s  %s", tempStr, pair);
  drawStatRow(40, "gpu", value, frame.gpuLoad);

  fmtPairGB(pair, sizeof(pair), frame.ramUsed, frame.ramTotal);
  drawStatRow(56, "ram", pair, frame.ramPercent);

  drawHalfFan(fastest);
}

/* Overnight face: just the time, dimmed, walking slowly around the panel.
 *
 * The board stays powered when the PC shuts down, so this is the
 * difference between a dark panel and a working desk clock. Nothing is
 * static: the digits step to a new position every minute, which is what
 * keeps eight hours of the same glyphs from etching into the display. */
static void drawNightClock() {
  RTCTime now;
  if (!RTC.getTime(now)) return;

  char buf[8];
  snprintf(buf, sizeof(buf), "%02d:%02d", now.getHour(), now.getMinutes());

  uint8_t m = now.getMinutes();
  int16_t ox = (int16_t)((m % 7) * 4) - 12;
  int16_t oy = (int16_t)(((m / 7) % 5) * 3) - 6;

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_logisoso24_tn);
  uint16_t w = u8g2.getUTF8Width(buf);
  u8g2.drawUTF8((SCREEN_W - w) / 2 + ox, 42 + oy, buf);
  u8g2.sendBuffer();
}

static void drawWaiting() {
  u8g2.setFont(u8g2_font_6x12_tf);
  u8g2.drawUTF8(2, 26, "catsole");
  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, 40, "waiting for the PC");

  /* A slow sweep, so an idle device never looks like a crashed one. */
  uint8_t x = (millis() / 24) % (SCREEN_W + 24);
  if (x < SCREEN_W) u8g2.drawBox(x, 50, 10, 2);
}

static void drawStaleBadge() {
  uint32_t secs = (millis() - lastFrameMs) / 1000;
  char badge[20];
  snprintf(badge, sizeof(badge), "no link %lus", (unsigned long)secs);

  u8g2.setFont(u8g2_font_4x6_tf);
  uint16_t w = u8g2.getUTF8Width(badge) + 4;

  /* Blink slowly: present enough to notice, not so much it nags. */
  if ((millis() / 700) % 2 == 0) {
    u8g2.setDrawColor(1);
    u8g2.drawBox(0, 0, w, 8);
    u8g2.setDrawColor(0);
    u8g2.drawUTF8(2, 6, badge);
    u8g2.setDrawColor(1);
  }
}

static void render() {
  uint32_t t0 = micros();

  u8g2.clearBuffer();

  switch (linkState) {
    case LINK_BOOT:
    case LINK_WAITING:
      drawWaiting();
      break;

    case LINK_LIVE:
    case LINK_STALE:
      if (strcmp(frame.mode, "stats") == 0) drawStats();
      else if (strcmp(frame.mode, "clock") == 0) drawClock();
      else drawLyrics();
      break;
  }

  if (linkState == LINK_STALE) {
    /* Dim the last known frame rather than blanking it: the content is
       still true, just old, and a dark screen reads as a dead device. */
    dimBuffer();
    drawStaleBadge();
  }

  uint32_t t1 = micros();
  u8g2.sendBuffer();
  uint32_t t2 = micros();

  perfRenderSum += (t1 - t0);
  perfSendSum += (t2 - t1);
  perfFrames++;

  fpsFrames++;
  fpsBusyUs += (t2 - t0);
  uint32_t windowMs = millis() - fpsWindowMs;
  if (windowMs >= 1000) {
    fpsValue = (uint8_t)((fpsFrames * 1000UL) / windowMs);
    /* Busy microseconds against the window, as a percentage. */
    uint32_t pct = fpsBusyUs / (windowMs * 10UL);
    mcuLoad = (uint8_t)(pct > 100 ? 100 : pct);
    fpsFrames = 0;
    fpsBusyUs = 0;
    fpsWindowMs = millis();
  }
}

/* ==================================================================== *
 * boot sequence
 *
 * Opens with a full-white flash, which doubles as the panel check: if
 * nothing appears at all it is wiring, not configuration. Then the cat
 * rises into place, blinks, and the name types in beside it.
 * ==================================================================== */
static void bootAnimation() {
  const char *title = "catsole";
  const uint8_t titleLen = 7;
  const int16_t catX = 43;  /* 7 chars at 6x12 is 42px, centred */
  const int16_t restY = 20;
  const uint8_t lineH = 11;

  u8g2.clearBuffer();
  u8g2.drawBox(0, 0, SCREEN_W, SCREEN_H);
  u8g2.sendBuffer();
  delay(60);

  /* Rise from below the bottom edge, easing out so it settles. */
  uint32_t start = millis();
  for (;;) {
    uint32_t t = millis() - start;
    if (t >= 650) break;
    float q = 1.0f - (t / 650.0f);
    float eased = 1.0f - (q * q * q);
    int16_t y = (int16_t)((SCREEN_H + 14) - eased * ((SCREEN_H + 14) - restY));
    u8g2.clearBuffer();
    drawCat(catX, y, CAT_IDLE, u8g2_font_6x12_tf, lineH);
    u8g2.sendBuffer();
  }

  /* Two blinks, unevenly spaced so it reads as alive rather than timed. */
  const uint16_t blinkHold[2] = {110, 90};
  const uint16_t blinkGap[2] = {170, 320};
  for (uint8_t i = 0; i < 2; i++) {
    u8g2.clearBuffer();
    drawCat(catX, restY, CAT_BLINK, u8g2_font_6x12_tf, lineH);
    u8g2.sendBuffer();
    delay(blinkHold[i]);
    u8g2.clearBuffer();
    drawCat(catX, restY, CAT_IDLE, u8g2_font_6x12_tf, lineH);
    u8g2.sendBuffer();
    delay(blinkGap[i]);
  }

  /* Name types in, and the cat perks up once it is finished. */
  char buf[16];
  for (uint8_t n = 1; n <= titleLen; n++) {
    memcpy(buf, title, n);
    buf[n] = 0;
    u8g2.clearBuffer();
    drawCat(catX, restY, n >= titleLen ? CAT_HAPPY : CAT_IDLE,
            u8g2_font_6x12_tf, lineH);
    u8g2.setFont(u8g2_font_7x13B_tf);
    uint16_t w = u8g2.getUTF8Width(buf);
    u8g2.drawUTF8((SCREEN_W - w) / 2, 62, buf);
    u8g2.sendBuffer();
    delay(65);
  }
  delay(600);
}

/* ==================================================================== *
 * setup / loop
 * ==================================================================== */

void setup() {
  Serial.begin(115200);

  resetFrame();
  for (uint8_t i = 0; i < EQ_BARS; i++) {
    eqHeight[i] = 1.0f;
    eqPhase[i] = (uint16_t)random(0, 628);
  }

  /* Must be set before begin(), which is when the init sequence is sent. */
  RTC.begin();
  rtcReady = RTC.isRunning();

  u8g2.setBusClock(DISPLAY_BUS_HZ);
  u8g2.begin();
  u8g2.setFontMode(1);
  u8g2.enableUTF8Print();

  bootAnimation();

  linkState = LINK_WAITING;
  lastFrameMs = millis();
  sendHello();
  lastHelloMs = millis();
}

void loop() {
  pollSerial();

  uint32_t now = millis();

  /* Keep announcing ourselves until the PC answers, so starting the
     service after the board is already powered still connects. */
  if (!everReceived && now - lastHelloMs >= HELLO_INTERVAL_MS) {
    sendHello();
    lastHelloMs = now;
  }

  if (!everReceived) {
    linkState = LINK_WAITING;
  } else if (now - lastFrameMs > STALE_AFTER_MS) {
    linkState = LINK_STALE;
  } else {
    linkState = LINK_LIVE;
  }

  /* With a clock set, a PC that has gone away leaves a desk clock rather
     than a dark panel. The board is powered either way. */
  bool quiet = (now - lastFrameMs) > SLEEP_AFTER_MS;

  if (quiet && rtcReady) {
    if (!nightMode) {
      nightMode = true;
      displayAsleep = false;
      u8g2.setPowerSave(0);
      u8g2.setContrast(NIGHT_CONTRAST);
      sendSleepState(false);
      lastDrawMs = 0;
    }
    /* Once a second is plenty for a clock showing minutes, and it leaves
       the panel idle the rest of the time. */
    if (now - lastDrawMs >= 1000) {
      drawNightClock();
      lastDrawMs = now;
    }
    return;
  }

  if (nightMode) {
    nightMode = false;
    u8g2.setContrast(255);
  }

  bool shouldSleep = quiet;
  if (shouldSleep != displayAsleep) {
    displayAsleep = shouldSleep;
    u8g2.setPowerSave(displayAsleep ? 1 : 0);
    sendSleepState(displayAsleep);
    if (!displayAsleep) lastDrawMs = 0;  /* redraw immediately on waking */
  }

  /* Nothing below here matters while the panel is off. */
  if (displayAsleep) return;

  if (now - lastMarqueeMs >= 40) {
    marqueeOffset++;
    if (marqueeOffset > 16000) marqueeOffset = 0;
    lastMarqueeMs = now;
  }

  if (now - lastDrawMs >= FRAME_INTERVAL_MS) {
    /* Anchor to the intended cadence, not to when drawing finished.
       Stamping this afterwards made every cycle "render time + interval",
       which at a 28ms frame turned a 33ms target into 61ms -- half the
       frame rate, for no reason. */
    lastDrawMs = now;
    render();
  }
}
