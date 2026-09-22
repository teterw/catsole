/*
 * desk-console firmware
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
 *        {"t":"frame","mode":"stats","cpu":{...},"gpu":{...}}
 *        {"t":"ping"}
 *   out  {"t":"hello","fw":"1.0.0","variant":0}
 */

#include <Arduino.h>
#include <SPI.h>
#include <U8g2lib.h>
#include <ArduinoJson.h>

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

/* ---- link state ---------------------------------------------------- */
enum LinkState { LINK_BOOT, LINK_WAITING, LINK_LIVE, LINK_STALE };

static LinkState linkState = LINK_BOOT;
static uint32_t lastFrameMs = 0;
static uint32_t lastHelloMs = 0;
static bool everReceived = false;
static bool displayAsleep = false;

/* Frame timing, so headroom questions can be answered with numbers rather
   than estimates. Reported by the "perf" command, which also resets. */
static uint32_t perfRenderSum = 0;
static uint32_t perfSendSum = 0;
static uint32_t perfFrames = 0;
static uint32_t perfSince = 0;


/* ---- current frame -------------------------------------------------- */
struct Frame {
  char mode[8];
  char meta[72];
  char mainText[104];
  char lyr[8];
  char state[10];
  uint8_t eq;
  uint32_t holdMs;
  uint32_t receivedAtMs;

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

/* A lyric line that swaps instantly is jarring at this size, so the
   outgoing line is kept around long enough to slide it out while the new
   one slides in beneath it. */
static const uint16_t TRANSITION_MS = 260;
static char prevMainText[104];
static uint32_t transitionStartMs = 0;

/* Set when the main line changes; the layout is rebuilt on the next draw
   rather than inside the serial handler, which keeps the text-layout types
   out of the parser's scope. */
static bool wrapDirty = true;

#define MAX_WRAP_LINES 4
#define MAX_WRAP_CHARS 48

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
};

static WrappedText wrapCurrent;
static WrappedText wrapPrev;

/* ---- serial receive -------------------------------------------------- */
static char rxBuf[640];
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
    copyField(prevMainText, sizeof(prevMainText), frame.mainText);
    transitionStartMs = millis();
    wrapDirty = true;
  }
  copyField(frame.mainText, sizeof(frame.mainText), incoming);
  frame.eq = doc["eq"] | 0;
  frame.holdMs = doc["hold_ms"] | 0UL;
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

static void drawMarquee(const char *text, uint8_t baseline, uint8_t boxW) {
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
  uint8_t top = (baseline >= 8) ? (uint8_t)(baseline - 8) : 0;
  uint8_t bottom = (uint8_t)min((int)baseline + 3, (int)SCREEN_H);

  u8g2.setClipWindow(0, top, boxW + 2, bottom);
  u8g2.drawUTF8(2 - offset, baseline, text);
  u8g2.drawUTF8(2 - offset + span, baseline, text);
  u8g2.setMaxClipWindow();
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

      if (curLen + (curLen ? 1 : 0) + wordLen >= MAX_WRAP_CHARS) {
        p = wordStart;
        break;
      }

      uint16_t pos = curLen;
      memcpy(cand, cur, curLen);
      if (curLen) cand[pos++] = ' ';
      memcpy(cand + pos, wordStart, wordLen);
      pos += wordLen;
      cand[pos] = 0;

      if (u8g2.getUTF8Width(cand) <= boxW) {
        memcpy(cur, cand, pos + 1);
        curLen = pos;
        while (*p == ' ') p++;
      } else {
        p = wordStart;  /* Does not fit: this word starts the next line. */
        break;
      }
    }

    if (curLen == 0) {
      /* A single word wider than the panel. Break it mid-word, otherwise
         the loop cannot advance and nothing would ever be drawn. */
      const char *wordStart = p;
      uint16_t fit = 1;
      for (uint16_t i = 1; wordStart[i] && i < MAX_WRAP_CHARS - 1; i++) {
        memcpy(cand, wordStart, i);
        cand[i] = 0;
        if (u8g2.getUTF8Width(cand) <= boxW) fit = i;
        else break;
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
  uint8_t maxLines;
  uint8_t lineH;
};

/* Largest first. The first style that fits the whole line wins. */
static const TextStyle MAIN_STYLES[] = {
    {u8g2_font_helvB10_tf, 2, 13},
    {u8g2_font_6x12_tf, 3, 11},
    {u8g2_font_5x7_tf, 4, 8},
};
static const uint8_t MAIN_STYLE_COUNT = 3;

/* The band between the rule and the equalizer row. */
static const uint8_t BAND_TOP = 14;
static const uint8_t BAND_BOTTOM = 50;


/* Lay text out once. Call when the text changes, not when drawing. */
static void prepareWrap(const char *text, uint8_t boxW, WrappedText &out) {
  out.count = 0;
  out.styleIndex = 0;
  if (text == NULL || text[0] == 0) return;

  uint8_t chosen = MAIN_STYLE_COUNT - 1;
  for (uint8_t i = 0; i < MAIN_STYLE_COUNT; i++) {
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
  }
}

/* Draws a prepared layout, vertically centred. yShift moves the whole
   block, which is what the slide transition uses; the caller clips to the
   band so shifted text cannot escape it. */
static void drawWrapped(const WrappedText &wrapped, int16_t yShift) {
  if (wrapped.count == 0) return;

  const TextStyle style = MAIN_STYLES[wrapped.styleIndex];
  u8g2.setFont(style.font);

  uint8_t total = wrapped.count * style.lineH;
  uint8_t bandH = BAND_BOTTOM - BAND_TOP;
  uint8_t top = BAND_TOP + (bandH > total ? (uint8_t)((bandH - total) / 2) : 0);

  for (uint8_t i = 0; i < wrapped.count; i++) {
    int16_t y = (int16_t)(top + (i + 1) * style.lineH - 3) + yShift;
    /* Skip lines that have travelled clear of the band. */
    if (y < (int16_t)BAND_TOP - 20 || y > (int16_t)BAND_BOTTOM + 20) continue;
    u8g2.drawUTF8(2, y, wrapped.lines[i]);
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

static const char *const CAT_ART[CAT_POSE_COUNT][3] = {
    /* Ears sit at the outer corners, over the sides of the head. Tucked
       inboard they landed directly above the eyes and read as wrong. */
    {"/\\___/\\", "( o.o )", " > ^ < "},  /* idle    */
    {"/\\___/\\", "( -.- )", " > ^ < "},  /* blink   */
    {"/\\___/\\", "( ^.^ )", " > ~ < "},  /* music   */
    {"/\\___/\\", "( u.u )", " > _ < "},  /* asleep  */
    {"/\\___/\\", "( -.o )", " > ^ < "},  /* squint  */
};

static void drawCat(int16_t x, int16_t y, uint8_t pose, const uint8_t *font,
                    uint8_t lineH) {
  if (pose >= CAT_POSE_COUNT) pose = CAT_IDLE;
  u8g2.setFont(font);
  for (uint8_t row = 0; row < 3; row++) {
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

static void drawLyrics() {
  u8g2.setFont(u8g2_font_5x7_tf);
  drawMarquee(frame.meta, META_BASELINE, SCREEN_W - 4);
  u8g2.drawHLine(0, RULE_Y, SCREEN_W);

  bool idle = strcmp(frame.state, "idle") == 0;

  if (idle) {
    /* Nothing to show, so the mascot gets the space. */
    drawCat(6, 24, catPose(false, false), u8g2_font_6x12_tf, 11);
    u8g2.setFont(u8g2_font_5x7_tf);
    u8g2.drawUTF8(56, 34, "nothing");
    u8g2.drawUTF8(56, 44, "playing");
  } else if (frame.mainText[0] != 0) {
    /* Rebuild the layout only when the line actually changed. The outgoing
       line's layout is already finished, so it is copied rather than
       recomputed. */
    if (wrapDirty) {
      wrapPrev = wrapCurrent;
      prepareWrap(frame.mainText, SCREEN_W - 4, wrapCurrent);
      wrapDirty = false;
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
      drawWrapped(wrapPrev, (int16_t)(-eased * travel));
      drawWrapped(wrapCurrent, (int16_t)((1.0f - eased) * travel));
      u8g2.setMaxClipWindow();
    } else {
      drawWrapped(wrapCurrent, 0);
    }

    if (strcmp(frame.lyr, "synced") == 0) {
      /* Hairline showing how much of this line's window remains. When it
         completes and nothing has replaced the line, the frame is late. */
      if (frame.holdMs > 0) {
        uint32_t elapsed = millis() - frame.receivedAtMs;
        if (elapsed < frame.holdMs) {
          uint8_t w = (uint8_t)(((frame.holdMs - elapsed) * (SCREEN_W - 4)) /
                                frame.holdMs);
          u8g2.drawHLine(2, RULE_Y + 2, w);
        }
      }
    } else if (wrapCount <= 2) {
      /* Only label the fallback when the title left room for it. */
      u8g2.setFont(u8g2_font_4x6_tf);
      u8g2.drawUTF8(2, BAND_BOTTOM, strcmp(frame.lyr, "plain") == 0
                                        ? "lyrics not timed"
                                        : "no lyrics found");
    }
  }

  drawEqualizer(frame.eq == 1 && strcmp(frame.state, "playing") == 0);
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

/* One labelled row: name, a preformatted value string, and a usage bar. */
static void drawStatRow(uint8_t baseline, const char *label, const char *value,
                        float load) {
  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, baseline, label);
  u8g2.drawUTF8(24, baseline, value);

  /* An unknown load still draws the empty frame, so the row reads as a row
     rather than vanishing. */
  const uint8_t barY = baseline + 2;
  u8g2.drawFrame(2, barY, SCREEN_W - 4, 6);
  if (!isnan(load)) {
    float pct = load;
    if (pct < 0) pct = 0;
    if (pct > 100) pct = 100;
    uint8_t w = (uint8_t)((pct / 100.0f) * (SCREEN_W - 8));
    if (w > 0) u8g2.drawBox(4, barY + 2, w, 2);
  }
}

static void drawStats() {
  char value[40];
  char tempStr[12];
  char pair[20];

  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, META_BASELINE, "system");
  u8g2.drawHLine(0, RULE_Y, SCREEN_W);

  /* Three rows on a 17px pitch: baselines at 20, 37 and 54 put the last
     bar at 56..61, just inside the panel. */
  fmtNum(tempStr, sizeof(tempStr), frame.cpuTemp, 0, "C");
  if (isnan(frame.cpuClock)) {
    snprintf(value, sizeof(value), "%s  --", tempStr);
  } else {
    char ghz[10];
    dtostrf(frame.cpuClock / 1000.0f, 0, 2, ghz);
    snprintf(value, sizeof(value), "%s  %sGHz", tempStr, ghz);
  }
  drawStatRow(20, "cpu", value, frame.cpuLoad);

  fmtNum(tempStr, sizeof(tempStr), frame.gpuTemp, 0, "C");
  fmtPairGB(pair, sizeof(pair), frame.vramUsed, frame.vramTotal);
  snprintf(value, sizeof(value), "%s  %s", tempStr, pair);
  drawStatRow(37, "gpu", value, frame.gpuLoad);

  /* RAM has no temperature, so the pair gets the whole width. */
  fmtPairGB(pair, sizeof(pair), frame.ramUsed, frame.ramTotal);
  drawStatRow(54, "ram", pair, frame.ramPercent);
}

static void drawWaiting() {
  u8g2.setFont(u8g2_font_6x12_tf);
  u8g2.drawUTF8(2, 26, "desk-console");
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
  const int16_t catX = 43;
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

  /* Blank the panel once the PC has been gone a while, and bring it back
     the moment anything arrives. Driven by time since the last frame, so
     it covers both a PC that went away and one that never showed up. */
  bool shouldSleep = (now - lastFrameMs) > SLEEP_AFTER_MS;
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
