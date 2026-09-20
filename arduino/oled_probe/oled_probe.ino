/*
 * oled_probe - a throwaway diagnostic, not part of the product.
 *
 * Finds the fastest SPI bus speed this wiring is actually reliable at.
 * Corrupted init commands (an image that arrives upside down) and the
 * panel dropping out are both classic signal-integrity symptoms, and the
 * R4 clocks SPI faster than loose dupont wiring tolerates.
 *
 * The screen shows a heartbeat counter that increments four times a
 * second. That distinguishes the three failure modes by eye:
 *   counter stops       -> the panel froze, or the sketch hung
 *   counter keeps going but screen blinks -> panel losing state
 *   image inverted      -> init commands arriving corrupted
 *
 * Serial commands:
 *   0 1 2 3   switch controller profile
 *   b         step to the next (slower) bus speed
 *   B         step to the next (faster) bus speed
 *   r         re-initialise now
 *   i         toggle automatic re-init every 2s
 *   ?         report state
 */

#include <Arduino.h>
#include <SPI.h>
#include <U8g2lib.h>

#define PIN_CS 10
#define PIN_DC 9
#define PIN_RES 8

U8G2_SSD1309_128X64_NONAME0_F_4W_HW_SPI p0(U8G2_R0, PIN_CS, PIN_DC, PIN_RES);
U8G2_SSD1309_128X64_NONAME2_F_4W_HW_SPI p1(U8G2_R0, PIN_CS, PIN_DC, PIN_RES);
U8G2_SSD1306_128X64_NONAME_F_4W_HW_SPI  p2(U8G2_R0, PIN_CS, PIN_DC, PIN_RES);
U8G2_SH1106_128X64_NONAME_F_4W_HW_SPI   p3(U8G2_R0, PIN_CS, PIN_DC, PIN_RES);

U8G2 *panels[] = {&p0, &p1, &p2, &p3};
const char *names[] = {"SSD1309 N0", "SSD1309 N2", "SSD1306 NN", "SH1106 NN"};
const uint8_t PANEL_COUNT = 4;

/* Fastest first. Profile 0 rendered correctly at the default speed, so the
   question is not whether it works but how fast it works reliably. */
const uint32_t BUS_SPEEDS[] = {8000000, 4000000, 2000000, 1000000, 500000};
const char *BUS_LABELS[] = {"8MHz", "4MHz", "2MHz", "1MHz", "500kHz"};
const uint8_t BUS_COUNT = 5;

/* Start slow. If this is rock solid, step back up to find the ceiling. */
static uint8_t busIndex = 3;

static const uint16_t REDRAW_MS = 250;
static const uint32_t REINIT_MS = 2000;

static uint8_t current = 0;
static uint32_t heartbeat = 0;
static uint32_t lastRedraw = 0;
static uint32_t lastReinit = 0;
static bool autoReinit = false;

static void report(const char *event) {
  Serial.print(F("{\"t\":\""));
  Serial.print(event);
  Serial.print(F("\",\"index\":"));
  Serial.print(current);
  Serial.print(F(",\"profile\":\""));
  Serial.print(names[current]);
  Serial.print(F("\",\"bus\":\""));
  Serial.print(BUS_LABELS[busIndex]);
  Serial.print(F("\",\"auto_reinit\":"));
  Serial.print(autoReinit ? F("true") : F("false"));
  Serial.print(F(",\"beats\":"));
  Serial.print(heartbeat);
  Serial.println(F("}"));
}

static void hardReset() {
  pinMode(PIN_RES, OUTPUT);
  digitalWrite(PIN_RES, HIGH);
  delay(10);
  digitalWrite(PIN_RES, LOW);
  delay(50);
  digitalWrite(PIN_RES, HIGH);
  delay(50);
}

static void initPanel() {
  hardReset();
  /* Must be set before begin(), so a speed change means a full re-init. */
  panels[current]->setBusClock(BUS_SPEEDS[busIndex]);
  panels[current]->begin();
  panels[current]->setContrast(255);
  lastReinit = millis();
}

static void drawPanel() {
  U8G2 *panel = panels[current];
  panel->clearBuffer();

  panel->setFont(u8g2_font_logisoso32_tn);
  char digit[2] = {(char)('0' + current), 0};
  panel->drawStr(4, 44, digit);

  panel->setFont(u8g2_font_6x12_tf);
  panel->drawStr(34, 16, names[current]);

  panel->setFont(u8g2_font_7x13B_tf);
  panel->drawStr(34, 32, BUS_LABELS[busIndex]);

  /* Heartbeat: if this number stops climbing, the freeze is upstream of
     the panel. If it climbs while the screen blinks, the panel is at
     fault. */
  panel->setFont(u8g2_font_6x12_tf);
  char beat[16];
  snprintf(beat, sizeof(beat), "%lu", (unsigned long)heartbeat);
  panel->drawStr(34, 46, beat);

  /* Border: the tell for a column offset. Should be a clean rectangle
     touching all four edges. */
  panel->drawFrame(0, 0, 128, 64);

  /* Sweep: continuous motion makes a brief dropout obvious. */
  uint8_t x = (uint8_t)((heartbeat * 3) % 118);
  panel->drawBox(5 + x, 55, 6, 4);

  panel->sendBuffer();
}

static void handleSerial() {
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c >= '0' && c <= '3') {
      current = (uint8_t)(c - '0');
      initPanel();
      report("profile");
    } else if (c == 'b') {
      if (busIndex + 1 < BUS_COUNT) busIndex++;
      initPanel();
      report("bus");
    } else if (c == 'B') {
      if (busIndex > 0) busIndex--;
      initPanel();
      report("bus");
    } else if (c == 'r') {
      initPanel();
      report("reinit");
    } else if (c == 'i') {
      autoReinit = !autoReinit;
      report("auto_reinit");
    } else if (c == '?') {
      report("status");
    }
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);

  pinMode(PIN_CS, OUTPUT);
  pinMode(PIN_DC, OUTPUT);
  pinMode(PIN_RES, OUTPUT);

  current = 0;
  initPanel();
  report("boot");
}

void loop() {
  handleSerial();

  uint32_t now = millis();

  if (autoReinit && now - lastReinit >= REINIT_MS) {
    initPanel();
  }

  if (now - lastRedraw >= REDRAW_MS) {
    heartbeat++;
    drawPanel();
    lastRedraw = now;
  }
}
