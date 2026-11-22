#pragma once

#include <Arduino.h>
#include "telemetry.h"

// Initializes the SD bus/card and scans for pending ride CSVs. Safe to call when
// no card is inserted (returns false).
bool sdlogInit();

// Starts a new ride_YYYYMMDD_HHMMSS.csv file. Requires a valid UTC clock.
bool sdlogStart();

// Flushes buffered rows and closes the ride file. Returns true when recording
// stopped and the final flush succeeded with no dropped rows. Returns false when
// the recorder still runs or the ride is incomplete; callers should warn.
bool sdlogStop();

bool sdlogIsActive();

// Appends one CSV row to the active ride (no-op when not recording).
void sdlogWriteSample(const TelemetrySample& sample);

// Number of completed, not-yet-uploaded ride_*.csv files on the card (cached).
size_t sdlogPendingCount();

// Number of parked *.csv.big / *.csv.bad files (empty, oversized, too many rows
// or rejected).
size_t sdlogProblemCount();

// Re-scans the card for pending/parked rides (boot, or periodically from net).
void sdlogRefreshPending();

// POSTs one pending CSV to /api/devices/{id}/rides. On success the firmware
// removes the card copy (the server holds the durable copy). Permanent 4xx
// responses, except 401/403/408/429, become *.csv.bad; rides above the byte or
// row limits become *.csv.big. Returns true when a ride was uploaded.
bool sdlogUploadPending();

// The TF slot and touch panel share VSPI. UI code brackets raw touch reads with
// these helpers so a concurrent SD transfer cannot change the bus pins mid-read.
bool sdlogBusLock(uint32_t timeoutMs = 50);
void sdlogBusUnlock();
