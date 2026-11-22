#include "sdlog.h"

#include <ArduinoJson.h>
#include <HTTPClient.h>
#include <SD.h>
#include <SPI.h>
#include <WiFi.h>
#include <atomic>
#include <cstdio>
#include <cstring>
#include <ctime>
#include <freertos/FreeRTOS.h>
#include <freertos/semphr.h>

#include "config.h"
#include "gps.h"
#include "net.h"
#include "settings.h"

namespace {

constexpr const char* CSV_HEADER =
    "ts,lat,lon,alt,speed_kmh,sats,hdop,temp_c,humidity,pressure_hpa,"
    "pm1,pm2_5,pm4,pm10,nc0_5,nc1,nc2_5,nc4,nc10,aqi,gas_kohm,eco2_ppm,tvoc_ppb";

constexpr size_t SDLOG_LINE_MAX = 280;
constexpr size_t BUFFER_MAX = SDLOG_FLUSH_SAMPLES * SDLOG_LINE_MAX;

SemaphoreHandle_t s_mutex = nullptr;
std::atomic<bool> s_active{false};
std::atomic<bool> s_uploading{false};
// Set while the bus is routed to the SD pins but the state mutex is released
// (during the streaming HTTP upload). Touch code must not touch SPI then.
std::atomic<bool> s_busBusy{false};
std::atomic<size_t> s_pending{0};
std::atomic<size_t> s_problems{0};

bool s_mounted = false;
bool s_fileCreated = false;
char s_path[48] = "";
char s_buf[BUFFER_MAX];
size_t s_bufLen = 0;
size_t s_bufSamples = 0;
uint32_t s_dropped = 0;

struct UploadGuard {
  ~UploadGuard() { s_uploading.store(false); }
};

const char* baseName(const char* path) {
  if (!path) return "";
  const char* slash = strrchr(path, '/');
  return slash ? slash + 1 : path;
}

bool endsWith(const char* text, const char* suffix) {
  size_t textLen = strlen(text);
  size_t suffixLen = strlen(suffix);
  return textLen >= suffixLen && strcmp(text + textLen - suffixLen, suffix) == 0;
}

bool isRideCsv(const char* name) {
  const char* base = baseName(name);
  return strlen(base) > 9 && strncmp(base, "ride_", 5) == 0 && endsWith(base, ".csv");
}

bool isProblemFile(const char* name) {
  const char* base = baseName(name);
  return endsWith(base, ".csv.big") || endsWith(base, ".csv.bad");
}

// Uploaded rides are removed after a verified upload. A file that could not be
// removed is renamed to this suffix and pruned on a later mount/upload pass.
bool isDoneFile(const char* name) {
  const char* base = baseName(name);
  return endsWith(base, ".csv.done");
}

bool takeMutex() {
  return s_mutex && xSemaphoreTake(s_mutex, portMAX_DELAY) == pdTRUE;
}

void giveMutex() {
  if (s_mutex) xSemaphoreGive(s_mutex);
}

// The TF slot and the XPT2046 touch panel share the VSPI peripheral. While a
// recording is active the card stays mounted; the pins are switched to the SD
// side only for actual file I/O and restored to the touch side afterwards.
void busToSdLocked() {
  SPI.begin(PIN_SD_SCK, PIN_SD_MISO, PIN_SD_MOSI, PIN_SD_CS);
}

void busToTouchLocked() {
  SPI.begin(PIN_TOUCH_CLK, PIN_TOUCH_MISO, PIN_TOUCH_MOSI, PIN_TOUCH_CS);
}

bool mountSdLocked() {
  busToSdLocked();
  if (!SD.begin(PIN_SD_CS, SPI, SD_SPI_FREQ_HZ)) {
    busToTouchLocked();
    return false;
  }
  s_mounted = true;
  return true;
}

void releaseSdLocked() {
  if (s_mounted) {
    SD.end();
    s_mounted = false;
  }
  busToTouchLocked();
}

void scanLocked() {
  size_t pending = 0;
  size_t problems = 0;
  File dir = SD.open("/");
  if (dir) {
    for (;;) {
      File entry = dir.openNextFile();
      if (!entry) break;
      const char* name = entry.name();
      bool current = s_active.load() && s_path[0] != '\0' &&
                     strcmp(baseName(name), baseName(s_path)) == 0;
      if (!entry.isDirectory()) {
        if (isRideCsv(name) && !current) {
          ++pending;
        } else if (isProblemFile(name)) {
          ++problems;
        }
      }
      entry.close();
    }
    dir.close();
  }
  s_pending.store(pending);
  s_problems.store(problems);
}

bool flushLocked() {
  if (s_bufLen == 0) return true;
  if (!s_mounted && !mountSdLocked()) return false;
  if (s_path[0] == '\0') return false;

  busToSdLocked();
  File f = SD.open(s_path, s_fileCreated ? FILE_APPEND : FILE_WRITE);
  bool ok = false;
  if (f) {
    if (!s_fileCreated) {
      f.print(CSV_HEADER);
      f.print("\n");
    }
    size_t written = f.write(reinterpret_cast<const uint8_t*>(s_buf), s_bufLen);
    f.flush();
    f.close();
    ok = (written == s_bufLen);
    if (ok) s_fileCreated = true;
  }
  busToTouchLocked();

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
  char gas[12] = "";
  char eco2[8] = "";
  char tvoc[8] = "";

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
  appendField(gas, sizeof(gas), (s.env_valid_mask & ENV_BIT_GAS) != 0, "%.2f", s.gas_kohm);
  if (s.env_valid_mask & ENV_BIT_ECO2) {
    snprintf(eco2, sizeof(eco2), "%u", static_cast<unsigned>(s.eco2_ppm));
  }
  if (s.env_valid_mask & ENV_BIT_TVOC) {
    snprintf(tvoc, sizeof(tvoc), "%u", static_cast<unsigned>(s.tvoc_ppb));
  }

  // PM1..NC10 stay empty until an SPS30 (or similar) driver is added.
  int n = snprintf(line, lineLen,
                   "%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,,,,,,,,,,%s,%s,%s,%s\n",
                   ts, lat, lon, alt, speed, sats, hdop, temp, hum, pres, aqi, gas, eco2, tvoc);
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

time_t bestEpoch() {
  time_t now = time(nullptr);
  if (now >= 1700000000L) return now;
  GpsReading gps = gpsGetReading();
  if (gps.utcValid && gps.utcEpochMs > 0) {
    return static_cast<time_t>(gps.utcEpochMs / 1000LL);
  }
  return 0;
}

bool countDataRows(File& f, size_t& rows) {
  if (!f.seek(0)) return false;
  size_t newlines = 0;
  int last = '\n';
  uint8_t chunk[256];
  for (;;) {
    int n = f.read(chunk, sizeof(chunk));
    if (n <= 0) break;
    for (int i = 0; i < n; ++i) {
      if (chunk[i] == '\n') ++newlines;
    }
    last = chunk[n - 1];
  }
  if (!f.seek(0)) return false;
  size_t lines = newlines + ((last != '\n') ? 1 : 0);
  rows = (lines > 0) ? lines - 1 : 0;  // exclude the header
  return true;
}

bool renameFileLocked(const char* path, const char* suffix) {
  char renamed[64];
  snprintf(renamed, sizeof(renamed), "%s%s", path, suffix);
  return SD.rename(path, renamed);
}

// Removes already-uploaded *.csv.done files. The server already accepted them
// (rows verified), so the card copy is redundant. Stops after maxRemove files
// to bound the time spent holding the shared SPI bus. Returns the number removed.
size_t removeDoneFilesLocked(size_t maxRemove) {
  if (maxRemove == 0) return 0;
  size_t removed = 0;
  bool again = true;
  while (again && removed < maxRemove) {
    again = false;
    char name[64] = "";
    File dir = SD.open("/");
    if (dir) {
      for (;;) {
        File entry = dir.openNextFile();
        if (!entry) break;
        if (!entry.isDirectory() && isDoneFile(entry.name())) {
          snprintf(name, sizeof(name), "/%s", baseName(entry.name()));
          entry.close();
          again = true;
          break;
        }
        entry.close();
      }
      dir.close();
    }
    if (!again) break;
    if (!SD.remove(name)) {
      // Leave it for a later pass rather than spinning on it.
      Serial.printf("[SD] could not remove uploaded %s\n", name);
      break;
    }
    ++removed;
  }
  return removed;
}

}  // namespace

bool sdlogBusLock(uint32_t timeoutMs) {
  if (!s_mutex || s_busBusy.load()) return false;
  if (xSemaphoreTake(s_mutex, pdMS_TO_TICKS(timeoutMs)) != pdTRUE) return false;
  if (s_busBusy.load()) {
    xSemaphoreGive(s_mutex);
    return false;
  }
  return true;
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
  bool ok = false;
  if (mountSdLocked()) {
    size_t removed = removeDoneFilesLocked(8);
    if (removed > 0) {
      Serial.printf("[SD] pruned %u uploaded (.done) file(s) on mount\n",
                    static_cast<unsigned>(removed));
    }
    scanLocked();
    releaseSdLocked();
    ok = true;
  }
  giveMutex();
  Serial.printf("[SD] init %s, pending=%u parked=%u\n", ok ? "ok" : "failed",
                static_cast<unsigned>(s_pending.load()),
                static_cast<unsigned>(s_problems.load()));
  return ok;
}

void sdlogRefreshPending() {
  if (!s_mutex) return;
  if (s_active.load() || s_uploading.load()) return;
  if (!sdlogBusLock(200)) return;
  if (!s_active.load() && !s_uploading.load()) {
    if (!s_mounted && !mountSdLocked()) {
      sdlogBusUnlock();
      return;
    }
    scanLocked();
    releaseSdLocked();
  }
  sdlogBusUnlock();
}

bool sdlogStart() {
  if (s_uploading.load()) return false;
  if (!sdlogBusLock(200)) return false;
  if (s_active.load()) {
    giveMutex();
    return true;
  }

  time_t now = bestEpoch();
  char path[48] = "";
  if (!makeRidePath(path, sizeof(path), now)) {
    giveMutex();
    Serial.println("[SD] start refused: no UTC clock (NTP/GPS)");
    return false;
  }
  if (!s_mounted && !mountSdLocked()) {
    giveMutex();
    Serial.println("[SD] start refused: card mount failed");
    return false;
  }

  // Avoid truncating a ride if start is tapped twice within the same second.
  for (int bump = 1; bump < 6 && path[0] != '\0' && SD.exists(path); ++bump) {
    if (!makeRidePath(path, sizeof(path), now + bump)) path[0] = '\0';
  }
  if (path[0] == '\0' || SD.exists(path)) {
    releaseSdLocked();
    giveMutex();
    Serial.println("[SD] start refused: could not allocate a unique filename");
    return false;
  }

  snprintf(s_path, sizeof(s_path), "%s", path);
  s_fileCreated = false;
  s_bufLen = 0;
  s_bufSamples = 0;
  s_dropped = 0;
  s_active.store(true);
  scanLocked();               // excludes the still-empty current file
  busToTouchLocked();         // card stays mounted until stop
  giveMutex();

  Serial.printf("[SD] recording -> %s\n", s_path);
  return true;
}

bool sdlogStop() {
  if (!s_active.load()) return true;
  if (!sdlogBusLock(250)) {
    Serial.println("[SD] stop failed: SD bus busy (timed out); still recording");
    netLogEvent("WARN", "sd", "Stop REC timed out on the SD bus; still recording");
    return false;
  }
  if (!s_active.load()) {
    giveMutex();
    return true;
  }

  bool ok = flushLocked();
  if (!ok && s_bufLen > 0) {
    Serial.println("[SD] final flush failed; buffered rows were lost");
    s_dropped += s_bufSamples;
  }
  uint32_t dropped = s_dropped;
  s_active.store(false);

  if (s_mounted) {
    // Do not leave a header-only/partial file behind (e.g. start/stop before
    // the first sample, or a failed first flush).
    if (s_path[0] != '\0') {
      busToSdLocked();
      File f = SD.open(s_path, FILE_READ);
      size_t size = f ? f.size() : 0;
      if (f) f.close();
      if (size <= strlen(CSV_HEADER) + 2) {
        SD.remove(s_path);
      }
    }
    scanLocked();
    releaseSdLocked();
  }

  Serial.printf("[SD] stopped %s (dropped=%lu)\n", s_path,
                static_cast<unsigned long>(s_dropped));
  if (dropped > 0) {
    netLogEvent("WARN", "sd", "Stop REC: rows were dropped (card write failed)");
  }
  s_path[0] = '\0';
  s_fileCreated = false;
  s_bufLen = 0;
  s_bufSamples = 0;
  giveMutex();
  return ok && dropped == 0;
}

bool sdlogIsActive() {
  return s_active.load();
}

size_t sdlogPendingCount() {
  return s_pending.load();
}

size_t sdlogProblemCount() {
  return s_problems.load();
}

void sdlogWriteSample(const TelemetrySample& sample) {
  if (!s_active.load()) return;

  char line[SDLOG_LINE_MAX];
  if (!formatRow(sample, line, sizeof(line))) {
    return;  // no valid UTC timestamp -> server would reject the row
  }
  size_t lineLen = strlen(line);

  if (!takeMutex()) return;
  if (!s_active.load()) {
    giveMutex();
    return;
  }

  if (s_bufLen + lineLen >= sizeof(s_buf)) {
    if (!flushLocked()) {
      s_dropped += s_bufSamples;
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
      s_dropped += s_bufSamples;
      s_bufLen = 0;
      s_bufSamples = 0;
    }
  }
  giveMutex();
}

bool sdlogUploadPending() {
  if (!s_mutex || s_active.load()) return false;
  if (s_uploading.exchange(true)) return false;
  UploadGuard guard;

  DeviceCredentials cred = settingsGetCredentials();
  if (WiFi.status() != WL_CONNECTED || cred.device_id.length() == 0 || cred.device_key.length() == 0) {
    return false;
  }

  if (!takeMutex()) return false;
  if (s_active.load()) {
    giveMutex();
    return false;
  }
  if (!s_mounted && !mountSdLocked()) {
    giveMutex();
    return false;
  }

  char path[48] = "";
  char filename[48] = "";
  File file;
  File dir = SD.open("/");
  if (dir) {
    for (;;) {
      File entry = dir.openNextFile();
      if (!entry) break;
      if (!entry.isDirectory() && isRideCsv(entry.name())) {
        snprintf(path, sizeof(path), "/%s", baseName(entry.name()));
        snprintf(filename, sizeof(filename), "%s", baseName(entry.name()));
        entry.close();
        file = SD.open(path, FILE_READ);
        break;
      }
      entry.close();
    }
    dir.close();
  }

  if (path[0] == '\0' || !file) {
    if (file) file.close();
    scanLocked();
    releaseSdLocked();
    giveMutex();
    return false;
  }

  size_t size = file.size();
  size_t dataRows = 0;
  if (!countDataRows(file, dataRows)) {
    file.close();
    scanLocked();
    releaseSdLocked();
    giveMutex();
    Serial.printf("[SD] could not read %s; leaving it queued\n", path);
    return false;
  }
  // The server answers 413 above SDLOG_MAX_UPLOAD_ROWS rows and the firmware
  // treats every 413 as permanent, so stop such rides here (as .csv.big) rather
  // than letting the upload strand them in .csv.bad.
  const char* reason = nullptr;
  if (size == 0 || size > SDLOG_MAX_UPLOAD_BYTES) {
    reason = "empty or larger than the upload byte limit";
  } else if (dataRows == 0) {
    reason = "header-only";
  } else if (dataRows > SDLOG_MAX_UPLOAD_ROWS) {
    reason = "more than the upload row limit";
  }
  if (reason) {
    file.close();
    bool renamed = renameFileLocked(path, ".big");
    scanLocked();
    releaseSdLocked();
    giveMutex();
    Serial.printf("[SD] parked %s (%s: size=%u rows=%u; %s)\n", path, reason,
                  static_cast<unsigned>(size), static_cast<unsigned>(dataRows),
                  renamed ? "renamed .big" : "rename failed");
    if (renamed) netLogEvent("WARN", "sd", "Parked ride CSV that cannot be uploaded");
    return false;
  }

  WiFiClient client;
  HTTPClient http;
  String url = String("http://") + SERVER_HOST + ":" + String(HTTP_PORT) +
               "/api/devices/" + cred.device_id + "/rides";
  http.setTimeout(15000);
  http.setReuse(false);
  if (!http.begin(client, url)) {
    file.close();
    scanLocked();
    releaseSdLocked();
    giveMutex();
    return false;
  }
  http.addHeader("Content-Type", "text/csv");
  http.addHeader("X-Device-Id", cred.device_id);
  http.addHeader("X-Device-Key", cred.device_key);
  http.addHeader("X-Filename", filename);

  // Stream from the file without buffering it in RAM. Release the state mutex
  // during the network transfer (touch sees s_busBusy and skips SPI), then take
  // it back for the rename/scan.
  busToSdLocked();
  s_busBusy.store(true);
  giveMutex();
  int code = http.sendRequest("POST", &file, size);
  String resp = (code > 0) ? http.getString() : "";
  http.end();
  file.close();
  if (!takeMutex()) {
    s_busBusy.store(false);
    return false;
  }
  s_busBusy.store(false);

  bool uploadOk = false;
  bool terminal = false;
  if (code >= 200 && code < 300) {
    JsonDocument doc;
    DeserializationError err = deserializeJson(doc, resp);
    long samples = doc["samples"] | -1L;
    long inserted = doc["inserted"] | -1L;
    long rejected = doc["rejected"] | -1L;
    bool duplicate = doc["duplicate"] | false;
    bool serverOk = doc["ok"] | false;
    bool consistent = !err && serverOk && samples >= 0 && inserted >= 0 && rejected >= 0 &&
                      static_cast<size_t>(samples + rejected) == dataRows &&
                      (duplicate ? inserted == 0 : inserted == samples);
    if (consistent) {
      uploadOk = true;
    } else {
      Serial.printf("[SD] ride response inconsistent (%s, rows=%u): %s\n",
                    err.c_str(), static_cast<unsigned>(dataRows), resp.c_str());
      netLogEvent("WARN", "sd", "Ride upload response did not match CSV; will retry");
    }
  } else {
    // 401/403 are retryable: credentials are re-read from settings on every
    // attempt, so re-provisioning the device recovers the queued ride.
    terminal = code >= 400 && code < 500 && code != 401 && code != 403 &&
               code != 408 && code != 429;
    Serial.printf("[SD] ride upload %s failed -> %d (%s)\n", filename, code,
                  HTTPClient::errorToString(code).c_str());
  }

  if (uploadOk) {
    // Drop the file after the verified upload: the server is the durable copy and
    // keeping it would eventually fill the card. If removal fails, rename it to
    // .done so the next scan/prune pass removes it instead of re-uploading it.
    if (!SD.remove(path) && renameFileLocked(path, ".done")) {
      Serial.printf("[SD] uploaded %s but could not remove it; queued .done for pruning\n", path);
    }
  } else if (terminal) {
    if (renameFileLocked(path, ".bad")) {
      netLogEvent("WARN", "sd", "Ride CSV rejected permanently; parked as .bad");
    }
  }

  size_t pruned = removeDoneFilesLocked(4);
  scanLocked();
  releaseSdLocked();
  giveMutex();

  if (uploadOk) {
    Serial.printf("[SD] uploaded %s -> %d %s (removed from card, pruned=%u)\n", filename, code,
                  resp.c_str(), static_cast<unsigned>(pruned));
    netLogEvent("INFO", "sd", "Ride CSV uploaded to dashboard");
  }
  return uploadOk;
}
