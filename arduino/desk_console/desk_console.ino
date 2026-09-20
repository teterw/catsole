/*
 * desk-console firmware
 * Arduino UNO R4 WiFi + SSD1309 128x64 OLED (hardware SPI) + PN532 (I2C)
 *
 * This board is a renderer, not a decision-maker. It owns animation, link
 * state and tap classification. Everything about what to show -- track
 * metadata, lyric timing, sensor values -- arrives from the PC as finished
 * strings and numbers over USB CDC.
 *
 * No WiFi is used. The radio stays off; all communication is Serial.
 *
 * Protocol (newline-delimited JSON):
 *   in   {"t":"frame","mode":"lyrics","meta":"...","main":"...","hold_ms":3200,"eq":1,"state":"playing","lyr":"synced"}
 *        {"t":"frame","mode":"stats","cpu":{...},"gpu":{...}}
 *        {"t":"ping"}
 *   out  {"t":"hello","fw":"1.0.0"}
 *        {"t":"tap","uid":"04A2B3C4","kind":"short"|"hold"}
 */

#include <Arduino.h>
#include <SPI.h>
#include <Wire.h>
#include <U8g2lib.h>
#include <ArduinoJson.h>
#include <PN532_I2C.h>
#include <PN532.h>

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

PN532_I2C pn532i2c(Wire);
PN532 nfc(pn532i2c);

/* ---- timing ------------------------------------------------------- */
static const uint16_t FRAME_INTERVAL_MS = 33;   /* ~30fps */
static const uint32_t STALE_AFTER_MS = 4000;    /* link considered dead */
static const uint32_t HELLO_INTERVAL_MS = 1500; /* until first frame lands */
static const uint16_t HOLD_THRESHOLD_MS = 1500;
static const uint16_t NFC_TIMEOUT_MS = 50;      /* short: the loop must keep drawing */
static const uint8_t NFC_MISS_LIMIT = 3;        /* debounce flaky reads */

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

/* ---- NFC ------------------------------------------------------------ */
static bool nfcReady = false;
static bool tagPresent = false;
static bool holdFired = false;
static uint8_t missCount = 0;
static uint32_t tagSinceMs = 0;
static uint32_t lastNfcPollMs = 0;
static char tagUid[21] = {0};

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

static void sendTap(const char *uid, const char *kind) {
  Serial.print(F("{\"t\":\"tap\",\"uid\":\""));
  Serial.print(uid);
  Serial.print(F("\",\"kind\":\""));
  Serial.print(kind);
  Serial.println(F("\"}"));
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
 * NFC
 * ==================================================================== */

static void uidToHex(const uint8_t *uid, uint8_t len, char *out, size_t size) {
  size_t pos = 0;
  for (uint8_t i = 0; i < len && pos + 2 < size; i++) {
    static const char digits[] = "0123456789ABCDEF";
    out[pos++] = digits[(uid[i] >> 4) & 0x0F];
    out[pos++] = digits[uid[i] & 0x0F];
  }
  out[pos] = 0;
}

static void pollNfc() {
  if (!nfcReady) return;

  uint8_t uid[7] = {0};
  uint8_t uidLen = 0;

  /* This call blocks for its whole timeout, which is why the timeout is
     short: the marquee and equalizer must keep moving. */
  bool found = nfc.readPassiveTargetID(PN532_MIFARE_ISO14443A, uid, &uidLen,
                                       NFC_TIMEOUT_MS);

  if (found && uidLen > 0) {
    missCount = 0;
    if (!tagPresent) {
      tagPresent = true;
      holdFired = false;
      tagSinceMs = millis();
      uidToHex(uid, uidLen, tagUid, sizeof(tagUid));
    }
    /* Fire the hold while the tag is still down, so the feedback ring
       completing and the action happening are the same moment. */
    if (!holdFired && (millis() - tagSinceMs) >= HOLD_THRESHOLD_MS) {
      holdFired = true;
      sendTap(tagUid, "hold");
    }
    return;
  }

  if (!tagPresent) return;

  /* PN532 reads drop out intermittently while a tag is still resting on
     the coil, so require several consecutive misses before calling it a
     release. */
  if (++missCount < NFC_MISS_LIMIT) return;

  tagPresent = false;
  missCount = 0;
  if (!holdFired) sendTap(tagUid, "short");
  holdFired = false;
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
  u8g2.setClipWindow(0, baseline - 8, boxW + 2, baseline + 3);
  u8g2.drawUTF8(2 - offset, baseline, text);
  u8g2.drawUTF8(2 - offset + span, baseline, text);
  u8g2.setMaxClipWindow();
}

/* Wrap into at most two lines, breaking on spaces. */
static void drawWrapped(const char *text, uint8_t topBaseline, uint8_t lineH,
                        uint8_t boxW) {
  if (text[0] == 0) return;

  char line[sizeof(frame.mainText)];
  uint16_t len = strlen(text);

  if (u8g2.getUTF8Width(text) <= boxW) {
    u8g2.drawUTF8(2, topBaseline + lineH / 2, text);
    return;
  }

  /* Find the last space that still fits on the first line. */
  uint16_t best = 0;
  for (uint16_t i = 0; i < len; i++) {
    if (text[i] != ' ') continue;
    memcpy(line, text, i);
    line[i] = 0;
    if (u8g2.getUTF8Width(line) <= boxW) best = i;
    else break;
  }

  if (best == 0) {
    /* One very long word: hard-truncate rather than overflow the band. */
    memcpy(line, text, sizeof(line) - 1);
    line[sizeof(line) - 1] = 0;
    while (strlen(line) > 1 && u8g2.getUTF8Width(line) > boxW) {
      line[strlen(line) - 1] = 0;
    }
    u8g2.drawUTF8(2, topBaseline, line);
    return;
  }

  memcpy(line, text, best);
  line[best] = 0;
  u8g2.drawUTF8(2, topBaseline, line);

  const char *rest = text + best + 1;
  copyField(line, sizeof(line), rest);
  while (strlen(line) > 1 && u8g2.getUTF8Width(line) > boxW) {
    line[strlen(line) - 1] = 0;
  }
  u8g2.drawUTF8(2, topBaseline + lineH, line);
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

static void drawHoldRing() {
  if (!tagPresent || holdFired) return;
  uint32_t held = millis() - tagSinceMs;
  if (held > HOLD_THRESHOLD_MS) held = HOLD_THRESHOLD_MS;
  uint8_t width = (uint8_t)((held * (SCREEN_W - 4)) / HOLD_THRESHOLD_MS);
  u8g2.drawBox(2, 0, width, 2);
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
    if (strcmp(frame.lyr, "synced") == 0) {
      u8g2.setFont(u8g2_font_helvB10_tf);
      drawWrapped(frame.mainText, 28, 13, SCREEN_W - 4);

      /* Hairline showing how much of this line's window remains. When it
         completes and nothing has replaced the line, the frame is late. */
      if (frame.holdMs > 0) {
        uint32_t elapsed = millis() - frame.receivedAtMs;
        if (elapsed < frame.holdMs) {
          uint8_t w = (uint8_t)(((frame.holdMs - elapsed) * (SCREEN_W - 4)) /
                                frame.holdMs);
          u8g2.drawHLine(2, 13, w);
        }
      }
    } else {
      /* No timing available, so the track itself is the headline. */
      u8g2.setFont(u8g2_font_helvB10_tf);
      drawWrapped(frame.mainText, 28, 13, SCREEN_W - 4);
      u8g2.setFont(u8g2_font_4x6_tf);
      u8g2.drawUTF8(2, 48, strcmp(frame.lyr, "plain") == 0
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

  drawHoldRing();
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
  u8g2.drawUTF8(2, 46, nfcReady ? "pn532 ready" : "pn532 not found");
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

  u8g2.begin();
  u8g2.setFontMode(1);
  u8g2.enableUTF8Print();

  Wire.begin();
  nfc.begin();
  uint32_t version = nfc.getFirmwareVersion();
  if (version) {
    /* SAMConfig is mandatory: without it reads fail silently forever. */
    nfc.SAMConfig();
    nfcReady = true;
  }

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

  /* Poll the reader between frames rather than every pass, so its blocking
     timeout cannot dominate the frame budget. */
  if (now - lastNfcPollMs >= 100) {
    pollNfc();
    lastNfcPollMs = millis();
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
