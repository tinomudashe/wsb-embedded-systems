#include "ui.h"

#include <Preferences.h>
#include <SPI.h>
#include <TFT_eSPI.h>
#include <XPT2046_Touchscreen.h>
#include <atomic>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <qrcode.h>
#include <cmath>
#include <ctime>
#include "config.h"
#include "portal.h"
#include "power.h"
#include "sdlog.h"

namespace {

// Strict Flat White + Black Text + Solid Merito Blue (#0066FF) palette (RGB565)
constexpr uint16_t COL_BG = 0xFFFF;           // #FFFFFF pure white
constexpr uint16_t COL_CARD = 0xFFFF;         // #FFFFFF card white
constexpr uint16_t COL_SURFACE_ALT = 0xF79E;  // #F4F4F5 subtle light grey surface
constexpr uint16_t COL_BORDER = 0xE71C;       // #E4E4E7 1px light grey border
constexpr uint16_t COL_TEXT = 0x0000;         // #000000 black primary text
constexpr uint16_t COL_MUTED = 0x52AA;        // #52525B dark grey labels
constexpr uint16_t COL_WHITE = 0xFFFF;        // #FFFFFF white
constexpr uint16_t COL_BLUE = 0x033F;         // #0066FF solid Merito Blue
constexpr uint16_t COL_BLUE_DARK = 0x0255;    // deeper Merito Blue for inactive header elements

constexpr int TOP_BAR_H = 24;
constexpr int NAV_BAR_Y = 212;
constexpr int NAV_BAR_H = 28;
constexpr int CONTENT_H = NAV_BAR_Y - TOP_BAR_H;  // 188px

constexpr int TOAST_X = 16;
constexpr int TOAST_Y = 28;
constexpr int TOAST_W = 288;
constexpr int TOAST_H = 26;

constexpr int SYS_REC_BTN_X = 14;
constexpr int SYS_REC_BTN_Y = 180;
constexpr int SYS_REC_BTN_W = 140;
constexpr int SYS_REC_BTN_H = 24;

TFT_eSPI s_tft;
TFT_eSprite s_statusSpr(&s_tft);
TFT_eSprite s_sparkSpr(&s_tft);
TFT_eSprite s_toastSpr(&s_tft);

XPT2046_Touchscreen s_touch(PIN_TOUCH_CS, PIN_TOUCH_IRQ);

std::atomic<uint8_t> s_page{PAGE_ENV};
std::atomic<bool> s_needFullPageRedraw{true};
std::atomic<bool> s_displayInverted{true};
std::atomic<bool> s_pendingInvertChange{false};
bool s_wasScreenOff = false;
uint32_t s_lastUiUpdateMs = 0;

// Touch debounce state
bool s_touchWasDown = false;
uint32_t s_lastTouchActionMs = 0;

// Boot splash line tracker
int s_splashLineY = 68;

// Cached redraw state
char s_lastQrPayload[196] = "";
bool s_lastQrApMode = false;
bool s_lastQrHasCreds = false;
int s_lastAqiBand = -99;

// System tab REC control cache
bool s_lastRecActive = false;
size_t s_lastRecPending = 0;
size_t s_lastRecProblems = 0;
bool s_recUiDrawn = false;

// Thread-safe onboard toast state
SemaphoreHandle_t s_toastMutex = nullptr;
char s_toastMsg[64] = "";
uint32_t s_toastUntilMs = 0;
bool s_toastWasVisible = false;

int getAqiBand(bool valid, int16_t aqi) {
  if (!valid) return -1;
  if (aqi <= 50) return 0;
  if (aqi <= 100) return 1;
  if (aqi <= 150) return 2;
  if (aqi <= 200) return 3;
  if (aqi <= 300) return 4;
  return 5;
}

const char* aqiLabelText(bool valid, int16_t aqi) {
  if (!valid) return "NO AQI";
  if (aqi <= 50) return "GOOD";
  if (aqi <= 100) return "MODERATE";
  if (aqi <= 150) return "SENSITIVE";
  if (aqi <= 200) return "UNHEALTHY";
  if (aqi <= 300) return "VERY POOR";
  return "HAZARD";
}

void drawCardFrame(int x, int y, int w, int h, bool blueTop = true, int barH = 4) {
  s_tft.fillRoundRect(x, y, w, h, 5, COL_CARD);
  s_tft.drawRoundRect(x, y, w, h, 5, COL_BORDER);
  if (blueTop) {
    s_tft.fillRect(x + 2, y + 1, w - 4, barH, COL_BLUE);
  }
}

void drawCheckIcon(int x, int y, bool ok, uint16_t color) {
  if (ok) {
    s_tft.drawLine(x + 1, y + 5, x + 4, y + 8, color);
    s_tft.drawLine(x + 4, y + 8, x + 10, y + 1, color);
    s_tft.drawLine(x + 1, y + 6, x + 4, y + 9, color);
    s_tft.drawLine(x + 4, y + 9, x + 10, y + 2, color);
  } else {
    s_tft.drawLine(x + 1, y + 4, x + 9, y + 4, color);
    s_tft.drawLine(x + 1, y + 5, x + 9, y + 5, color);
  }
}

void drawBottomNav(UiPage page) {
  const char* labels[PAGE_COUNT] = {"Env", "GPS", "QR/Wi-Fi", "System"};
  s_tft.drawFastHLine(0, NAV_BAR_Y, SCREEN_W, COL_BORDER);

  s_tft.setTextFont(2);
  s_tft.setTextPadding(0);
  s_tft.setTextDatum(MC_DATUM);

  for (int i = 0; i < PAGE_COUNT; ++i) {
    int x = i * 80;
    bool active = (i == static_cast<int>(page));
    uint16_t bg = active ? COL_BLUE : COL_SURFACE_ALT;
    uint16_t fg = active ? COL_WHITE : COL_MUTED;

    s_tft.fillRect(x, NAV_BAR_Y + 1, 80, NAV_BAR_H - 1, bg);
    if (!active && i > 0 && (i - 1) != static_cast<int>(page)) {
      s_tft.drawFastVLine(x, NAV_BAR_Y + 4, NAV_BAR_H - 8, COL_BORDER);
    }
    s_tft.setTextColor(fg, bg, true);
    s_tft.drawString(labels[i], x + 40, NAV_BAR_Y + (NAV_BAR_H / 2) + 1);
  }
}

void drawStatusBar(const GpsReading& gps, const NetStatus& net, size_t bufferedCount) {
  s_statusSpr.fillSprite(COL_BLUE);

  // 1. Brand title (x=5..65)
  s_statusSpr.setTextFont(1);
  s_statusSpr.setTextDatum(ML_DATUM);
  s_statusSpr.setTextColor(COL_WHITE, COL_BLUE);
  s_statusSpr.drawString("WSB MERITO", 5, 12);

  // 2. Wi-Fi RSSI / AP SETUP indicator (x=70..124)
  if (net.apActive) {
    s_statusSpr.fillRoundRect(70, 3, 52, 17, 3, COL_WHITE);
    s_statusSpr.setTextDatum(MC_DATUM);
    s_statusSpr.setTextColor(COL_BLUE, COL_WHITE);
    s_statusSpr.drawString("AP SETUP", 96, 12);
  } else {
    int bars = 0;
    if (net.wifiConnected) {
      if (net.rssi >= -55) bars = 4;
      else if (net.rssi >= -67) bars = 3;
      else if (net.rssi >= -78) bars = 2;
      else bars = 1;
    }
    for (int b = 0; b < 4; ++b) {
      int bh = 3 + b * 3;
      int bx = 70 + b * 4;
      int by = 18 - bh;
      uint16_t col = (b < bars) ? COL_WHITE : COL_BLUE_DARK;
      s_statusSpr.fillRect(bx, by, 3, bh, col);
    }
    s_statusSpr.setTextDatum(ML_DATUM);
    if (net.wifiConnected) {
      char rssiBuf[12];
      snprintf(rssiBuf, sizeof(rssiBuf), "%ddB", net.rssi);
      s_statusSpr.setTextColor(COL_WHITE, COL_BLUE);
      s_statusSpr.drawString(rssiBuf, 88, 12);
    } else {
      s_statusSpr.setTextColor(COL_WHITE, COL_BLUE);
      s_statusSpr.drawString("NOWIFI", 88, 12);
    }
  }

  // 3. MQTT / HTTP state pill (x=128..164)
  bool netReady = net.mqttConnected || (net.wifiConnected && net.httpHealthy);
  const char* mqttTxt = net.mqttConnected ? "MQTT"
                      : (net.wifiConnected && net.httpHealthy) ? "HTTP"
                                                               : "OFF";
  if (netReady) {
    s_statusSpr.fillRoundRect(128, 3, 36, 17, 3, COL_WHITE);
    s_statusSpr.setTextDatum(MC_DATUM);
    s_statusSpr.setTextColor(COL_BLUE, COL_WHITE);
    s_statusSpr.drawString(mqttTxt, 146, 12);
  } else {
    s_statusSpr.fillRoundRect(128, 3, 36, 17, 3, COL_BLUE_DARK);
    s_statusSpr.drawRoundRect(128, 3, 36, 17, 3, COL_WHITE);
    s_statusSpr.setTextDatum(MC_DATUM);
    s_statusSpr.setTextColor(COL_WHITE, COL_BLUE_DARK);
    s_statusSpr.drawString(mqttTxt, 146, 12);
  }

  // 4. GPS fix + sats (x=169..214)
  char gpsBuf[14];
  if (gps.fix) {
    snprintf(gpsBuf, sizeof(gpsBuf), "GPS:%u", gps.sats);
  } else if (gps.hasData) {
    snprintf(gpsBuf, sizeof(gpsBuf), "FIX:%u", gps.sats);
  } else {
    snprintf(gpsBuf, sizeof(gpsBuf), "NOGPS");
  }
  s_statusSpr.setTextDatum(ML_DATUM);
  s_statusSpr.setTextColor(COL_WHITE, COL_BLUE);
  s_statusSpr.drawString(gpsBuf, 169, 12);

  // 5. UTC Time HH:MM:SS (x=218..268)
  char timeBuf[12] = "--:--:--";
  int64_t epochMs = 0;
  bool haveTime = netGetNtpEpochMs(epochMs);
  if (!haveTime && gps.utcValid) {
    epochMs = gps.utcEpochMs;
    haveTime = true;
  }
  if (haveTime && epochMs > 0) {
    time_t sec = static_cast<time_t>(epochMs / 1000LL);
    struct tm tmUtc;
    gmtime_r(&sec, &tmUtc);
    snprintf(timeBuf, sizeof(timeBuf), "%02d:%02d:%02d",
             tmUtc.tm_hour, tmUtc.tm_min, tmUtc.tm_sec);
  }
  s_statusSpr.setTextDatum(ML_DATUM);
  s_statusSpr.setTextColor(COL_WHITE, COL_BLUE);
  s_statusSpr.drawString(timeBuf, 218, 12);

  // 6. Buffered count pill Q:n (x=272..315)
  char bufTxt[12];
  snprintf(bufTxt, sizeof(bufTxt), "Q:%u", static_cast<unsigned>(bufferedCount));
  s_statusSpr.fillRoundRect(272, 3, 43, 17, 3, COL_WHITE);
  s_statusSpr.setTextDatum(MC_DATUM);
  s_statusSpr.setTextColor(COL_BLUE, COL_WHITE);
  s_statusSpr.drawString(bufTxt, 293, 12);

  s_statusSpr.pushSprite(0, 0);
}

void drawToastBanner(const char* msg) {
  s_toastSpr.fillSprite(COL_WHITE);
  s_toastSpr.fillRoundRect(0, 0, TOAST_W, TOAST_H, 6, COL_BLUE);
  s_toastSpr.setTextFont(2);
  s_toastSpr.setTextPadding(0);
  s_toastSpr.setTextDatum(MC_DATUM);
  s_toastSpr.setTextColor(COL_WHITE, COL_BLUE);
  s_toastSpr.drawString(msg, TOAST_W / 2, (TOAST_H / 2) + 1);
  s_toastSpr.pushSprite(TOAST_X, TOAST_Y);
}

void drawSparkline(const EnvReading& env) {
  float hist[TEMP_SPARKLINE_LEN];
  size_t count = sensorsGetTempHistory(hist, TEMP_SPARKLINE_LEN);

  s_sparkSpr.fillSprite(COL_CARD);
  s_sparkSpr.drawRoundRect(0, 0, 308, 57, 5, COL_BORDER);
  s_sparkSpr.fillRect(2, 1, 304, 4, COL_BLUE);

  s_sparkSpr.setTextFont(1);
  s_sparkSpr.setTextDatum(TL_DATUM);
  s_sparkSpr.setTextColor(COL_MUTED, COL_CARD);
  s_sparkSpr.drawString("TEMP TREND (60 SAMPLES)", 8, 8);

  if (count == 0) {
    s_sparkSpr.setTextDatum(MC_DATUM);
    s_sparkSpr.drawString(env.envChip == ENV_NONE ? "No temperature sensor" : "Collecting samples...",
                          154, 34);
    s_sparkSpr.pushSprite(6, 149);
    return;
  }

  float minT = hist[0], maxT = hist[0];
  for (size_t i = 1; i < count; ++i) {
    if (hist[i] < minT) minT = hist[i];
    if (hist[i] > maxT) maxT = hist[i];
  }
  float span = maxT - minT;
  if (span < 0.5f) {
    float mid = 0.5f * (minT + maxT);
    minT = mid - 0.25f;
    maxT = mid + 0.25f;
    span = 0.5f;
  }

  char rangeBuf[28];
  snprintf(rangeBuf, sizeof(rangeBuf), "%.1f..%.1fC", minT, maxT);
  s_sparkSpr.setTextDatum(TR_DATUM);
  s_sparkSpr.setTextColor(COL_BLUE, COL_CARD);
  s_sparkSpr.drawString(rangeBuf, 300, 8);

  const int plotX = 8;
  const int plotY = 19;
  const int plotW = 292;
  const int plotH = 31;
  s_sparkSpr.drawFastHLine(plotX, plotY + plotH / 2, plotW, COL_BORDER);

  if (count == 1) {
    int y = plotY + plotH / 2;
    s_sparkSpr.fillCircle(plotX + plotW - 2, y, 2, COL_BLUE);
  } else {
    int prevX = 0, prevY = 0;
    for (size_t i = 0; i < count; ++i) {
      int x = plotX + static_cast<int>((i * (plotW - 1)) / (TEMP_SPARKLINE_LEN - 1));
      if (count < TEMP_SPARKLINE_LEN) {
        x = plotX + plotW - 1 -
            static_cast<int>(((count - 1 - i) * (plotW - 1)) / (TEMP_SPARKLINE_LEN - 1));
      }
      float norm = (hist[i] - minT) / span;
      int y = plotY + plotH - 1 - static_cast<int>(lroundf(norm * (plotH - 1)));
      y = constrain(y, plotY, plotY + plotH - 1);
      if (i > 0) {
        s_sparkSpr.drawLine(prevX, prevY, x, y, COL_BLUE);
        if (prevY + 1 <= plotY + plotH - 1 && y + 1 <= plotY + plotH - 1) {
          s_sparkSpr.drawLine(prevX, prevY + 1, x, y + 1, COL_BLUE);
        }
      }
      prevX = x;
      prevY = y;
    }
    s_sparkSpr.fillCircle(prevX, prevY, 3, COL_BLUE);
    s_sparkSpr.fillCircle(prevX, prevY, 1, COL_WHITE);
  }

  s_sparkSpr.pushSprite(6, 149);
}

void drawEnvPage(const EnvReading& env, bool fullRedraw) {
  if (fullRedraw) {
    s_lastAqiBand = -99;
    drawCardFrame(6, 28, 151, 56, true, 4);
    drawCardFrame(163, 28, 151, 56, true, 4);
    drawCardFrame(6, 88, 151, 56, true, 4);
    drawCardFrame(163, 88, 151, 56, true, 4);

    s_tft.setTextFont(1);
    s_tft.setTextPadding(0);
    s_tft.setTextDatum(TL_DATUM);
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
    s_tft.drawString("TEMPERATURE", 14, 36);
    s_tft.drawString("HUMIDITY", 171, 36);
    s_tft.drawString("PRESSURE", 14, 96);
    s_tft.drawString("AIR QUALITY", 171, 96);

    const char* chipTag = (env.envChip == ENV_BME680) ? "BME680"
                        : (env.envChip == ENV_BME280) ? "BME280"
                                                      : "NO BME";
    s_tft.setTextDatum(TR_DATUM);
    s_tft.drawString(chipTag, 149, 36);
  }

  char valBuf[32];

  // Temperature value
  s_tft.setTextFont(4);
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(132);
  if (env.tempValid) {
    snprintf(valBuf, sizeof(valBuf), "%.1f C", env.tempC);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(valBuf, sizeof(valBuf), "--.- C");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(valBuf, 14, 52);

  // Humidity value (Merito Blue)
  if (env.humValid) {
    snprintf(valBuf, sizeof(valBuf), "%.1f %%", env.humidity);
    s_tft.setTextColor(COL_BLUE, COL_CARD, true);
  } else {
    snprintf(valBuf, sizeof(valBuf), "--.- %%");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(valBuf, 171, 52);

  // Pressure value + Baro Alt subtext
  if (env.presValid) {
    snprintf(valBuf, sizeof(valBuf), "%.1f", env.pressureHpa);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(valBuf, sizeof(valBuf), "----.-");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.setTextPadding(100);
  s_tft.drawString(valBuf, 14, 111);

  s_tft.setTextFont(1);
  s_tft.setTextPadding(0);
  s_tft.setTextDatum(TR_DATUM);
  s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  s_tft.drawString("hPa", 149, 119);

  s_tft.setTextPadding(70);
  if (env.baroAltValid) {
    snprintf(valBuf, sizeof(valBuf), "Alt:%.0fm", env.baroAltM);
  } else {
    snprintf(valBuf, sizeof(valBuf), "Alt:--");
  }
  s_tft.drawString(valBuf, 149, 96);

  // Air Quality card (x=163, y=88, w=151, h=56) with Merito Blue status pill
  s_tft.setTextFont(4);
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(58);
  if (env.aqiValid) {
    snprintf(valBuf, sizeof(valBuf), "%d", env.aqi);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(valBuf, sizeof(valBuf), "--");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(valBuf, 171, 108);

  int band = getAqiBand(env.aqiValid, env.aqi);
  if (fullRedraw || band != s_lastAqiBand) {
    s_lastAqiBand = band;
    const char* aqiLabel = aqiLabelText(env.aqiValid, env.aqi);
    uint16_t pillBg = env.aqiValid ? COL_BLUE : COL_SURFACE_ALT;
    uint16_t pillFg = env.aqiValid ? COL_WHITE : COL_MUTED;
    s_tft.fillRoundRect(236, 109, 72, 18, 4, pillBg);
    if (!env.aqiValid) {
      s_tft.drawRoundRect(236, 109, 72, 18, 4, COL_BORDER);
    }
    s_tft.setTextFont(1);
    s_tft.setTextPadding(0);
    s_tft.setTextDatum(MC_DATUM);
    s_tft.setTextColor(pillFg, pillBg, true);
    s_tft.drawString(aqiLabel, 272, 118);
  }

  s_tft.setTextFont(1);
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(136);
  s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  if (env.aqiSource == AQI_ENS160 && env.eco2Valid && env.tvocValid) {
    snprintf(valBuf, sizeof(valBuf), "eCO2:%u TVOC:%u", env.eco2Ppm, env.tvocPpb);
  } else if (env.aqiSource == AQI_BME680_IAQ && env.gasValid) {
    snprintf(valBuf, sizeof(valBuf), "Gas:%.0fk%s", env.gasKohm,
             env.bme680BurnInDone ? "" : " (burn-in)");
  } else if (env.hasEns160) {
    snprintf(valBuf, sizeof(valBuf), "ENS160 warming up");
  } else {
    snprintf(valBuf, sizeof(valBuf), "Src: none");
  }
  s_tft.drawString(valBuf, 171, 133);

  drawSparkline(env);
}

void drawGpsPage(const GpsReading& gps, const EnvReading& env, bool fullRedraw) {
  if (fullRedraw) {
    drawCardFrame(6, 28, 308, 26, true, 3);
    drawCardFrame(6, 58, 308, 54, true, 4);
    drawCardFrame(6, 116, 151, 43, true, 4);
    drawCardFrame(163, 116, 151, 43, true, 4);
    drawCardFrame(6, 163, 151, 43, true, 4);
    drawCardFrame(163, 163, 151, 43, true, 4);

    s_tft.setTextFont(1);
    s_tft.setTextPadding(0);
    s_tft.setTextDatum(TL_DATUM);
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
    s_tft.drawString("LAT", 14, 68);
    s_tft.drawString("LON", 14, 92);
    s_tft.drawString("GPS ALTITUDE", 14, 123);
    s_tft.drawString("BARO ALTITUDE", 171, 123);
    s_tft.drawString("SPEED / COURSE", 14, 170);
    s_tft.drawString("SATS / HDOP", 171, 170);
  }

  char buf[40];

  // Top fix status (3D FIX in Merito Blue) & fix age
  s_tft.setTextFont(2);
  s_tft.setTextDatum(ML_DATUM);
  s_tft.setTextPadding(144);
  if (gps.fix) {
    s_tft.setTextColor(COL_BLUE, COL_CARD, true);
    s_tft.drawString("STATUS: 3D FIX", 14, 42);
  } else if (gps.hasData) {
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
    s_tft.drawString("STATUS: SEARCHING", 14, 42);
  } else {
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
    s_tft.drawString("STATUS: NO DATA", 14, 42);
  }

  s_tft.setTextFont(1);
  s_tft.setTextDatum(MR_DATUM);
  s_tft.setTextPadding(140);
  if (gps.fixAgeMs == UINT32_MAX) {
    snprintf(buf, sizeof(buf), "FixAge: -- (%ub)", gps.currentBaud);
  } else if (gps.fixAgeMs < 10000UL) {
    snprintf(buf, sizeof(buf), "FixAge: %.1fs", gps.fixAgeMs / 1000.0f);
  } else {
    snprintf(buf, sizeof(buf), "FixAge: %lus", static_cast<unsigned long>(gps.fixAgeMs / 1000UL));
  }
  s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  s_tft.drawString(buf, 306, 42);

  // Lat / Lon (6 dp)
  s_tft.setTextFont(4);
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(240);
  if (gps.fix && gps.latLonValid) {
    snprintf(buf, sizeof(buf), "%.6f", gps.lat);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(buf, sizeof(buf), "---.------");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(buf, 52, 64);

  if (gps.fix && gps.latLonValid) {
    snprintf(buf, sizeof(buf), "%.6f", gps.lon);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(buf, sizeof(buf), "---.------");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(buf, 52, 88);

  // GPS Alt
  s_tft.setTextFont(2);
  s_tft.setTextPadding(132);
  if (gps.fix && gps.altValid) {
    snprintf(buf, sizeof(buf), "%.1f m", gps.altM);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(buf, sizeof(buf), "--.- m");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(buf, 14, 138);

  // Baro Alt (Merito Blue)
  if (env.baroAltValid) {
    snprintf(buf, sizeof(buf), "%.1f m", env.baroAltM);
    s_tft.setTextColor(COL_BLUE, COL_CARD, true);
  } else {
    snprintf(buf, sizeof(buf), "--.- m");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(buf, 171, 138);

  // Speed / Course
  if (gps.fix && gps.speedValid && gps.courseValid) {
    snprintf(buf, sizeof(buf), "%.1f km/h  %.0fd", gps.speedKmh, gps.courseDeg);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else if (gps.fix && gps.speedValid) {
    snprintf(buf, sizeof(buf), "%.1f km/h  --d", gps.speedKmh);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(buf, sizeof(buf), "-- km/h  --d");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(buf, 14, 185);

  // Sats / HDOP
  if (gps.satsValid && gps.hdopValid) {
    snprintf(buf, sizeof(buf), "%u sats  %.2f", gps.sats, gps.hdop);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else if (gps.satsValid) {
    snprintf(buf, sizeof(buf), "%u sats  --", gps.sats);
    s_tft.setTextColor(COL_TEXT, COL_CARD, true);
  } else {
    snprintf(buf, sizeof(buf), "-- sats  --");
    s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  }
  s_tft.drawString(buf, 171, 185);
}

void drawQrMatrix(const char* text) {
  s_tft.fillRect(10, 30, 144, 144, COL_WHITE);
  s_tft.drawRect(10, 30, 144, 144, COL_BORDER);

  size_t len = strlen(text);
  if (len == 0 || len > 192) {
    s_tft.setTextFont(1);
    s_tft.setTextDatum(MC_DATUM);
    s_tft.setTextColor(COL_TEXT, COL_WHITE, true);
    s_tft.drawString("QR data too long", 82, 102);
    return;
  }

  uint8_t ver = 6;
  int scale = 3;
  if (len <= 78) {
    ver = 4;
    scale = 4;
  } else if (len <= 106) {
    ver = 5;
    scale = 3;
  } else if (len <= 134) {
    ver = 6;
    scale = 3;
  } else if (len <= 154) {
    ver = 7;
    scale = 3;
  } else {
    ver = 8;
    scale = 2;
  }

  uint8_t qrcodeData[320];
  QRCode qrcode;
  int8_t rc = qrcode_initText(&qrcode, qrcodeData, ver, ECC_LOW, text);
  if (rc != 0) {
    s_tft.setTextFont(1);
    s_tft.setTextDatum(MC_DATUM);
    s_tft.setTextColor(COL_TEXT, COL_WHITE, true);
    s_tft.drawString("QR encode error", 82, 102);
    return;
  }

  int qrPx = qrcode.size * scale;
  int x0 = 10 + (144 - qrPx) / 2;
  int y0 = 30 + (144 - qrPx) / 2;

  for (uint8_t y = 0; y < qrcode.size; ++y) {
    for (uint8_t x = 0; x < qrcode.size; ++x) {
      if (qrcode_getModule(&qrcode, x, y)) {
        s_tft.fillRect(x0 + x * scale, y0 + y * scale, scale, scale, COL_TEXT);
      }
    }
  }
}

void drawBlueButton(int x, int y, int w, int h, const char* label) {
  s_tft.fillRoundRect(x, y, w, h, 6, COL_BLUE);
  s_tft.setTextFont(2);
  s_tft.setTextPadding(0);
  s_tft.setTextDatum(MC_DATUM);
  s_tft.setTextColor(COL_WHITE, COL_BLUE, true);
  s_tft.drawString(label, x + (w / 2), y + (h / 2) + 1);
}

void drawQrHeaderPill(const char* title) {
  s_tft.fillRoundRect(160, 30, 148, 22, 4, COL_BLUE);
  s_tft.setTextFont(2);
  s_tft.setTextPadding(0);
  s_tft.setTextDatum(MC_DATUM);
  s_tft.setTextColor(COL_WHITE, COL_BLUE, true);
  s_tft.drawString(title, 234, 41);
}

void drawQrPage(const NetStatus& net, bool fullRedraw) {
  bool hasCreds = settingsHasWifiCredentials();
  bool inApMode = net.apActive || !hasCreds;

  char qrPayload[196];
  if (inApMode) {
    snprintf(qrPayload, sizeof(qrPayload), "WIFI:T:WPA;S:%s;P:%s;;",
             AP_SETUP_SSID, AP_SETUP_PASS);
  } else if (net.pairUrl[0] != '\0') {
    snprintf(qrPayload, sizeof(qrPayload), "%s", net.pairUrl);
  } else {
    const char* id = (net.deviceId[0] != '\0') ? net.deviceId : "cyd-01";
    snprintf(qrPayload, sizeof(qrPayload), "%s/?device=%s", DASHBOARD_BASE_URL, id);
  }

  bool modeChanged = (inApMode != s_lastQrApMode) || (hasCreds != s_lastQrHasCreds);
  bool qrChanged = (strcmp(qrPayload, s_lastQrPayload) != 0);

  if (fullRedraw || modeChanged) {
    s_tft.fillRect(156, TOP_BAR_H, SCREEN_W - 156, CONTENT_H, COL_BG);
    s_lastQrApMode = inApMode;
    s_lastQrHasCreds = hasCreds;
    drawQrHeaderPill(inApMode ? "1. Scan for Wi-Fi" : "Phone Sign-In QR");
  }

  if (fullRedraw || qrChanged) {
    snprintf(s_lastQrPayload, sizeof(s_lastQrPayload), "%s", qrPayload);
    drawQrMatrix(qrPayload);

    s_tft.setTextFont(1);
    s_tft.setTextDatum(TC_DATUM);
    s_tft.setTextPadding(144);
    s_tft.setTextColor(COL_MUTED, COL_BG, true);
    s_tft.drawString(inApMode ? "Scan camera to join Wi-Fi"
                              : (net.bootstrapOk ? "Scan to pair on phone"
                                                 : "Scan to open dashboard"),
                     82, 182);
  }

  // Right-side information panel (x = 160..312)
  char buf[44];
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(148);

  if (inApMode) {
    s_tft.setTextFont(2);
    s_tft.setTextColor(COL_TEXT, COL_BG, true);
    snprintf(buf, sizeof(buf), "AP: %s", AP_SETUP_SSID);
    s_tft.drawString(buf, 160, 60);

    snprintf(buf, sizeof(buf), "Pass: %s", AP_SETUP_PASS);
    s_tft.drawString(buf, 160, 82);

    s_tft.setTextColor(COL_BLUE, COL_BG, true);
    s_tft.drawString("Portal: 192.168.4.1", 160, 104);

    s_tft.setTextFont(1);
    s_tft.setTextColor(COL_MUTED, COL_BG, true);
    s_tft.drawString("Join AP & pick router", 160, 134);
    s_tft.drawString("in captive portal page.", 160, 146);

    if (fullRedraw || modeChanged) {
      if (hasCreds) {
        drawBlueButton(160, 174, 148, 28, "Exit Setup");
      } else {
        s_tft.fillRect(160, 174, 148, 28, COL_BG);
        s_tft.setTextFont(1);
        s_tft.setTextPadding(148);
        s_tft.setTextColor(COL_MUTED, COL_BG, true);
        s_tft.drawString("2. Save Wi-Fi on phone", 160, 184);
      }
    }
  } else {
    s_tft.setTextFont(2);
    s_tft.setTextColor(COL_MUTED, COL_BG, true);
    s_tft.drawString("Scan to sign in &", 160, 58);
    s_tft.drawString("link unit on phone", 160, 76);

    if (net.claimCode[0] != '\0') {
      snprintf(buf, sizeof(buf), "Code: %s", net.claimCode);
      s_tft.setTextColor(COL_BLUE, COL_BG, true);
    } else if (net.wifiConnected) {
      snprintf(buf, sizeof(buf), "Code: syncing...");
      s_tft.setTextColor(COL_MUTED, COL_BG, true);
    } else {
      snprintf(buf, sizeof(buf), "Code: connecting...");
      s_tft.setTextColor(COL_MUTED, COL_BG, true);
    }
    s_tft.drawString(buf, 160, 104);

    char shortSsid[20];
    if (net.ssid[0] != '\0') {
      snprintf(shortSsid, sizeof(shortSsid), "%.17s%s",
               net.ssid, strlen(net.ssid) > 17 ? ".." : "");
    } else {
      snprintf(shortSsid, sizeof(shortSsid), "(none)");
    }
    snprintf(buf, sizeof(buf), "WiFi: %s", shortSsid);
    s_tft.setTextColor(COL_TEXT, COL_BG, true);
    s_tft.drawString(buf, 160, 126);

    s_tft.setTextFont(1);
    s_tft.setTextColor(COL_MUTED, COL_BG, true);
    snprintf(buf, sizeof(buf), "%s", net.wifiConnected ? net.ip : "Connecting Wi-Fi...");
    s_tft.drawString(buf, 160, 148);

    if (fullRedraw || modeChanged) {
      drawBlueButton(160, 174, 148, 28, "Wi-Fi Setup");
    }
  }
}

void drawSysRow(int row, const char* label, const char* value, uint16_t valCol) {
  int y = 32 + row * 16;
  s_tft.setTextFont(2);
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(92);
  s_tft.setTextColor(COL_MUTED, COL_CARD, true);
  s_tft.drawString(label, 14, y);

  s_tft.setTextDatum(TR_DATUM);
  s_tft.setTextPadding(195);
  s_tft.setTextColor(valCol, COL_CARD, true);
  s_tft.drawString(value, 306, y);
}

void drawSysPage(const EnvReading& env,
                 const GpsReading& gps,
                 const NetStatus& net,
                 const DeviceConfig& cfg,
                 size_t bufferedCount,
                 bool fullRedraw) {
  (void)env;
  if (fullRedraw) {
    drawCardFrame(6, 28, 308, 179, true, 4);
  }

  char buf[64];
  const char* devId = (net.deviceId[0] != '\0') ? net.deviceId : DEVICE_ID;

  snprintf(buf, sizeof(buf), "%s  (FW %s)", devId, FW_VERSION);
  drawSysRow(0, "Device / FW", buf, COL_BLUE);

  if (net.apActive) {
    snprintf(buf, sizeof(buf), "192.168.4.1 (AP)");
    drawSysRow(1, "IP Address", buf, COL_BLUE);
  } else {
    snprintf(buf, sizeof(buf), "%s", net.wifiConnected ? net.ip : "Disconnected");
    drawSysRow(1, "IP Address", buf, net.wifiConnected ? COL_TEXT : COL_MUTED);
  }

  snprintf(buf, sizeof(buf), "v%u  (intv %us)", cfg.version, cfg.telemetry_interval_s);
  drawSysRow(2, "Config Ver", buf, COL_TEXT);

  uint32_t upSec = millis() / 1000UL;
  uint32_t d = upSec / 86400UL;
  uint32_t h = (upSec % 86400UL) / 3600UL;
  uint32_t m = (upSec % 3600UL) / 60UL;
  uint32_t s = upSec % 60UL;
  snprintf(buf, sizeof(buf), "%lud %02lu:%02lu:%02lu",
           static_cast<unsigned long>(d), static_cast<unsigned long>(h),
           static_cast<unsigned long>(m), static_cast<unsigned long>(s));
  drawSysRow(3, "Uptime", buf, COL_TEXT);

  snprintf(buf, sizeof(buf), "%.1f KB  (Q:%u)",
           ESP.getFreeHeap() / 1024.0f, static_cast<unsigned>(bufferedCount));
  drawSysRow(4, "Free Heap", buf, COL_TEXT);

  snprintf(buf, sizeof(buf), "%lu ok / %lu err",
           static_cast<unsigned long>(net.mqttSent),
           static_cast<unsigned long>(net.mqttFailed));
  drawSysRow(5, "MQTT Tx/Err", buf, COL_TEXT);

  snprintf(buf, sizeof(buf), "%lu ok / %lu err",
           static_cast<unsigned long>(net.httpSent),
           static_cast<unsigned long>(net.httpFailed));
  drawSysRow(6, "HTTP Tx/Err", buf, COL_TEXT);

  char sensBuf[28];
  sensorsSummaryString(sensBuf, sizeof(sensBuf));
  snprintf(buf, sizeof(buf), "%s | GPS:%s", sensBuf, gps.hasData ? "OK" : "No");
  drawSysRow(7, "Sensors", buf, COL_TEXT);

  snprintf(buf, sizeof(buf), "BL:%u LDR:%u",
           powerGetBacklightDuty(), powerGetLdrRaw());
  drawSysRow(8, "Display/LDR", buf, COL_TEXT);

  // microSD ride recorder control (tap the full-width button below the rows).
  bool recActive = sdlogIsActive();
  size_t pending = sdlogPendingCount();
  size_t problems = sdlogProblemCount();
  if (fullRedraw || !s_recUiDrawn || recActive != s_lastRecActive ||
      pending != s_lastRecPending || problems != s_lastRecProblems) {
    s_lastRecActive = recActive;
    s_lastRecPending = pending;
    s_lastRecProblems = problems;
    s_recUiDrawn = true;

    drawBlueButton(SYS_REC_BTN_X, SYS_REC_BTN_Y, SYS_REC_BTN_W, SYS_REC_BTN_H,
                   recActive ? "Stop REC" : "Start REC");

    char recBuf[40];
    if (recActive) {
      snprintf(recBuf, sizeof(recBuf), "REC  %u q  %u parked",
               static_cast<unsigned>(pending), static_cast<unsigned>(problems));
    } else {
      snprintf(recBuf, sizeof(recBuf), "SD log  %u q  %u parked",
               static_cast<unsigned>(pending), static_cast<unsigned>(problems));
    }
    s_tft.setTextFont(1);
    s_tft.setTextDatum(ML_DATUM);
    s_tft.setTextPadding(150);
    s_tft.setTextColor(recActive ? COL_BLUE : COL_MUTED, COL_CARD, true);
    s_tft.drawString(recBuf, 162, SYS_REC_BTN_Y + (SYS_REC_BTN_H / 2));
    s_tft.setTextPadding(0);
  }
}

}  // namespace

void uiInit() {
  if (!s_toastMutex) {
    s_toastMutex = xSemaphoreCreateMutex();
  }

  bool inv = true;
  Preferences prefs;
  if (prefs.begin("wsb_cfg", true)) {
    if (prefs.isKey("inv")) {
      inv = prefs.getBool("inv", true);
    }
    prefs.end();
  }
  s_displayInverted.store(inv);

  s_tft.init();
  s_tft.invertDisplay(inv);
  s_tft.setRotation(1);
  s_tft.fillScreen(COL_BG);

  // Initialize touch on VSPI (global SPI = VSPI; TFT_eSPI uses HSPI)
  SPI.begin(PIN_TOUCH_CLK, PIN_TOUCH_MISO, PIN_TOUCH_MOSI, PIN_TOUCH_CS);
  s_touch.begin();
  s_touch.setRotation(1);

  // Sprites for flicker-free status bar, temperature sparkline, and toast banner
  s_statusSpr.setColorDepth(16);
  s_statusSpr.createSprite(SCREEN_W, TOP_BAR_H);

  s_sparkSpr.setColorDepth(16);
  s_sparkSpr.createSprite(308, 57);

  s_toastSpr.setColorDepth(16);
  s_toastSpr.createSprite(TOAST_W, TOAST_H);

  Serial.printf("[UI] Display 320x240 & XPT2046 touch ready (free heap: %u)\n",
                ESP.getFreeHeap());
}

void uiShowSplash() {
  s_tft.fillScreen(COL_BG);
  drawCardFrame(10, 10, 300, 220, false);
  s_tft.fillRect(11, 11, 298, 44, COL_BLUE);

  s_tft.setTextFont(4);
  s_tft.setTextDatum(TC_DATUM);
  s_tft.setTextColor(COL_WHITE, COL_BLUE, true);
  s_tft.drawString("WSB MERITO", 160, 14);

  s_tft.setTextFont(1);
  s_tft.setTextColor(COL_WHITE, COL_BLUE, true);
  char devId[36];
  settingsGetDeviceId(devId, sizeof(devId));
  char sub[72];
  snprintf(sub, sizeof(sub), "ESP32-2432S028R  ID:%s  FW:%s", devId, FW_VERSION);
  s_tft.drawString(sub, 160, 42);

  s_splashLineY = 68;
}

void uiSplashStep(bool ok, const char* text) {
  if (s_splashLineY > 206) return;
  drawCheckIcon(24, s_splashLineY + 2, ok, ok ? COL_BLUE : COL_MUTED);
  s_tft.setTextFont(2);
  s_tft.setTextDatum(TL_DATUM);
  s_tft.setTextPadding(0);
  s_tft.setTextColor(ok ? COL_TEXT : COL_MUTED, COL_CARD, true);
  s_tft.drawString(text, 42, s_splashLineY);
  s_splashLineY += 22;
}

void uiBeginLive(UiPage initialPage) {
  s_tft.fillScreen(COL_BG);
  s_page.store((initialPage < PAGE_COUNT) ? initialPage : PAGE_ENV);
  s_lastQrPayload[0] = '\0';
  s_needFullPageRedraw.store(true);
  s_lastUiUpdateMs = 0;
}

void uiSetPage(UiPage page) {
  uint8_t p = (page < PAGE_COUNT) ? static_cast<uint8_t>(page) : static_cast<uint8_t>(PAGE_ENV);
  s_page.store(p);
  s_needFullPageRedraw.store(true);
}

UiPage uiGetPage() {
  uint8_t p = s_page.load();
  return (p < PAGE_COUNT) ? static_cast<UiPage>(p) : PAGE_ENV;
}

void uiToggleInvertDisplay() {
  bool next = !s_displayInverted.load();
  s_displayInverted.store(next);
  Preferences prefs;
  if (prefs.begin("wsb_cfg", false)) {
    prefs.putBool("inv", next);
    prefs.end();
  }
  s_pendingInvertChange.store(true);
  s_needFullPageRedraw.store(true);
  Serial.printf("[UI] Display inversion toggled -> %s\n", next ? "ON" : "OFF");
}

bool uiIsDisplayInverted() {
  return s_displayInverted.load();
}

void uiShowToast(const char* msg, uint32_t durationMs) {
  if (!msg || msg[0] == '\0') return;
  if (!s_toastMutex) {
    s_toastMutex = xSemaphoreCreateMutex();
  }
  if (!s_toastMutex) return;

  xSemaphoreTake(s_toastMutex, portMAX_DELAY);
  snprintf(s_toastMsg, sizeof(s_toastMsg), "%s", msg);
  s_toastUntilMs = millis() + (durationMs > 0 ? durationMs : 3500UL);
  xSemaphoreGive(s_toastMutex);

  Serial.printf("[UI] Toast: %s\n", msg);
}

void uiPollTouch() {
  // The SD logger shares VSPI with the touch controller; take the bus briefly.
  if (!sdlogBusLock()) return;
  bool isDown = s_touch.touched();
  if (!isDown) {
    sdlogBusUnlock();
    s_touchWasDown = false;
    return;
  }

  // Trigger once per press with debounce cooldown
  uint32_t now = millis();
  if (s_touchWasDown || (now - s_lastTouchActionMs) < TOUCH_DEBOUNCE_MS) {
    sdlogBusUnlock();
    return;
  }
  s_touchWasDown = true;
  s_lastTouchActionMs = now;

  TS_Point p = s_touch.getPoint();
  sdlogBusUnlock();
  int mappedX = map(p.x, TOUCH_RAW_MIN_X, TOUCH_RAW_MAX_X, 0, SCREEN_W);
  mappedX = constrain(mappedX, 0, SCREEN_W - 1);
  int mappedY = map(p.y, TOUCH_RAW_MIN_Y, TOUCH_RAW_MAX_Y, 0, SCREEN_H);
  mappedY = constrain(mappedY, 0, SCREEN_H - 1);

  // First touch from DIM or OFF only wakes screen; does not navigate
  if (powerNotifyTouch()) {
    s_needFullPageRedraw.store(true);
    s_lastUiUpdateMs = 0;
    Serial.println("[UI] Touch wake (navigation suppressed)");
    return;
  }

  // Bottom tab bar direct selection (y >= 208)
  if (mappedY >= 208) {
    int tab = constrain(mappedX / 80, 0, PAGE_COUNT - 1);
    s_page.store(static_cast<uint8_t>(tab));
    s_needFullPageRedraw.store(true);
    s_lastUiUpdateMs = 0;
    netRequestStatusPublish();
    Serial.printf("[UI] Tab tap x=%d y=%d -> page %u\n",
                  mappedX, mappedY, static_cast<unsigned>(tab));
    return;
  }

  // Page 2 [ Wi-Fi Setup ] / [ Exit Setup ] button (x=158..312, y=170..205)
  UiPage curPage = uiGetPage();
  if (curPage == PAGE_QR && mappedX >= 158 && mappedX <= 312 &&
      mappedY >= 170 && mappedY <= 205) {
    if (portalIsActive()) {
      if (settingsHasWifiCredentials()) {
        portalStop();
        Serial.println("[UI] Exit Setup tapped -> stopping SoftAP");
      }
    } else {
      portalStart();
      Serial.println("[UI] Wi-Fi Setup tapped -> starting SoftAP");
    }
    s_needFullPageRedraw.store(true);
    s_lastUiUpdateMs = 0;
    netRequestStatusPublish();
    return;
  }

  // System tab [ Start REC ] / [ Stop REC ] touch button (x=14..154, y=180..204)
  if (curPage == PAGE_SYS && mappedX >= SYS_REC_BTN_X &&
      mappedX <= (SYS_REC_BTN_X + SYS_REC_BTN_W) &&
      mappedY >= SYS_REC_BTN_Y && mappedY <= (SYS_REC_BTN_Y + SYS_REC_BTN_H)) {
    if (sdlogIsActive()) {
      sdlogStop();
      uiShowToast("SD recording stopped", 2500);
      netLogEvent("INFO", "sd", "Ride recording stopped");
    } else if (sdlogStart()) {
      uiShowToast("SD recording started", 2500);
      netLogEvent("INFO", "sd", "Ride recording started");
    } else {
      uiShowToast("SD start failed: insert card / set clock", 3500);
      netLogEvent("WARN", "sd", "Ride recording start failed");
    }
    s_needFullPageRedraw.store(true);
    s_lastUiUpdateMs = 0;
    netRequestStatusPublish();
    return;
  }

  // Middle area (y < 170) left/right cycles pages
  if (mappedY < 170) {
    uint8_t cur = s_page.load();
    uint8_t next = (mappedX < (SCREEN_W / 2))
                       ? static_cast<uint8_t>((cur + PAGE_COUNT - 1) % PAGE_COUNT)
                       : static_cast<uint8_t>((cur + 1) % PAGE_COUNT);
    s_page.store(next);
    s_needFullPageRedraw.store(true);
    s_lastUiUpdateMs = 0;
    netRequestStatusPublish();
    Serial.printf("[UI] Touch x=%d y=%d -> page %u\n",
                  mappedX, mappedY, static_cast<unsigned>(next));
  }
}

void uiUpdate(const EnvReading& env,
              const GpsReading& gps,
              const NetStatus& net,
              const DeviceConfig& cfg,
              size_t bufferedCount,
              bool forceRedraw) {
  if (powerIsScreenOff()) {
    s_wasScreenOff = true;
    return;
  }

  if (s_wasScreenOff) {
    s_wasScreenOff = false;
    s_needFullPageRedraw.store(true);
  }

  if (s_pendingInvertChange.exchange(false)) {
    s_tft.invertDisplay(s_displayInverted.load());
    s_needFullPageRedraw.store(true);
  }

  // Check thread-safe toast state
  uint32_t now = millis();
  char toastMsg[64] = "";
  bool toastActive = false;
  if (s_toastMutex) {
    xSemaphoreTake(s_toastMutex, portMAX_DELAY);
    if (s_toastMsg[0] != '\0' && static_cast<int32_t>(s_toastUntilMs - now) > 0) {
      toastActive = true;
      snprintf(toastMsg, sizeof(toastMsg), "%s", s_toastMsg);
    } else {
      s_toastMsg[0] = '\0';
    }
    xSemaphoreGive(s_toastMutex);
  }

  // When toast expires, restore underlying cards via full page redraw
  bool toastExpired = false;
  if (s_toastWasVisible && !toastActive) {
    s_toastWasVisible = false;
    toastExpired = true;
  }
  bool toastJustAppeared = (toastActive && !s_toastWasVisible);

  if (!forceRedraw && !toastExpired && !s_needFullPageRedraw.load() && !toastJustAppeared &&
      (now - s_lastUiUpdateMs) < UI_REFRESH_MS) {
    return;
  }
  s_lastUiUpdateMs = now;

  bool full = s_needFullPageRedraw.exchange(false) || forceRedraw || toastExpired;
  UiPage activePage = uiGetPage();
  if (full) {
    s_tft.fillRect(0, TOP_BAR_H, SCREEN_W, CONTENT_H, COL_BG);
    drawBottomNav(activePage);
  }

  drawStatusBar(gps, net, bufferedCount);

  switch (activePage) {
    case PAGE_ENV:
      drawEnvPage(env, full);
      break;
    case PAGE_GPS:
      drawGpsPage(gps, env, full);
      break;
    case PAGE_QR:
      drawQrPage(net, full);
      break;
    case PAGE_SYS:
      drawSysPage(env, gps, net, cfg, bufferedCount, full);
      break;
    default:
      break;
  }

  if (toastActive) {
    drawToastBanner(toastMsg);
    s_toastWasVisible = true;
  }
}
