#include "sdlog.h"

#include <HTTPClient.h>
#include <SD.h>
#include <SPI.h>
#include <WiFi.h>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include "config.h"
#include "net.h"
#include "settings.h"

namespace {

constexpr const char* CSV_HEADER =
    "ts,lat,lon,alt,speed_kmh,sats,hdop,temp_c,humidity,pressure_hpa,"
    "pm1,pm2_5,pm4,pm10,nc0_5,nc1,nc2_5,nc4,nc10,aqi";

constexpr size_t LINE_MAX = 240;
constexpr size_t BUFFER_MAX = SDLOG_FLUSH_SAMPLES * LINE_MAX;

SemaphoreHandle_t s_mutex = nullptr;
bool s_active = false;
char s_path[48] = "";
char s_buf[BUFFER_MAX];
size_t s_bufLen = 0;
size_t s_bufSamples = 0;
size_t s_pending = 0;
uint32_t s_dropped = 0;

const char* baseName(const char* path) {
  if (!path) return "";
  const char* slash = strrchr(path, '/');
  return slash ? slash + 1 : path;
}

bool isRideCsv(const char* name) {
  const char* base = baseName(name);
  size_t len = strlen(base);
  return len > 9 && strncmp(base, "ride_", 5) == 0 && strcmp(base + len - 4, ".csv") == 0;
}

bool takeMutex() {
  return s_mutex && xSemaphoreTake(s_mutex, portMAX_DELAY) == pdTRUE;
}

void giveMutex() {
  if (s_mutex) xSemaphoreGive(s_mutex);
}

// The TF slot and the XPT2046 touch panel share the VSPI peripheral. SD
// operations briefly route the bus to the TF pins and restore the touch pins
// afterwards; everything is serialized with s_mutex.
bool mountSdLocked() {
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
  if (!SD.begin(PIN_SD_CS, SPI, SD_SPI_FREQ_HZ)) {
    SPI.begin(PIN_TOUCH_CLK, PIN_TOUCH_MISO, PIN_TOUCH_MOSI, PIN_TOUCH_CS);
    return false;
  }
  return true;
}

void unmountSdLocked() {
  SD.end();
  SPI.begin(PIN_TOUCH_CLK, PIN_TOUCH_MISO, PIN_TOUCH_MOSI, PIN_TOUCH_CS);
}

size_t scanPendingLocked() {
  size_t count = 0;
  File dir = SD.open("/");
  if (!dir) return 0;
  for (;;) {
    File entry = dir.openNextFile();
    if (!entry) break;
    const char* name = entry.name();
    bool current = s_active && s_path[0] != '\0' && strcmp(baseName(name), baseName(s_path)) == 0;
    if (!entry.isDirectory() && isRideCsv(name) && !current) {
      ++count;
    }
    entry.close();
  }
  dir.close();
  return count;
}

bool flushLocked() {
  if (s_bufLen == 0 || s_path[0] == '\0') return true;
  if (!mountSdLocked()) return false;

  bool ok = false;
  File f = SD.open(s_path, FILE_APPEND);
  if (f) {
    size_t written = f.write(reinterpret_cast<const uint8_t*>(s_buf), s_bufLen);
    f.flush();
    f.close();
    ok = (written == s_bufLen);
  }
  unmountSdLocked();

  if (ok) {
    s_bufLen = 0;
    s_bufSamples = 0;
  }
  return ok;
}

void appendField(char* out, size_t outLen, bool valid, const char* fmt, double value) {
  if (valid) {
    snprintf(out, outLen, fmt, value);
  } else {
    out[0] = '\0';
  }
}

bool formatRow(const TelemetrySample& s, char* line, size_t lineLen) {
  if (s.ts_source == TS_NONE || s.ts_epoch_ms <= 0) return false;

  char ts[32];
  telemetryFormatIsoUtc(s.ts_epoch_ms, ts, sizeof(ts));

  char lat[16] = "";
  char lon[16] = "";
  char alt[14] = "";
  char speed[14] = "";
  char sats[8] = "";
  char hdop[12] = "";
  char temp[12] = "";
  char hum[12] = "";
  char pres[12] = "";
  char aqi[8] = "";

  bool fix = s.gps_fix;
  appendField(lat, sizeof(lat), fix, "%.6f", s.lat);
  appendField(lon, sizeof(lon), fix, "%.6f", s.lon);
  appendField(alt, sizeof(alt), fix && (s.gps_valid_mask & GPS_BIT_ALT), "%.1f", s.gps_alt);
  appendField(speed, sizeof(speed), fix && (s.gps_valid_mask & GPS_BIT_SPEED), "%.2f", s.speed_kmh);
  if (s.gps_valid_mask & GPS_BIT_SATS) {
    snprintf(sats, sizeof(sats), "%u", static_cast<unsigned>(s.sats));
  }
  appendField(hdop, sizeof(hdop), (s.gps_valid_mask & GPS_BIT_HDOP) != 0, "%.2f", s.hdop);
  appendField(temp, sizeof(temp), (s.env_valid_mask & ENV_BIT_TEMP) != 0, "%.2f", s.temp_c);
  appendField(hum, sizeof(hum), (s.env_valid_mask & ENV_BIT_HUM) != 0, "%.2f", s.humidity);
  appendField(pres, sizeof(pres), (s.env_valid_mask & ENV_BIT_PRES) != 0, "%.2f", s.pressure_hpa);
  if (s.env_valid_mask & ENV_BIT_AQI) {
    snprintf(aqi, sizeof(aqi), "%d", static_cast<int>(s.aqi));
  }

  // PM1..NC10 stay empty until an SPS30 (or similar) driver is added.
  int n = snprintf(line, lineLen,
                   "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,,,,,,,,,,%s\n",
                   ts, lat, lon, alt, speed, sats, hdop, temp, hum, pres, aqi);
  return n > 0 && static_cast<size_t>(n) < lineLen;
}

bool makeRidePath(char* out, size_t outLen, time_t stamp) {
  if (stamp < 1700000000L) return false;
  struct tm tmUtc;
  gmtime_r(&stamp, &tmUtc);
  snprintf(out, outLen, "/ride_%04d%02d%02d_%02d%02d%02d.csv",
           tmUtc.tm_year + 1900, tmUtc.tm_mon + 1, tmUtc.tm_mday,
           tmUtc.tm_hour, tmUtc.tm_min, tmUtc.tm_sec);
  return true;
}

}  // namespace

bool sdlogBusLock(uint32_t timeoutMs) {
  return s_mutex && xSemaphoreTake(s_mutex, pdMS_TO_TICKS(timeoutMs)) == pdTRUE;
}

void sdlogBusUnlock() {
  if (s_mutex) xSemaphoreGive(s_mutex);
}

bool sdlogInit() {
  if (!s_mutex) {
    s_mutex = xSemaphoreCreateMutex();
  }
  if (!s_mutex) return false;

  if (!takeMutex()) return false;
  bool ok = mountSdLocked();
  if (ok) {
    s_pending = scanPendingLocked();
    unmountSdLocked();
  }
  giveMutex();
  Serial.printf("[SD] init %s, pending rides=%u\n", ok ? "ok" : "failed",
                static_cast<unsigned>(s_pending));
  return ok;
}

bool sdlogStart() {
  if (!takeMutex()) return false;
  if (s_active) {
    giveMutex();
    return true;
  }

  time_t now = time(nullptr);
  char path[48] = "";
  if (!makeRidePath(path, sizeof(path), now)) {
    giveMutex();
    Serial.println("[SD] start refused: UTC clock not set");
    return false;
  }
  if (!mountSdLocked()) {
    giveMutex();
    Serial.println("[SD] start refused: card mount failed");
    return false;
  }

  // Avoid truncating a ride if start is tapped twice within the same second.
  for (int bump = 1; bump < 6 && path[0] != '\0' && SD.exists(path); ++bump) {
    if (!makeRidePath(path, sizeof(path), now + bump)) path[0] = '\0';
  }
  if (path[0] == '\0' || SD.exists(path)) {
    unmountSdLocked();
    giveMutex();
    Serial.println("[SD] start refused: could not allocate a unique filename");
    return false;
  }

  bool ok = false;
  File f = SD.open(path, FILE_WRITE);
  if (f) {
    f.print(CSV_HEADER);
    f.print("\n");
    f.flush();
    f.close();
    ok = true;
  }

  if (ok) {
    snprintf(s_path, sizeof(s_path), "%s", path);
    s_active = true;
    s_bufLen = 0;
    s_bufSamples = 0;
    s_pending = scanPendingLocked();  // excludes the file being recorded
    Serial.printf("[SD] recording -> %s\n", s_path);
  }
  unmountSdLocked();
  giveMutex();
  return ok;
}

void sdlogStop() {
  if (!takeMutex()) return;
  if (!s_active) {
    giveMutex();
    return;
  }

  bool ok = flushLocked();
  if (!ok) {
    Serial.println("[SD] final flush failed; buffered rows were lost");
  }
  Serial.printf("[SD] stopped %s (dropped=%lu)\n", s_path,
                static_cast<unsigned long>(s_dropped));
  s_active = false;
  s_path[0] = '\0';
  s_bufLen = 0;
  s_bufSamples = 0;

  if (mountSdLocked()) {
    s_pending = scanPendingLocked();
    unmountSdLocked();
  }
  giveMutex();
}

bool sdlogIsActive() {
  if (!takeMutex()) return false;
  bool active = s_active;
  giveMutex();
  return active;
}

void sdlogWriteSample(const TelemetrySample& sample) {
  char line[LINE_MAX];
  if (!formatRow(sample, line, sizeof(line))) {
    return;  // no valid UTC timestamp -> server would reject the row
  }
  size_t lineLen = strlen(line);

  if (!takeMutex()) return;
  if (!s_active) {
    giveMutex();
    return;
  }

  if (s_bufLen + lineLen >= sizeof(s_buf)) {
    if (!flushLocked()) {
      ++s_dropped;
      s_bufLen = 0;
      s_bufSamples = 0;
    }
  }

  if (s_bufLen + lineLen < sizeof(s_buf)) {
    memcpy(s_buf + s_bufLen, line, lineLen);
    s_bufLen += lineLen;
    ++s_bufSamples;
  } else {
    ++s_dropped;
  }

  if (s_bufSamples >= SDLOG_FLUSH_SAMPLES) {
    if (!flushLocked()) {
      ++s_dropped;
      s_bufLen = 0;
      s_bufSamples = 0;
    }
  }
  giveMutex();
}

size_t sdlogPendingCount() {
  if (!takeMutex()) return 0;
  size_t n = s_pending;
  giveMutex();
  return n;
}

bool sdlogUploadPending() {
  if (!s_mutex) return false;
  DeviceCredentials cred = settingsGetCredentials();
  if (WiFi.status() != WL_CONNECTED || cred.device_id.length() == 0 || cred.device_key.length() == 0) {
    return false;
  }

  char path[48] = "";
  char filename[48] = "";
  String body;

  if (!takeMutex()) return false;
  if (s_active || s_pending == 0) {
    giveMutex();
    return false;  // never switch the shared bus away from a running recording
  }
  if (!mountSdLocked()) {
    giveMutex();
    return false;
  }

  File dir = SD.open("/");
  if (dir) {
    for (;;) {
      File entry = dir.openNextFile();
      if (!entry) break;
      bool candidate = !entry.isDirectory() && isRideCsv(entry.name());
      if (candidate) {
        snprintf(path, sizeof(path), "/%s", baseName(entry.name()));
        snprintf(filename, sizeof(filename), "%s", baseName(entry.name()));
        size_t size = entry.size();
        if (size > 0 && size <= SDLOG_MAX_UPLOAD_BYTES) {
          body.reserve(size + 1);
          uint8_t chunk[256];
          while (entry.available()) {
            size_t n = entry.read(chunk, sizeof(chunk));
            if (n == 0) break;
            body.concat(reinterpret_cast<const char*>(chunk), n);
          }
        }
        entry.close();
        break;
      }
      entry.close();
    }
    dir.close();
  }

  if (path[0] == '\0') {
    s_pending = scanPendingLocked();
    unmountSdLocked();
    giveMutex();
    return false;
  }

  if (body.length() == 0) {
    // Zero-byte or oversized ride: move it aside so it cannot block the queue.
    char parked[56];
    snprintf(parked, sizeof(parked), "%s.big", path);
    SD.rename(path, parked);
    s_pending = scanPendingLocked();
    unmountSdLocked();
    giveMutex();
    netLogEvent("WARN", "sd", "Parked oversized/empty ride file");
    return false;
  }
  unmountSdLocked();
  giveMutex();

  WiFiClient client;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + String(HTTP_PORT) +
               "/api/devices/" + cred.device_id + "/rides";
  http.setTimeout(8000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    return false;
  }
  http.addHeader("Content-Type", "text/csv");
  http.addHeader("X-Device-Id", cred.device_id);
  http.addHeader("X-Device-Key", cred.device_key);
  http.addHeader("X-Filename", filename);

  int code = http.POST(body);
  String resp = (code > 0) ? http.getString() : "";
  http.end();

  if (code < 200 || code >= 300) {
    Serial.printf("[SD] ride upload %s failed -> %d (%s)\n", filename, code,
                  HTTPClient::errorToString(code).c_str());
    return false;
  }

  if (!takeMutex()) return true;
  if (mountSdLocked()) {
    char done[56];
    snprintf(done, sizeof(done), "%s.done", path);
    if (!SD.rename(path, done)) {
      Serial.printf("[SD] uploaded %s but rename to .done failed\n", path);
    }
    s_pending = scanPendingLocked();
    unmountSdLocked();
  }
  giveMutex();

  Serial.printf("[SD] uploaded %s -> %d %s\n", filename, code, resp.c_str());
  netLogEvent("INFO", "sd", "Ride CSV uploaded to dashboard");
  return true;
}
