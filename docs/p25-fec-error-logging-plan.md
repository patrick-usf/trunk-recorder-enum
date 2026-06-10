# P25 FEC Error Logging — Implementation Plan

## Background

P25 Phase 1 FDMA uses several Forward Error Correction algorithms across different frame types. Every one of these algorithms already computes an error count internally at the gr-op25 layer — the data exists and is simply discarded. This plan instruments the existing call sites, routes the stats through the GNURadio message queue, and writes them to a structured TSV for offline RF quality analysis.

---

## FEC Algorithm Inventory

| Algorithm | Function | Location | Error info available | Current fate |
|-----------|----------|----------|---------------------|--------------|
| **BCH(63,39)** | `bchDec()` | `p25p1_fdma.cc` via framer | `int` 0–11 corrected, -1 uncorrectable | Stored in `framer->bch_errors`, added to `rx_status.error_count` — tracked but not externally logged |
| **Golay(24,12,8)** HDU | `gly24128Dec()` | `p25p1_fdma.cc` `process_HDU` | `size_t* errs` pointer | Accumulated in local `gly_errs`, printed to stderr at debug level 10 only |
| **Golay(24,12,8)** TDU15 | `gly24128Dec()` | `p25p1_fdma.cc` `process_TDU15` | `size_t* errs` pointer | **Bug: captured in `errs` but never accumulated into `gly_errs` — always logs 0** |
| **Hamming(10,6,3)** | `hmg1063Dec()` | `p25p1_fdma.cc` `process_LLDU` | **None** — return value is corrected data only | **No error count exposed at all** |
| **RS(63,47)** `rs16` | `rs16.decode()` | `p25p1_fdma.cc` `process_HDU` | `int` 0–16, -1 uncorrectable | Captured in `ec`, stderr at debug 10 |
| **RS(63,55)** `rs8` | `rs8.decode()` | `p25p1_fdma.cc` `process_LDU2` | `int` 0–8, -1 uncorrectable | Captured in `ec`, conditionally logged to stderr |
| **RS(63,51)** `rs12` | `rs12.decode()` | `p25p1_fdma.cc` `process_LCW` | `int` 0–12, -1 uncorrectable | Captured in `ec`, **validity-checked but never logged anywhere** |
| **RS(63,35)** `rs28` | `rs28.decode()` | `p25p2_tdma.cc` | `int` 0–28, -1 uncorrectable | Captured in `rs_errs`, passed to mac handlers, conditionally stderr |
| **IMBE** (voice) | (codec internal) | `p25p1_fdma.cc` | `size_t` 0–15, fully tracked | Already in `rx_status.error_count` with spike detection — best-instrumented |
| **Trellis** (DMR only) | `CDMRTrellis::decode()` | `dmr_slot.cc` | `bool` pass/fail only | Not applicable to P25 primary path; no magnitude in decoder |

**Summary:** BCH, Golay, and all RS variants already compute error counts. Hamming needs a one-line modification to expose its count. Trellis is DMR-only and provides only pass/fail.

### Bugs Surfaced by This Analysis

- **TDU15 Golay accumulation bug** — `gly_errs` is not incremented in `process_TDU15` even though `errs` is received. The loop captures the value but the accumulator line is missing. Fixing this is a prerequisite for correct TDU15 stats.
- **RS12 (LCW) never reported** — the only RS variant where the error count is validity-checked but never surfaced to the operator in any form.

---

## Architecture

### Why Not Per-Frame Messages

The FEC algorithms fire at 20–200 Hz on the control channel. Sending a GNURadio message per FEC invocation would flood the queue. The right model is:

**Accumulate counters in-place → flush periodically → one message per interval**

Each `p25p1_fdma` instance maintains a `FecPeriodStats` struct. FEC call sites update integer counters in-place (no allocation, no lock — same thread). A timer fires every N seconds (default 10), serializes the struct, sends it as message type `M_P25_FEC_STATS = 21`, then resets the accumulators.

At the `parse_message()` layer the new type 21 handler deserializes the struct and passes it to `P25FecLogger`, which appends a TSV record. One row per 10 seconds per system.

---

## New Files

### `lib/op25_repeater/lib/p25_fec_stats.h`

Plain C struct shared between the gr-op25 layer (producer) and the parser layer (consumer). No STL, no exceptions — safe to memcpy through the message queue payload.

```cpp
struct FecAlgStats {
    uint32_t attempts;        // times this algorithm was invoked
    uint32_t with_errors;     // invocations where corrected > 0
    uint32_t total_corrected; // sum of all corrected symbol/bit counts
    uint8_t  max_single;      // worst single-frame corrected count
    uint32_t uncorrectable;   // decode() returned -1
};

struct FecPeriodStats {
    uint32_t    interval_sec;  // length of this reporting period
    uint32_t    nac;
    FecAlgStats bch;           // BCH(63) NID decode
    FecAlgStats golay_hdu;     // Golay(24,12,8) HDU codewords
    FecAlgStats golay_tdu;     // Golay(24,12,8) TDU15 codewords
    FecAlgStats hamming;       // Hamming(10,6,3) LLDU per-codeword
    FecAlgStats rs16;          // RS(63,47) HDU
    FecAlgStats rs8;           // RS(63,55) LDU2 ESS
    FecAlgStats rs12;          // RS(63,51) LCW
    FecAlgStats rs28;          // RS(63,35) Phase 2 MAC PDU
};
```

### `trunk-recorder/systems/p25_fec_logger.h/.cc`

Singleton logger matching the pattern of `P25FrameLogger`. Receives a `FecPeriodStats` on each type 21 message, appends one TSV row, rolls file at configurable size (default 50 MB). Opened by `monitor_systems.cc` via a separate config key `fecStatsLog`.

---

## Files to Modify

### 1. `lib/op25_repeater/lib/op25_msg_types.h`

```cpp
static const int16_t M_P25_FEC_STATS = 21; // periodic FEC error accumulation
```

### 2. `lib/op25_repeater/lib/op25_hamming.h`

`hmg1063Dec` currently returns only the corrected 6-bit hexbit value. Add an overload that also writes 1 to `*err` if a correction was applied:

```cpp
// existing — returns corrected data, no error flag
static inline int hmg1063Dec(uint32_t Dat, uint32_t Par);

// new — writes 1 to *err if syndrome was nonzero (a correction was made)
static inline int hmg1063DecE(uint32_t Dat, uint32_t Par, int *err) {
    // compute syndrome; if nonzero, a correction occurred
    uint32_t syn = /* existing syndrome computation */;
    *err = (syn != 0) ? 1 : 0;
    return hmg1063DecTbl[syn];  // same logic as existing decode
}
```

### 3. `lib/op25_repeater/lib/p25p1_fdma.h`

```cpp
#include "p25_fec_stats.h"

// add to class private section:
FecPeriodStats  d_fec_stats;
int64_t         d_fec_last_send_us;
int             d_fec_interval_sec;   // default 10

void fec_stats_reset();
void fec_stats_send();
```

### 4. `lib/op25_repeater/lib/p25p1_fdma.cc`

**Constructor:** call `fec_stats_reset()`, set `d_fec_interval_sec = 10`.

**`process_HDU` — Golay:**
```cpp
// existing accumulation (unchanged):
gly_errs += errs;
// add:
d_fec_stats.golay_hdu.attempts++;
if (errs > 0) {
    d_fec_stats.golay_hdu.with_errors++;
    d_fec_stats.golay_hdu.total_corrected += errs;
    if (errs > d_fec_stats.golay_hdu.max_single)
        d_fec_stats.golay_hdu.max_single = (uint8_t)errs;
}
```

**`process_HDU` — RS16:**
```cpp
ec = rs16.decode(HB);
d_fec_stats.rs16.attempts++;
if (ec < 0) {
    d_fec_stats.rs16.uncorrectable++;
} else if (ec > 0) {
    d_fec_stats.rs16.with_errors++;
    d_fec_stats.rs16.total_corrected += ec;
    if (ec > d_fec_stats.rs16.max_single)
        d_fec_stats.rs16.max_single = (uint8_t)ec;
}
```

**`process_TDU15` — Golay (bug fix + instrumentation):**
```cpp
uint32_t D = gly24128Dec(CW, &errs);
gly_errs += errs;      // ← this line was MISSING (bug fix)
d_fec_stats.golay_tdu.attempts++;
if (errs > 0) {
    d_fec_stats.golay_tdu.with_errors++;
    d_fec_stats.golay_tdu.total_corrected += errs;
    if (errs > d_fec_stats.golay_tdu.max_single)
        d_fec_stats.golay_tdu.max_single = (uint8_t)errs;
}
```

**`process_LLDU` — Hamming (24 codewords per LDU):**
```cpp
// replace: HB[39 + i] = hmg1063Dec(CW >> 4, CW & 0x0f);
// with:
int hmg_err = 0;
HB[39 + i] = hmg1063DecE(CW >> 4, CW & 0x0f, &hmg_err);
d_fec_stats.hamming.attempts++;
if (hmg_err) {
    d_fec_stats.hamming.with_errors++;
    d_fec_stats.hamming.total_corrected++;
    // max_single stays 1 for Hamming — it corrects at most 1 bit per codeword
}
```

**`process_LDU2` — RS8:**
```cpp
ec = rs8.decode(HB);
d_fec_stats.rs8.attempts++;
if (ec < 0) {
    d_fec_stats.rs8.uncorrectable++;
} else if (ec > 0) {
    d_fec_stats.rs8.with_errors++;
    d_fec_stats.rs8.total_corrected += ec;
    if (ec > d_fec_stats.rs8.max_single)
        d_fec_stats.rs8.max_single = (uint8_t)ec;
}
```

**`process_LCW` — RS12 (currently not logged anywhere):**
```cpp
int ec = rs12.decode(HB);
d_fec_stats.rs12.attempts++;
if (ec < 0) {
    d_fec_stats.rs12.uncorrectable++;
} else if (ec > 0) {
    d_fec_stats.rs12.with_errors++;
    d_fec_stats.rs12.total_corrected += ec;
    if (ec > d_fec_stats.rs12.max_single)
        d_fec_stats.rs12.max_single = (uint8_t)ec;
}
```

**BCH — copy from framer after NID decode in `process_frame()`:**

BCH is already tracked in `framer->bch_errors`. After the `process_NID()` / framer decode block, copy the value:
```cpp
if (framer->bch_errors >= 0) {
    d_fec_stats.bch.attempts++;
    if (framer->bch_errors > 0) {
        d_fec_stats.bch.with_errors++;
        d_fec_stats.bch.total_corrected += framer->bch_errors;
        if (framer->bch_errors > d_fec_stats.bch.max_single)
            d_fec_stats.bch.max_single = (uint8_t)framer->bch_errors;
    }
} else {
    d_fec_stats.bch.uncorrectable++;
}
```

**Periodic send — at end of `process_frame()`:**
```cpp
int64_t now_us = /* use existing logts or gettimeofday */;
if (now_us - d_fec_last_send_us >= (int64_t)d_fec_interval_sec * 1000000LL) {
    fec_stats_send();
}
```

**`fec_stats_send()`:**
```cpp
void p25p1_fdma::fec_stats_send() {
    d_fec_stats.nac = framer->nac;
    d_fec_stats.interval_sec = d_fec_interval_sec;
    std::string payload(sizeof(FecPeriodStats), '\0');
    memcpy(&payload[0], &d_fec_stats, sizeof(FecPeriodStats));
    send_msg(payload, M_P25_FEC_STATS);
    fec_stats_reset();
    d_fec_last_send_us = now_us;
}
```

### 5. `lib/op25_repeater/lib/p25p2_tdma.h/.cc`

Same pattern. Add `FecPeriodStats d_fec_stats` member. Instrument the `rs28.decode(HB, Erasures)` call site in `handle_acch_frame()`. Also instrument any Hamming or Golay calls present in Phase 2 processing. Periodic send tied to the `decode_mac_msg` boundary.

```cpp
rs_errs = rs28.decode(HB, Erasures);
d_fec_stats.rs28.attempts++;
if (rs_errs < 0) {
    d_fec_stats.rs28.uncorrectable++;
} else if (rs_errs > 0) {
    d_fec_stats.rs28.with_errors++;
    d_fec_stats.rs28.total_corrected += rs_errs;
    if (rs_errs > d_fec_stats.rs28.max_single)
        d_fec_stats.rs28.max_single = (uint8_t)rs_errs;
}
```

### 6. `trunk-recorder/systems/p25_parser.cc`

In `parse_message()`, after the type 20 handler:

```cpp
} else if (type == 21) { // FEC stats period
    if (s.length() >= sizeof(FecPeriodStats)) {
        FecPeriodStats stats;
        memcpy(&stats, s.data(), sizeof(FecPeriodStats));
        P25FecLogger::instance().log_period(stats, system);
    }
    return messages;  // no TrunkMessage produced
}
```

### 7. `trunk-recorder/systems/p25_parser.h`

```cpp
#include "p25_fec_logger.h"
// (p25_fec_stats.h is included transitively via p25_fec_logger.h)
```

### 8. `trunk-recorder/global_structs.h`

```cpp
std::string fec_stats_log;  // path for FEC error TSV log; empty = disabled
```

### 9. `trunk-recorder/config.cc`

```cpp
config.fec_stats_log = data.value("fecStatsLog", "");
if (!config.fec_stats_log.empty()) {
    BOOST_LOG_TRIVIAL(info) << "FEC Stats Log: " << config.fec_stats_log;
}
```

### 10. `trunk-recorder/monitor_systems.cc`

```cpp
if (!config.fec_stats_log.empty()) {
    P25FecLogger::instance().open(config.fec_stats_log);
    BOOST_LOG_TRIVIAL(info) << "P25 FEC stats logger active: " << config.fec_stats_log;
}
```

### 11. `trunk-recorder/CMakeLists.txt`

```cmake
trunk-recorder/systems/p25_fec_logger.cc
```

---

## TSV Output Format

One row per reporting interval per system. 44 columns grouped by algorithm. At 10-second intervals on the Howard County control channel: ~one row per 10 seconds, ~220 bytes per row, ~750 KB/day — trivial storage.

```
timestamp_start  timestamp_end  sys_name  nac  interval_sec

bch_attempts  bch_with_errors  bch_total_corrected  bch_max_single  bch_uncorrectable

golay_hdu_attempts  golay_hdu_with_errors  golay_hdu_total_corrected  golay_hdu_max  golay_hdu_uncorrectable
golay_tdu_attempts  golay_tdu_with_errors  golay_tdu_total_corrected  golay_tdu_max  golay_tdu_uncorrectable

hamming_attempts  hamming_with_errors  hamming_total_corrected  hamming_max  hamming_uncorrectable

rs8_attempts   rs8_with_errors   rs8_total_corrected   rs8_max   rs8_uncorrectable
rs12_attempts  rs12_with_errors  rs12_total_corrected  rs12_max  rs12_uncorrectable
rs16_attempts  rs16_with_errors  rs16_total_corrected  rs16_max  rs16_uncorrectable
rs28_attempts  rs28_with_errors  rs28_total_corrected  rs28_max  rs28_uncorrectable
```

### Column Semantics

| Column suffix | Description |
|--------------|-------------|
| `_attempts` | Times the algorithm was invoked during the interval |
| `_with_errors` | Invocations where the corrected symbol count was > 0 |
| `_total_corrected` | Sum of all corrected symbol/bit counts across the interval |
| `_max` | Worst single-frame corrected count seen in the interval |
| `_uncorrectable` | Invocations where decode returned -1 (beyond correction capacity) |

---

## Configuration

```json
{
    "controlFrameLog": "/home/sdr/P25/trunk-b210/p25_frames.tsv",
    "fecStatsLog":     "/home/sdr/P25/trunk-b210/p25_fec_stats.tsv"
}
```

A `fecStatsInterval` config key can optionally control the reporting period (default 10 seconds). If `fecStatsLog` is absent or empty, the logger does nothing and type 21 messages are silently discarded.

---

## Implementation Order

1. `p25_fec_stats.h` — standalone struct, no deps, can be written first
2. `op25_hamming.h` — add `hmg1063DecE` overload alongside existing function
3. `p25p1_fdma.h/.cc` — add member + instrument all Phase 1 FEC call sites + periodic send
4. `p25p2_tdma.h/.cc` — instrument Phase 2 (primarily RS28)
5. `op25_msg_types.h` — add type 21 constant
6. `p25_fec_logger.h/.cc` — new logger (can be modeled directly on `p25_frame_logger`)
7. `p25_parser.cc/.h` — type 21 handler, include new header
8. Config + wiring: `global_structs.h`, `config.cc`, `monitor_systems.cc`
9. `CMakeLists.txt` — add new source file
10. Build, restart trunk-recorder, verify TSV output appears

---

## Bugs Fixed as Side Effects

These are pre-existing bugs in the gr-op25 code that become visible when adding instrumentation:

- **TDU15 Golay accumulation** — `gly_errs` not incremented in `process_TDU15`. The `errs` pointer is passed to `gly24128Dec` and the return value is captured, but the line `gly_errs += errs` is absent. The stats accumulation adds this missing line as a prerequisite.
- **RS12 (LCW) never reported** — the only RS variant with an error count that is captured and validity-checked but has no log output whatsoever. First surfaced by this audit.

---

## What Trellis Provides

`CDMRTrellis::decode()` in `trellis.cc` returns only `bool`. Full error magnitude would require exposing the Viterbi path metric Hamming distance — a more invasive change. Since Trellis is DMR-only (not on the P25 control channel), this is out of scope. If DMR monitoring is added later, the decoder can be extended at that time.

---

## Out of Scope (Deferred)

- Per-frame FEC event log (one row per frame rather than aggregate) — too much volume for 24/7 capture without filtering
- IMBE voice codec error logging — already fully tracked in `rx_status`; cross-referencing with FEC can be a later analysis pass
- Trellis error magnitude — requires Viterbi path metric exposure, DMR only
- Log compression — deferred in the same way as for `p25_frames.tsv`
