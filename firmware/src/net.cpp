#include "net.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <atomic>
#include <ctime>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>
#include <freertos/task.h>
#include <sys/time.h>

#include "config.h"
#include "gps.h"
#include "portal.h"
#include "power.h"
#include "sdlog.h"
#include "settings.h"
#include "telemetry.h"
#include "ui.h"

namespace {

constexpr uint32_t WIFI_CONNECT_WINDOW_MS = 15000UL;
constexpr uint8_t WIFI_MAX_FAIL_ATTEMPTS = 3;
constexpr size_t LOG_BUFFER_CAPACITY = 16;

struct DeviceLogEntry {
  uint32_t ts_ms = 0;
  char level[12] = "INFO";
  char event[24] = "";
  char detail[96] = "";
};

SemaphoreHandle_t s_netMutex = nullptr;
SemaphoreHandle_t s_logMutex = nullptr;
TaskHandle_t s_netTaskHandle = nullptr;
NetStatus s_status;

DeviceLogEntry s_logRing[LOG_BUFFER_CAPACITY];
size_t s_logHead = 0;
size_t s_logCount = 0;

WiFiClient s_mqttWifiClient;
PubSubClient s_mqtt(s_mqttWifiClient);

char s_topicTelemetry[96];
char s_topicStatus[96];
char s_topicConfig[96];
char s_topicCmd[96];
char s_topicLogs[96];
char s_activeDeviceId[36] = "";
char s_lastOwnerUser[36] = "";

std::atomic<uint32_t> s_sendPulseSeq{0};
std::atomic<uint32_t> s_pendingGpsBaud{0};
std::atomic<bool> s_statusPublishNeeded{false};
std::atomic<bool> s_forceSampleNow{false};

bool s_ntpConfigured = false;
bool s_wasWifiConnected = false;
bool s_wasApActive = false;
bool s_bootstrapDone = false;
bool s_bootstrapLogSent = false;
bool s_hasServerAgps = false;
uint32_t s_lastWifiAttemptMs = 0;
uint32_t s_wifiBackoffMs = WIFI_CONNECT_WINDOW_MS;
uint8_t s_wifiFailAttempts = 0;
uint32_t s_lastBootstrapAttemptMs = 0;
uint32_t s_lastMqttAttemptMs = 0;
uint32_t s_mqttBackoffMs = 2000;
uint32_t s_lastHttpConfigAttemptMs = 0;
uint32_t s_lastHttpPostAttemptMs = 0;
uint32_t s_lastRideUploadMs = 0;
uint32_t s_httpBackoffMs = 2000;

template <typename Fn>
void updateStatusLocked(Fn fn) {
  if (!s_netMutex) return;
  xSemaphoreTake(s_netMutex, portMAX_DELAY);
  fn(s_status);
  xSemaphoreGive(s_netMutex);
}

void makeFallbackPairUrl(const char* devId, char* out, size_t outLen) {
  const char* id = (devId && devId[0] != '\0') ? devId : "cyd-01";
  snprintf(out, outLen, "%s/?device=%s", DASHBOARD_BASE_URL, id);
}

void syncDeviceTopics(const char* devId) {
  const char* id = (devId && devId[0] != '\0') ? devId : "cyd-01";
  if (strcmp(s_activeDeviceId, id) == 0) return;

  snprintf(s_activeDeviceId, sizeof(s_activeDeviceId), "%s", id);
  snprintf(s_topicTelemetry, sizeof(s_topicTelemetry), "wsb/devices/%s/telemetry", id);
  snprintf(s_topicStatus, sizeof(s_topicStatus), "wsb/devices/%s/status", id);
  snprintf(s_topicConfig, sizeof(s_topicConfig), "wsb/devices/%s/config", id);
  snprintf(s_topicCmd, sizeof(s_topicCmd), "wsb/devices/%s/cmd", id);
  snprintf(s_topicLogs, sizeof(s_topicLogs), "wsb/devices/%s/logs", id);
  if (s_mqtt.connected()) {
    s_mqtt.disconnect();
  }
}

void configureWifiStation() {
  WiFi.persistent(false);
  WiFi.setSleep(false);
  WiFi.setScanMethod(WIFI_ALL_CHANNEL_SCAN);
  WiFi.setSortMethod(WIFI_CONNECT_AP_BY_SIGNAL);
}

void flushDeviceLogs();

void publishOnlineStatus() {
  if (!s_mqtt.connected()) return;
  DeviceConfig cfg = settingsGet();
  ScreenState scr = powerGetScreenState();
  JsonDocument doc;
  doc["online"] = true;
  doc["fw"] = FW_VERSION;
  doc["ip"] = WiFi.localIP().toString();
  doc["cfg_version"] = cfg.version;
  doc["page"] = static_cast<int>(uiGetPage());
  doc["screen_state"] = (scr == SCREEN_OFF) ? "off" : (scr == SCREEN_DIM ? "dim" : "active");
  doc["backlight"] = powerGetBacklightDuty();
  doc["ldr"] = powerGetLdrRaw();
  doc["led_mode"] = powerGetLedMode();
  doc["inverted"] = uiIsDisplayInverted();

  char payload[256];
  size_t len = serializeJson(doc, payload, sizeof(payload));
  if (len > 0) {
    bool ok = s_mqtt.publish(s_topicStatus, payload, true);
    Serial.printf("[MQTT] Published retained online status (%s): %s\n",
                  ok ? "ok" : "fail", payload);
  }
}

void mqttCallback(char* topic, uint8_t* payload, unsigned int length) {
  if (!topic || !payload || length == 0) return;

  if (strcmp(topic, s_topicCmd) == 0) {
    Serial.printf("[MQTT] Received command on %s (%u bytes)\n", topic, length);
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, payload, length);
    if (err || !doc.is<JsonObject>()) {
      Serial.printf("[MQTT] Command JSON parse error: %s\n", err.c_str());
      return;
    }
    const char* cmd = doc["cmd"] | "";
    if (strcmp(cmd, "set_page") == 0) {
      int p = doc["page"] | 0;
      p = constrain(p, 0, static_cast<int>(PAGE_COUNT) - 1);
      uiSetPage(static_cast<UiPage>(p));
      netRequestStatusPublish();
      char detail[64];
      snprintf(detail, sizeof(detail), "Switched UI page to %d", p);
      netLogEvent("INFO", "cmd", detail);
    } else if (strcmp(cmd, "toast") == 0) {
      const char* msg = doc["message"] | "Remote ping";
      uiShowToast(msg, 4000);
      netLogEvent("INFO", "cmd", "Displayed remote toast");
    } else if (strcmp(cmd, "led") == 0) {
      const char* mode = doc["mode"] | "auto";
      powerSetLedOverride(mode);
      netRequestStatusPublish();
      char detail[64];
      snprintf(detail, sizeof(detail), "LED mode set to %s", powerGetLedMode());
      netLogEvent("INFO", "cmd", detail);
    } else if (strcmp(cmd, "screen") == 0) {
      if (!doc["brightness"].isNull()) {
        int b = constrain(doc["brightness"].as<int>(), 0, 255);
        settingsSetDisplayBrightness(static_cast<uint8_t>(b));
      }
      const char* st = doc["state"] | "wake";
      powerSetRemoteScreenState(st);
      netRequestStatusPublish();
      char detail[64];
      snprintf(detail, sizeof(detail), "Screen state set to %s", st);
      netLogEvent("INFO", "cmd", detail);
    } else if (strcmp(cmd, "sample_now") == 0) {
      s_forceSampleNow.store(true);
      netLogEvent("INFO", "cmd", "Immediate telemetry sample requested");
    } else if (strcmp(cmd, "start_ap") == 0) {
      portalStart();
      uiSetPage(PAGE_QR);
      netRequestStatusPublish();
      netLogEvent("INFO", "cmd", "Started SoftAP provisioning mode");
    } else if (strcmp(cmd, "stop_ap") == 0) {
      portalStop();
      netRequestStatusPublish();
      netLogEvent("INFO", "cmd", "Stopped SoftAP provisioning mode");
    } else if (strcmp(cmd, "invert") == 0) {
      uiToggleInvertDisplay();
      uiShowToast(uiIsDisplayInverted() ? "Display Inv: ON" : "Display Inv: OFF", 2500);
      netRequestStatusPublish();
      netLogEvent("INFO", "cmd", "Toggled display inversion");
    } else if (strcmp(cmd, "reboot") == 0) {
      netLogEvent("INFO", "cmd", "Remote reboot requested");
      flushDeviceLogs();
      delay(200);
      ESP.restart();
    } else {
      Serial.printf("[MQTT] Unknown cmd: %s\n", cmd);
    }
    return;
  }

  if (strcmp(topic, s_topicConfig) != 0) return;

  Serial.printf("[MQTT] Received config on %s (%u bytes)\n", topic, length);
  bool baudChanged = false;
  if (settingsApplyJson(reinterpret_cast<const char*>(payload), length, &baudChanged)) {
    DeviceConfig cfg = settingsGet();
    if (baudChanged) {
      s_pendingGpsBaud.store(cfg.gps_baud);
    }
    s_statusPublishNeeded.store(true);

    char logBuf[96];
    snprintf(logBuf, sizeof(logBuf), "Applied config v%u (mqtt)", cfg.version);
    netLogEvent("INFO", "config", logBuf);
  }
}

bool bootstrapDeviceHttp(const DeviceCredentials& cred) {
  WiFiClient client;
  HTTPClient http;
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d/api/device/bootstrap", SERVER_HOST, HTTP_PORT);

  JsonDocument reqDoc;
  reqDoc["mac"] = WiFi.macAddress();
  reqDoc["device_id"] = cred.device_id;
  reqDoc["device_key"] = cred.device_key;
  reqDoc["fw"] = FW_VERSION;

  String reqBody;
  serializeJson(reqDoc, reqBody);

  http.setTimeout(4000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    Serial.println("[HTTP] Failed to init POST /api/device/bootstrap");
    return false;
  }
  http.addHeader("Content-Type", "application/json");
  if (cred.device_id.length() > 0) {
    http.addHeader("X-Device-Id", cred.device_id);
  }
  if (cred.device_key.length() > 0) {
    http.addHeader("X-Device-Key", cred.device_key);
  }

  int code = http.POST(reqBody);
  String body = (code > 0) ? http.getString() : "";
  http.end();

  if (code >= 200 && code < 300) {
    JsonDocument respDoc;
    DeserializationError err = deserializeJson(respDoc, body);
    if (err || !respDoc.is<JsonObject>()) {
      Serial.printf("[BOOT] Bootstrap JSON parse error: %s\n", err.c_str());
      return false;
    }

    String nextId = respDoc["device_id"] | String("");
    String nextKey = respDoc["device_key"] | String("");
    nextId.trim();
    if (nextId.length() > 0 || nextKey.length() > 0) {
      if (settingsSaveDeviceAuth(nextId, nextKey)) {
        if (nextId.length() > 0) {
          syncDeviceTopics(nextId.c_str());
        }
      }
    }

    String activeId = (nextId.length() > 0) ? nextId : cred.device_id;
    String pairUrl = respDoc["pair_url"] | String("");
    String claimCode = respDoc["claim_code"] | String("");
    String ownerUser = respDoc["owner_username"] | String("");
    pairUrl.trim();
    claimCode.trim();
    ownerUser.trim();

    bool ownerChanged = false;
    if (ownerUser.length() > 0) {
      if (strcmp(s_lastOwnerUser, ownerUser.c_str()) != 0) {
        snprintf(s_lastOwnerUser, sizeof(s_lastOwnerUser), "%s", ownerUser.c_str());
        ownerChanged = true;
        char toastBuf[64];
        snprintf(toastBuf, sizeof(toastBuf), "Signed in: %s", ownerUser.c_str());
        uiShowToast(toastBuf);
      }
    } else {
      s_lastOwnerUser[0] = '\0';
    }

    if (!s_bootstrapLogSent || ownerChanged) {
      s_bootstrapLogSent = true;
      char logBuf[96];
      snprintf(logBuf, sizeof(logBuf), "Paired / owner: %s (id=%s)",
               ownerUser.length() > 0 ? ownerUser.c_str() : "unclaimed",
               activeId.c_str());
      netLogEvent("INFO", "auth", logBuf);
    }

    if (respDoc["agps"].is<JsonObject>()) {
      JsonObject agps = respDoc["agps"].as<JsonObject>();
      if (!agps["lat"].isNull() && !agps["lon"].isNull()) {
        double lat = agps["lat"].as<double>();
        double lon = agps["lon"].as<double>();
        float alt = agps["alt"].isNull() ? 35.0f : agps["alt"].as<float>();
        uint8_t sats = agps["sats"].isNull() ? 7 : agps["sats"].as<uint8_t>();
        float hdop = agps["hdop"].isNull() ? 1.1f : agps["hdop"].as<float>();
        gpsSetAssistedFix(lat, lon, alt, sats, hdop);
        s_hasServerAgps = true;
      }
    } else if (!s_hasServerAgps) {
      gpsSetAssistedFix(54.3757, 18.5987, 35.0f, 7, 1.1f);
    }

    updateStatusLocked([&](NetStatus& st) {
      st.bootstrapOk = true;
      st.httpHealthy = true;
      snprintf(st.deviceId, sizeof(st.deviceId), "%s", activeId.c_str());
      if (pairUrl.length() > 0) {
        snprintf(st.pairUrl, sizeof(st.pairUrl), "%s", pairUrl.c_str());
      } else {
        makeFallbackPairUrl(activeId.c_str(), st.pairUrl, sizeof(st.pairUrl));
      }
      snprintf(st.claimCode, sizeof(st.claimCode), "%s", claimCode.c_str());
    });

    Serial.printf("[BOOT] Bootstrap OK: id=%s owner=%s code=%s url=%s\n",
                  activeId.c_str(),
                  ownerUser.length() > 0 ? ownerUser.c_str() : "-",
                  claimCode.length() > 0 ? claimCode.c_str() : "-",
                  pairUrl.length() > 0 ? pairUrl.c_str() : "(fallback)");
    return true;
  }

  Serial.printf("[HTTP] POST /api/device/bootstrap failed -> %d (%s)\n",
                code, HTTPClient::errorToString(code).c_str());
  updateStatusLocked([&](NetStatus& st) {
    st.bootstrapOk = false;
    if (st.pairUrl[0] == '\0') {
      makeFallbackPairUrl(cred.device_id.c_str(), st.pairUrl, sizeof(st.pairUrl));
    }
  });
  return false;
}

bool fetchBootConfigHttp(const DeviceCredentials& cred) {
  if (cred.device_id.length() == 0 || cred.device_key.length() == 0) {
    return false;
  }

  WiFiClient client;
  HTTPClient http;
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d/api/device/config", SERVER_HOST, HTTP_PORT);

  http.setTimeout(4000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    Serial.println("[HTTP] Failed to init GET /api/device/config");
    return false;
  }
  http.addHeader("X-Device-Id", cred.device_id);
  http.addHeader("X-Device-Key", cred.device_key);

  int code = http.GET();
  if (code >= 200 && code < 300) {
    String body = http.getString();
    http.end();
    Serial.printf("[HTTP] Boot config GET -> %d (%u bytes)\n", code, body.length());

    bool baudChanged = false;
    if (settingsApplyJson(body.c_str(), body.length(), &baudChanged)) {
      DeviceConfig cfg = settingsGet();
      if (baudChanged) {
        s_pendingGpsBaud.store(cfg.gps_baud);
      }
      s_statusPublishNeeded.store(true);

      char logBuf[96];
      snprintf(logBuf, sizeof(logBuf), "Applied config v%u (http)", cfg.version);
      netLogEvent("INFO", "config", logBuf);
    }
    updateStatusLocked([](NetStatus& st) {
      st.bootConfigFetched = true;
      st.httpHealthy = true;
    });
    return true;
  }

  Serial.printf("[HTTP] Boot config GET failed: %d (%s)\n",
                code, HTTPClient::errorToString(code).c_str());
  http.end();
  return false;
}

bool postBatchHttp(const DeviceCredentials& cred, const TelemetrySample* samples, size_t count) {
  if (!samples || count == 0 || cred.device_id.length() == 0 || cred.device_key.length() == 0) {
    return false;
  }

  String body;
  if (!telemetrySerializeArray(samples, count, body)) {
    Serial.println("[HTTP] Failed to serialize batch JSON");
    return false;
  }

  WiFiClient client;
  HTTPClient http;
  char url[128];
  snprintf(url, sizeof(url), "http://%s:%d/api/ingest", SERVER_HOST, HTTP_PORT);

  http.setTimeout(5000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    return false;
  }
  http.addHeader("X-Device-Id", cred.device_id);
  http.addHeader("X-Device-Key", cred.device_key);
  http.addHeader("Content-Type", "application/json");

  int code = http.POST(body);
  String resp = (code > 0) ? http.getString() : "";
  http.end();

  if (code >= 200 && code < 300) {
    Serial.printf("[HTTP] POST /api/ingest (%u items) -> %d %s\n",
                  static_cast<unsigned>(count), code, resp.c_str());
    return true;
  }

  Serial.printf("[HTTP] POST /api/ingest (%u items) failed -> %d (%s)\n",
                static_cast<unsigned>(count), code,
                HTTPClient::errorToString(code).c_str());
  return false;
}

void ensureWifi(const DeviceCredentials& cred) {
  if (cred.wifi_ssid.length() == 0) {
    return;
  }

  uint32_t now = millis();
  if (WiFi.status() == WL_CONNECTED) {
    s_wifiFailAttempts = 0;
    s_wifiBackoffMs = WIFI_CONNECT_WINDOW_MS;
    if (!s_ntpConfigured) {
      configTime(0, 0, "pool.ntp.org", "time.nist.gov");
      s_ntpConfigured = true;
      Serial.println("[NET] Configured NTP (pool.ntp.org, UTC)");
    }
    return;
  }

  // First attempt starts immediately; subsequent attempts only after a full 15s window
  if (s_lastWifiAttemptMs == 0) {
    s_lastWifiAttemptMs = now;
    s_wifiBackoffMs = WIFI_CONNECT_WINDOW_MS;
    Serial.printf("[WIFI] Connecting to SSID '%s' (attempt 1/%u, 15s window)...\n",
                  cred.wifi_ssid.c_str(), WIFI_MAX_FAIL_ATTEMPTS);
    configureWifiStation();
    WiFi.begin(cred.wifi_ssid.c_str(), cred.wifi_pass.c_str());
    return;
  }

  if ((now - s_lastWifiAttemptMs) >= s_wifiBackoffMs) {
    ++s_wifiFailAttempts;
    if (s_wifiFailAttempts >= WIFI_MAX_FAIL_ATTEMPTS) {
      Serial.printf("[WIFI] %u consecutive 15s attempts failed -> entering AP setup\n",
                    s_wifiFailAttempts);
      s_wifiFailAttempts = 0;
      s_lastWifiAttemptMs = 0;
      netLogEvent("WARN", "wifi", "Wi-Fi connect failed after 3 attempts; starting AP setup");
      portalStart();
      return;
    }

    s_lastWifiAttemptMs = now;
    Serial.printf("[WIFI] Retrying SSID '%s' (attempt %u/%u, 15s window)...\n",
                  cred.wifi_ssid.c_str(), s_wifiFailAttempts + 1, WIFI_MAX_FAIL_ATTEMPTS);
    WiFi.disconnect(false);
    configureWifiStation();
    WiFi.begin(cred.wifi_ssid.c_str(), cred.wifi_pass.c_str());
  }
}

void ensureMqtt(const DeviceCredentials& cred) {
  if (WiFi.status() != WL_CONNECTED ||
      cred.device_id.length() == 0 ||
      cred.device_key.length() == 0) {
    if (s_mqtt.connected()) {
      s_mqtt.disconnect();
    }
    return;
  }

  syncDeviceTopics(cred.device_id.c_str());

  if (s_mqtt.connected()) {
    s_mqttBackoffMs = 2000;
    s_mqtt.loop();
    if (s_statusPublishNeeded.exchange(false)) {
      publishOnlineStatus();
    }
    return;
  }

  uint32_t now = millis();
  if (s_lastMqttAttemptMs == 0 || (now - s_lastMqttAttemptMs) >= s_mqttBackoffMs) {
    s_lastMqttAttemptMs = now;
    Serial.printf("[MQTT] Connecting to %s:%d as %s...\n",
                  SERVER_HOST, MQTT_PORT, cred.device_id.c_str());
    bool ok = s_mqtt.connect(cred.device_id.c_str(),
                             cred.device_id.c_str(),
                             cred.device_key.c_str(),
                             s_topicStatus,
                             1,
                             true,
                             "{\"online\":false}");
    if (ok) {
      Serial.println("[MQTT] Connected");
      s_mqttBackoffMs = 2000;
      publishOnlineStatus();
      bool subCfgOk = s_mqtt.subscribe(s_topicConfig, 1);
      bool subCmdOk = s_mqtt.subscribe(s_topicCmd, 1);
      Serial.printf("[MQTT] Subscribed config=%s cmd=%s\n",
                    subCfgOk ? "ok" : "fail", subCmdOk ? "ok" : "fail");
      uiShowToast("Connected to WSB Server");
      netLogEvent("INFO", "mqtt", "MQTT session established");
    } else {
      Serial.printf("[MQTT] Connect failed, rc=%d (retry in %ums)\n",
                    s_mqtt.state(), s_mqttBackoffMs);
      s_mqttBackoffMs = min<uint32_t>(s_mqttBackoffMs * 2, 30000UL);
    }
  }
}

void flushDeviceLogs() {
  if (!s_mqtt.connected() || !s_logMutex) return;

  for (size_t sent = 0; sent < 4; ++sent) {
    DeviceLogEntry entry;
    bool hasEntry = false;
    xSemaphoreTake(s_logMutex, portMAX_DELAY);
    if (s_logCount > 0) {
      entry = s_logRing[s_logHead];
      hasEntry = true;
    }
    xSemaphoreGive(s_logMutex);

    if (!hasEntry) break;

    JsonDocument doc;
    doc["ts_ms"] = entry.ts_ms;
    doc["level"] = entry.level;
    doc["event"] = entry.event;
    doc["detail"] = entry.detail;

    char jsonBuf[256];
    size_t len = serializeJson(doc, jsonBuf, sizeof(jsonBuf));
    if (len == 0 || len >= sizeof(jsonBuf)) {
      // Drop malformed/oversized log entry
      xSemaphoreTake(s_logMutex, portMAX_DELAY);
      if (s_logCount > 0) {
        s_logHead = (s_logHead + 1) % LOG_BUFFER_CAPACITY;
        --s_logCount;
      }
      xSemaphoreGive(s_logMutex);
      continue;
    }

    if (s_mqtt.publish(s_topicLogs, jsonBuf, false)) {
      xSemaphoreTake(s_logMutex, portMAX_DELAY);
      if (s_logCount > 0) {
        s_logHead = (s_logHead + 1) % LOG_BUFFER_CAPACITY;
        --s_logCount;
      }
      xSemaphoreGive(s_logMutex);
    } else {
      break;
    }
  }
}

void flushTelemetry(const DeviceCredentials& cred) {
  if (WiFi.status() != WL_CONNECTED) return;

  size_t pending = telemetryBufferCount();
  if (pending == 0) return;

  if (s_mqtt.connected()) {
    const size_t maxPerCycle = 5;
    TelemetrySample batch[maxPerCycle];
    size_t n = telemetryBufferPeekBatch(batch, maxPerCycle);
    static char jsonBuf[MQTT_BUFFER_SIZE];

    size_t sentCount = 0;
    for (size_t i = 0; i < n; ++i) {
      size_t len = telemetrySerializeSingle(batch[i], jsonBuf, sizeof(jsonBuf));
      if (len == 0 || len >= sizeof(jsonBuf)) {
        Serial.println("[MQTT] Telemetry serialization overflow; dropping corrupt item");
        ++sentCount;
        continue;
      }
      if (s_mqtt.publish(s_topicTelemetry, jsonBuf, false)) {
        ++sentCount;
        s_sendPulseSeq.fetch_add(1);
        updateStatusLocked([](NetStatus& st) { ++st.mqttSent; });
        s_mqtt.loop();
        vTaskDelay(pdMS_TO_TICKS(20));
      } else {
        Serial.println("[MQTT] Publish failed");
        telemetryBufferMarkReplay(n - i);
        updateStatusLocked([](NetStatus& st) { ++st.mqttFailed; });
        break;
      }
    }
    if (sentCount > 0) {
      telemetryBufferPopCount(sentCount);
    }
    return;
  }

  uint32_t now = millis();
  if (s_lastHttpPostAttemptMs != 0 && (now - s_lastHttpPostAttemptMs) < s_httpBackoffMs) {
    return;
  }
  s_lastHttpPostAttemptMs = now;

  static TelemetrySample batch[HTTP_BATCH_LIMIT];
  size_t n = telemetryBufferPeekBatch(batch, HTTP_BATCH_LIMIT);
  if (n == 0) return;

  if (postBatchHttp(cred, batch, n)) {
    telemetryBufferPopCount(n);
    s_httpBackoffMs = 1000;
    s_sendPulseSeq.fetch_add(1);
    updateStatusLocked([n](NetStatus& st) {
      st.httpSent += static_cast<uint32_t>(n);
      st.httpHealthy = true;
    });
  } else {
    telemetryBufferMarkReplay(n);
    s_httpBackoffMs = min<uint32_t>(max<uint32_t>(s_httpBackoffMs * 2, 5000UL), 30000UL);
    updateStatusLocked([n](NetStatus& st) {
      st.httpFailed += static_cast<uint32_t>(n);
      st.httpHealthy = false;
    });
  }
}

void netTaskFn(void* param) {
  (void)param;
  Serial.printf("[NET] Network task started on core %d\n", xPortGetCoreID());

  for (;;) {
    portalUpdate();

    if (portalConsumeCredentialsSaved()) {
      DeviceCredentials cred = settingsGetCredentials();
      syncDeviceTopics(cred.device_id.c_str());
      s_lastWifiAttemptMs = 0;
      s_wifiFailAttempts = 0;
      s_wifiBackoffMs = WIFI_CONNECT_WINDOW_MS;
      s_lastBootstrapAttemptMs = 0;
      s_bootstrapDone = false;
      s_bootstrapLogSent = false;
      s_lastOwnerUser[0] = '\0';
      s_lastHttpConfigAttemptMs = 0;
      s_lastMqttAttemptMs = 0;
      s_mqttBackoffMs = 2000;
      updateStatusLocked([&](NetStatus& st) {
        st.bootConfigFetched = false;
        st.bootstrapOk = false;
        st.claimCode[0] = '\0';
        snprintf(st.ssid, sizeof(st.ssid), "%s", cred.wifi_ssid.c_str());
        snprintf(st.deviceId, sizeof(st.deviceId), "%s", cred.device_id.c_str());
        makeFallbackPairUrl(cred.device_id.c_str(), st.pairUrl, sizeof(st.pairUrl));
      });
    }

    DeviceCredentials cred = settingsGetCredentials();
    bool apActive = portalIsActive();

    if (apActive) {
      s_wasApActive = true;
      if (s_mqtt.connected()) {
        s_mqtt.disconnect();
      }
      s_wasWifiConnected = false;
      updateStatusLocked([&](NetStatus& st) {
        st.apActive = true;
        st.wifiConnected = false;
        st.mqttConnected = false;
        st.rssi = 0;
        snprintf(st.ip, sizeof(st.ip), "192.168.4.1");
        snprintf(st.ssid, sizeof(st.ssid), "%s", cred.wifi_ssid.c_str());
        snprintf(st.deviceId, sizeof(st.deviceId), "%s", cred.device_id.c_str());
        if (st.pairUrl[0] == '\0') {
          makeFallbackPairUrl(cred.device_id.c_str(), st.pairUrl, sizeof(st.pairUrl));
        }
      });
      vTaskDelay(pdMS_TO_TICKS(15));
      continue;
    }

    if (s_wasApActive) {
      s_wasApActive = false;
      s_wifiFailAttempts = 0;
      s_lastWifiAttemptMs = 0;
    }

    ensureWifi(cred);

    bool wifiOk = (WiFi.status() == WL_CONNECTED);
    if (wifiOk) {
      uint32_t now = millis();
      if (!s_wasWifiConnected) {
        s_wasWifiConnected = true;
        s_bootstrapDone = false;
        s_lastBootstrapAttemptMs = 0;
        if (!s_hasServerAgps) {
          gpsSetAssistedFix(54.3757, 18.5987, 35.0f, 7, 1.1f);
        }
        uiShowToast("Wi-Fi connected!");

        char logBuf[96];
        snprintf(logBuf, sizeof(logBuf), "Connected to SSID %s IP %s",
                 cred.wifi_ssid.c_str(), WiFi.localIP().toString().c_str());
        netLogEvent("INFO", "wifi", logBuf);
      }

      // Bootstrap on connect (and refresh every 10 minutes while online)
      bool needBootstrap = !s_bootstrapDone
                               ? (s_lastBootstrapAttemptMs == 0 ||
                                  (now - s_lastBootstrapAttemptMs) >= 30000UL)
                               : ((now - s_lastBootstrapAttemptMs) >= BOOTSTRAP_REFRESH_MS);
      if (needBootstrap && !portalIsActive()) {
        s_lastBootstrapAttemptMs = now;
        if (bootstrapDeviceHttp(cred)) {
          s_bootstrapDone = true;
          cred = settingsGetCredentials();
        }
      }

      // Fetch boot config once via HTTP until succeeded
      bool bootFetched = false;
      updateStatusLocked([&bootFetched](NetStatus& st) { bootFetched = st.bootConfigFetched; });
      if (!bootFetched && !portalIsActive()) {
        if (s_lastHttpConfigAttemptMs == 0 || (now - s_lastHttpConfigAttemptMs) >= 15000UL) {
          s_lastHttpConfigAttemptMs = now;
          fetchBootConfigHttp(cred);
        }
      }
    } else {
      if (s_wasWifiConnected) {
        s_wasWifiConnected = false;
        s_lastWifiAttemptMs = 0;
        s_wifiFailAttempts = 0;
      }
    }

    if (!portalIsActive()) {
      ensureMqtt(cred);
      flushDeviceLogs();
      flushTelemetry(cred);

      // Upload one finished ride CSV at most once a minute. This runs on the
      // net task, so the UI loop keeps rendering. Live samples stay in the ring
      // buffer during the HTTP request.
      uint32_t now = millis();
      if (wifiOk && (s_lastRideUploadMs == 0 ||
                     (now - s_lastRideUploadMs) >= SDLOG_UPLOAD_INTERVAL_MS)) {
        s_lastRideUploadMs = now;
        sdlogRefreshPending();
        if (sdlogPendingCount() > 0) {
          sdlogUploadPending();
        }
      }
    }

    // Sync observable NetStatus snapshot for Core 1 UI/telemetry
    int64_t dummyEpoch = 0;
    bool ntpOk = netGetNtpEpochMs(dummyEpoch);
    int16_t rssi = wifiOk ? static_cast<int16_t>(WiFi.RSSI()) : 0;
    String ipStr = wifiOk ? WiFi.localIP().toString() : String("0.0.0.0");
    bool mqttOk = s_mqtt.connected();

    updateStatusLocked([&](NetStatus& st) {
      st.apActive = false;
      st.wifiConnected = wifiOk;
      st.rssi = rssi;
      snprintf(st.ip, sizeof(st.ip), "%s", ipStr.c_str());
      snprintf(st.ssid, sizeof(st.ssid), "%s", cred.wifi_ssid.c_str());
      snprintf(st.deviceId, sizeof(st.deviceId), "%s", cred.device_id.c_str());
      if (st.pairUrl[0] == '\0') {
        makeFallbackPairUrl(cred.device_id.c_str(), st.pairUrl, sizeof(st.pairUrl));
      }
      st.ntpSynced = ntpOk;
      st.mqttConnected = mqttOk;
    });

    vTaskDelay(pdMS_TO_TICKS(40));
  }
}

}  // namespace

void netInit() {
  if (!s_netMutex) {
    s_netMutex = xSemaphoreCreateMutex();
  }
  if (!s_logMutex) {
    s_logMutex = xSemaphoreCreateMutex();
  }

  portalInit();
  DeviceCredentials cred = settingsGetCredentials();
  syncDeviceTopics(cred.device_id.c_str());

  updateStatusLocked([&](NetStatus& st) {
    snprintf(st.ssid, sizeof(st.ssid), "%s", cred.wifi_ssid.c_str());
    snprintf(st.deviceId, sizeof(st.deviceId), "%s", cred.device_id.c_str());
    makeFallbackPairUrl(cred.device_id.c_str(), st.pairUrl, sizeof(st.pairUrl));
  });

  WiFi.setAutoReconnect(false);
  configureWifiStation();
  if (!settingsHasWifiCredentials()) {
    portalStart();
    updateStatusLocked([](NetStatus& st) {
      st.apActive = true;
      snprintf(st.ip, sizeof(st.ip), "192.168.4.1");
    });
  } else {
    WiFi.mode(WIFI_STA);
  }

  s_mqtt.setServer(SERVER_HOST, MQTT_PORT);
  s_mqtt.setBufferSize(MQTT_BUFFER_SIZE);
  s_mqtt.setKeepAlive(MQTT_KEEPALIVE_S);
  s_mqtt.setSocketTimeout(4);
  s_mqtt.setCallback(mqttCallback);
}

void netStartTask() {
  if (s_netTaskHandle != nullptr) return;
  xTaskCreatePinnedToCore(netTaskFn, "net_task", 10240, nullptr, 1, &s_netTaskHandle, 0);
}

NetStatus netGetStatus() {
  if (!s_netMutex) return s_status;
  xSemaphoreTake(s_netMutex, portMAX_DELAY);
  NetStatus copy = s_status;
  xSemaphoreGive(s_netMutex);
  return copy;
}

bool netGetNtpEpochMs(int64_t& outEpochMs) {
  struct timeval tv;
  if (gettimeofday(&tv, nullptr) == 0 && tv.tv_sec > 1700000000L) {
    outEpochMs = static_cast<int64_t>(tv.tv_sec) * 1000LL +
                 static_cast<int64_t>(tv.tv_usec / 1000LL);
    return true;
  }
  return false;
}

uint32_t netGetSendPulseSeq() {
  return s_sendPulseSeq.load();
}

uint32_t netConsumePendingGpsBaud() {
  return s_pendingGpsBaud.exchange(0);
}

bool netConsumeForceSample() {
  return s_forceSampleNow.exchange(false);
}

void netRequestStatusPublish() {
  s_statusPublishNeeded.store(true);
}

void netLogEvent(const char* level, const char* event, const char* detail) {
  if (!s_logMutex) {
    s_logMutex = xSemaphoreCreateMutex();
  }
  if (!s_logMutex) return;

  DeviceLogEntry entry;
  entry.ts_ms = millis();
  snprintf(entry.level, sizeof(entry.level), "%s", (level && level[0]) ? level : "INFO");
  snprintf(entry.event, sizeof(entry.event), "%s", (event && event[0]) ? event : "log");
  snprintf(entry.detail, sizeof(entry.detail), "%s", detail ? detail : "");

  xSemaphoreTake(s_logMutex, portMAX_DELAY);
  if (s_logCount == LOG_BUFFER_CAPACITY) {
    s_logHead = (s_logHead + 1) % LOG_BUFFER_CAPACITY;
    --s_logCount;
  }
  size_t tail = (s_logHead + s_logCount) % LOG_BUFFER_CAPACITY;
  s_logRing[tail] = entry;
  ++s_logCount;
  xSemaphoreGive(s_logMutex);
}
