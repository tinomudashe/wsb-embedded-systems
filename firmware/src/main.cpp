#include <Arduino.h>

#include "config.h"
#include "gps.h"
#include "net.h"
#include "portal.h"
#include "power.h"
#include "sdlog.h"
#include "sensors.h"
#include "settings.h"
#include "telemetry.h"
#include "ui.h"

namespace {

constexpr int PIN_BOOT_BTN = 0;
uint32_t s_seq = 0;
uint32_t s_lastSampleMs = 0;
bool s_bootBtnDown = false;
uint32_t s_bootBtnPressedAtMs = 0;
bool s_bootLongHandled = false;

}  // namespace

void setup() {
  Serial.begin(115200);
  delay(150);
  pinMode(PIN_BOOT_BTN, INPUT_PULLUP);

  settingsInit();
  DeviceConfig cfg = settingsGet();
  DeviceCredentials cred = settingsGetCredentials();

  Serial.println();
  Serial.println("============================================");
  Serial.printf("[BOOT] WSB Telemetry Node  FW=%s  ID=%s\n",
                FW_VERSION, cred.device_id.c_str());
  Serial.println("============================================");

  uiInit();
  powerInit(cfg);
  uiShowSplash();
  uiSplashStep(true, "Display: ILI9341 320x240");
  uiSplashStep(true, "Touch: XPT2046 (VSPI)");

  char splashBuf[48];
  snprintf(splashBuf, sizeof(splashBuf), "Settings: NVS cfg v%u", cfg.version);
  uiSplashStep(true, splashBuf);

  sensorsInit();
  char sensStr[32];
  sensorsSummaryString(sensStr, sizeof(sensStr));
  EnvReading env = sensorsGetReading();
  if (env.envChip != ENV_NONE || env.hasEns160) {
    snprintf(splashBuf, sizeof(splashBuf), "Sensors: %s", sensStr);
    uiSplashStep(true, splashBuf);
  } else {
    uiSplashStep(false, "Sensors: None detected");
  }

  gpsInit(cfg.gps_baud);
  snprintf(splashBuf, sizeof(splashBuf), "GPS UART2: %u baud", cfg.gps_baud);
  uiSplashStep(true, splashBuf);

  bool sdOk = sdlogInit();
  uiSplashStep(sdOk, sdOk ? "Storage: microSD ready" : "Storage: no SD card");

  telemetryInit();
  netInit();

  char bootDetail[96];
  if (strcmp(sensStr, "None") == 0) {
    snprintf(bootDetail, sizeof(bootDetail), "ESP32 CYD booted, no I2C sensors, GPS initialized");
  } else {
    snprintf(bootDetail, sizeof(bootDetail), "ESP32 CYD booted, %s/GPS initialized", sensStr);
  }
  netLogEvent("INFO", "boot", bootDetail);

  netStartTask();

  if (portalIsActive()) {
    uiSplashStep(false, "Wi-Fi: AP Setup (WSB-CYD-Setup)");
  } else {
    uiSplashStep(true, "Network: WiFi/MQTT task (Core 0)");
  }

  delay(1100);
  uiBeginLive(portalIsActive() ? PAGE_QR : PAGE_ENV);

  // Schedule first sample shortly after boot so live data arrives promptly
  s_lastSampleMs = millis();
}

void loop() {
  // Apply dynamic GPS baud change if config updated from HTTP/MQTT on Core 0
  uint32_t newBaud = netConsumePendingGpsBaud();
  if (newBaud != 0) {
    gpsSetBaud(newBaud);
  }

  DeviceConfig cfg = settingsGet();

  // Poll touch, physical BOOT button (GPIO 0), I2C sensors, and GPS UART2 on Core 1
  uiPollTouch();

  bool btnLow = (digitalRead(PIN_BOOT_BTN) == LOW);
  uint32_t btnNow = millis();
  if (btnLow && !s_bootBtnDown) {
    s_bootBtnDown = true;
    s_bootBtnPressedAtMs = btnNow;
    s_bootLongHandled = false;
  } else if (btnLow && s_bootBtnDown && !s_bootLongHandled && (btnNow - s_bootBtnPressedAtMs) >= 1500UL) {
    s_bootLongHandled = true;
    uiToggleInvertDisplay();
    uiShowToast(uiIsDisplayInverted() ? "Display Inv: ON" : "Display Inv: OFF", 2500);
    netLogEvent("INFO", "button", "BOOT long-press -> toggled display inversion");
    netRequestStatusPublish();
  } else if (!btnLow && s_bootBtnDown) {
    uint32_t heldMs = btnNow - s_bootBtnPressedAtMs;
    s_bootBtnDown = false;
    if (!s_bootLongHandled && heldMs >= 40UL) {
      if (!powerNotifyTouch()) {
        UiPage nextPage = static_cast<UiPage>((static_cast<int>(uiGetPage()) + 1) % PAGE_COUNT);
        uiSetPage(nextPage);
        uiShowToast("BOOT Button: Next Page", 2000);
      }
      netLogEvent("INFO", "button", "BOOT button pressed on board");
      netRequestStatusPublish();
      s_lastSampleMs = 0; // trigger immediate sample
    }
  }

  sensorsUpdate(cfg);
  gpsUpdate();

  NetStatus net = netGetStatus();
  powerUpdate(cfg, net.wifiConnected, net.mqttConnected, net.httpHealthy, netGetSendPulseSeq());

  EnvReading env = sensorsGetReading();
  GpsReading gps = gpsGetReading();

  // Periodic or remotely triggered telemetry capture (keeps buffering in AP mode too)
  uint32_t now = millis();
  uint32_t intervalMs = static_cast<uint32_t>(cfg.telemetry_interval_s) * 1000UL;
  bool forceSample = netConsumeForceSample();
  if (forceSample || (now - s_lastSampleMs) >= intervalMs) {
    s_lastSampleMs = now;
    ++s_seq;

    int64_t ntpEpochMs = 0;
    bool ntpOk = netGetNtpEpochMs(ntpEpochMs);
    bool canSendLive = net.mqttConnected || (net.wifiConnected && net.httpHealthy);

    TelemetrySample sample = telemetryBuildSample(s_seq, env, gps, cfg,
                                                  net.wifiConnected, net.rssi,
                                                  ntpOk, ntpEpochMs, canSendLive);
    telemetryBufferPush(sample);
    sdlogWriteSample(sample);
    Serial.printf("[TLM] Queued seq=%u ts_src=%u replay=%d buf=%u\n",
                  sample.seq, sample.ts_source, sample.replay ? 1 : 0,
                  static_cast<unsigned>(telemetryBufferCount()));
  }

  uiUpdate(env, gps, net, cfg, telemetryBufferCount());

  // Yield cleanly while still draining GPS UART2 frequently enough to prevent RX overflow
  delay(powerIsScreenOff() ? 50 : 20);
}
