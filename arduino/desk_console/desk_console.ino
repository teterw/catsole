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

/* ---- screen geometry ---------------------------------------------- */
static const uint8_t SCREEN_W = 128;
static const uint8_t SCREEN_H = 64;
static const uint8_t META_BASELINE = 7;
static const uint8_t RULE_Y = 10;
static const uint8_t EQ_BARS = 12;

/* ---- link state ---------------------------------------------------- */
enum LinkState { LINK_BOOT, LINK_WAITING, LINK_LIVE, LINK_STALE };

static LinkState linkState = LINK_BOOT;
static uint32_t lastFrameMs = 0;
static uint32_t lastHelloMs = 0;
static bool everReceived = false;

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
};

static Frame frame;

/* ---- animation ------------------------------------------------------ */
static uint32_t lastDrawMs = 0;
static int16_t marqueeOffset = 0;
static uint32_t lastMarqueeMs = 0;
static float eqHeight[EQ_BARS];
static uint16_t eqPhase[EQ_BARS];

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

  if (strcmp(type, "frame") != 0) return;

  copyField(frame.mode, sizeof(frame.mode), doc["mode"] | "lyrics");
  copyField(frame.state, sizeof(frame.state), doc["state"] | "idle");
  copyField(frame.lyr, sizeof(frame.lyr), doc["lyr"] | "none");
  copyField(frame.meta, sizeof(frame.meta), doc["meta"] | "");
  copyField(frame.mainText, sizeof(frame.mainText), doc["main"] | "");
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
#define MAX_WRAP_LINES 4
#define MAX_WRAP_CHARS 48

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

/* Draws the main line, vertically centred, at the largest size that fits. */
static void drawMainText(const char *text, uint8_t boxW) {
  if (text[0] == 0) return;

  uint8_t chosen = MAIN_STYLE_COUNT - 1;
  for (uint8_t i = 0; i < MAIN_STYLE_COUNT; i++) {
    u8g2.setFont(MAIN_STYLES[i].font);
    if (wrapText(text, boxW, MAIN_STYLES[i].maxLines)) {
      chosen = i;
      break;
    }
  }

  /* If nothing fit, wrapBuf already holds the smallest font's attempt. */
  const TextStyle style = MAIN_STYLES[chosen];
  u8g2.setFont(style.font);

  uint8_t total = wrapCount * style.lineH;
  uint8_t bandH = BAND_BOTTOM - BAND_TOP;
  uint8_t top = BAND_TOP + (bandH > total ? (uint8_t)((bandH - total) / 2) : 0);

  for (uint8_t i = 0; i < wrapCount; i++) {
    u8g2.drawUTF8(2, top + (i + 1) * style.lineH - 3, wrapBuf[i]);
  }
}

static void drawEqualizer(bool active) {
  const uint8_t barW = 3;
  const uint8_t gap = 1;
  const uint8_t totalW = EQ_BARS * barW + (EQ_BARS - 1) * gap;
  const uint8_t x0 = (SCREEN_W - totalW) / 2;

  for (uint8_t i = 0; i < EQ_BARS; i++) {
    float target;
    if (active) {
      /* Cosmetic: there is no microphone on this build, so the bars are
         driven by offset sines rather than pretending to follow audio. */
      float phase = (millis() / 260.0f) + (eqPhase[i] / 100.0f);
      target = 2.0f + fabs(sin(phase)) * 9.0f;
    } else {
      target = 1.0f;  /* decay to a flat line when idle or paused */
    }
    eqHeight[i] += (target - eqHeight[i]) * (active ? 0.25f : 0.12f);

    uint8_t h = (uint8_t)max(1.0f, eqHeight[i]);
    u8g2.drawBox(x0 + i * (barW + gap), SCREEN_H - h, barW, h);
  }
}

static void drawLyrics() {
  u8g2.setFont(u8g2_font_5x7_tf);
  drawMarquee(frame.meta, META_BASELINE, SCREEN_W - 4);
  u8g2.drawHLine(0, RULE_Y, SCREEN_W);

  bool idle = strcmp(frame.state, "idle") == 0;

  if (idle) {
    u8g2.setFont(u8g2_font_6x12_tf);
    u8g2.drawUTF8(2, 34, "nothing playing");
  } else if (frame.mainText[0] != 0) {
    drawMainText(frame.mainText, SCREEN_W - 4);

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

static void drawStatRow(uint8_t baseline, const char *label, float temp,
                        float clockOrVramUsed, float vramTotal, float load,
                        bool isCpu) {
  char buf[40];
  char tempStr[12];
  char rightStr[20];

  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, baseline, label);

  fmtNum(tempStr, sizeof(tempStr), temp, 0, "C");

  if (isCpu) {
    if (isnan(clockOrVramUsed)) {
      strcpy(rightStr, "--");
    } else {
      char ghz[10];
      dtostrf(clockOrVramUsed / 1000.0f, 0, 2, ghz);
      snprintf(rightStr, sizeof(rightStr), "%sGHz", ghz);
    }
  } else {
    if (isnan(clockOrVramUsed) || isnan(vramTotal)) {
      strcpy(rightStr, "--");
    } else {
      char used[10], total[10];
      dtostrf(clockOrVramUsed / 1024.0f, 0, 1, used);
      dtostrf(vramTotal / 1024.0f, 0, 1, total);
      snprintf(rightStr, sizeof(rightStr), "%s/%sGB", used, total);
    }
  }

  snprintf(buf, sizeof(buf), "%s  %s", tempStr, rightStr);
  u8g2.drawUTF8(24, baseline, buf);

  /* Usage bar. An unknown load draws the empty frame, so the row still
     reads as a row rather than vanishing. */
  const uint8_t barY = baseline + 3;
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
  u8g2.setFont(u8g2_font_5x7_tf);
  u8g2.drawUTF8(2, META_BASELINE, "system");
  u8g2.drawHLine(0, RULE_Y, SCREEN_W);

  drawStatRow(26, "cpu", frame.cpuTemp, frame.cpuClock, NAN, frame.cpuLoad, true);
  drawStatRow(48, "gpu", frame.gpuTemp, frame.vramUsed, frame.vramTotal,
              frame.gpuLoad, false);
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

  u8g2.sendBuffer();
}

/* ==================================================================== *
 * boot self-test
 *
 * Makes the correct DISPLAY_VARIANT obvious on first upload: the border
 * must be crisp against the panel edge, the checkerboard must read as an
 * even grey rather than as bands, and the fill must be uniform.
 * ==================================================================== */
static void selfTest() {
  u8g2.clearBuffer();
  u8g2.drawBox(0, 0, SCREEN_W, SCREEN_H);
  u8g2.sendBuffer();
  delay(250);

  u8g2.clearBuffer();
  u8g2.drawFrame(0, 0, SCREEN_W, SCREEN_H);
  for (uint8_t y = 16; y < 48; y++) {
    for (uint8_t x = 8 + (y & 1); x < 120; x += 2) u8g2.drawPixel(x, y);
  }
  u8g2.sendBuffer();
  delay(600);

  u8g2.clearBuffer();
  u8g2.setFont(u8g2_font_6x12_tf);
  u8g2.drawUTF8(2, 20, "desk-console");
  u8g2.setFont(u8g2_font_5x7_tf);
  char line[32];
  snprintf(line, sizeof(line), "fw %s  variant %d", FIRMWARE_VERSION,
           DISPLAY_VARIANT);
  u8g2.drawUTF8(2, 34, line);
  u8g2.sendBuffer();
  delay(900);
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

  selfTest();

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

  if (now - lastMarqueeMs >= 40) {
    marqueeOffset++;
    if (marqueeOffset > 16000) marqueeOffset = 0;
    lastMarqueeMs = now;
  }

  if (now - lastDrawMs >= FRAME_INTERVAL_MS) {
    render();
    lastDrawMs = millis();
  }
}
