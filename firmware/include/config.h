#pragma once

#if defined(__has_include)
  #if __has_include("secrets.h")
    #include "secrets.h"
  #else
    #error "Missing include/secrets.h! Copy include/secrets.example.h to include/secrets.h and fill in your WiFi credentials and DEVICE_KEY."
  #endif
#else
  #include "secrets.h"
#endif

// Firmware metadata
#define FW_VERSION "1.0.0"

// Display / Backlight (TFT SPI pins configured via platformio.ini build_flags)
#define PIN_TFT_BL 21
#define SCREEN_W 320
#define SCREEN_H 240

// XPT2046 Touch (separate VSPI bus)
#define PIN_TOUCH_CLK 25
#define PIN_TOUCH_MOSI 32
#define PIN_TOUCH_MISO 39
#define PIN_TOUCH_CS 33
#define PIN_TOUCH_IRQ 36

// Default raw touch calibration bounds (0..4095)
#define TOUCH_RAW_MIN_X 240
#define TOUCH_RAW_MAX_X 3800
#define TOUCH_RAW_MIN_Y 250
#define TOUCH_RAW_MAX_Y 3800
#define TOUCH_DEBOUNCE_MS 250

// Onboard RGB LED (active LOW)
#define PIN_LED_R 4
#define PIN_LED_G 16
#define PIN_LED_B 17

// LDR light sensor (ADC1, higher raw value = darker)
#define PIN_LDR 34
#define LDR_BRIGHT_RAW 250
#define LDR_DARK_RAW 3200

// I2C environmental sensors (CN1 connector)
#define PIN_I2C_SDA 27
#define PIN_I2C_SCL 22
#define I2C_FREQ_HZ 100000

// GPS UART2 (P3 connector, IO35 is input-only)
#define PIN_GPS_RX 35
#define PIN_GPS_TX -1

// microSD / TF slot (SPI). Note: the TF slot and XPT2046 touch share the VSPI
// peripheral, so sdlog.cpp switches the bus pins around SD operations.
#define PIN_SD_CS 5
#define PIN_SD_MOSI 23
#define PIN_SD_MISO 19
#define PIN_SD_SCK 18
#define SD_SPI_FREQ_HZ 4000000
#define SDLOG_FLUSH_SAMPLES 10
#define SDLOG_UPLOAD_INTERVAL_MS 60000UL
#define SDLOG_MAX_UPLOAD_BYTES (2 * 1024 * 1024)

// Telemetry & buffering
#define TELEMETRY_BUFFER_CAPACITY 180
#define HTTP_BATCH_LIMIT 20
#define MQTT_BUFFER_SIZE 1536
#define MQTT_KEEPALIVE_S 30

// Sampling, UI & power intervals
#define SENSOR_POLL_MS 2000UL
#define SPARKLINE_INTERVAL_MS 5000UL
#define TEMP_SPARKLINE_LEN 60
#define UI_REFRESH_MS 333UL
#define LDR_POLL_MS 250UL
#define BME680_BURNIN_MS 300000UL

// SoftAP provisioning & dashboard pairing
#define AP_SETUP_SSID "WSB-CYD-Setup"
#define AP_SETUP_PASS "merito2026"
#define DASHBOARD_BASE_URL "https://wsbmerito.telemetry.164-92-227-240.sslip.io"
#define BOOTSTRAP_REFRESH_MS 600000UL
