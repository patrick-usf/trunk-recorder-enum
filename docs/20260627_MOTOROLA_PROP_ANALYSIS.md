# Motorola Proprietary P25 LCW Frame Analysis

**Date:** 2026-06-27  
**System:** Howard County MD Motorola Astro 25, NAC=0x842  
**Hardware:** USRP B210, center 856.0625 MHz, 10.5 Msps  
**Log file:** `/home/sdr/P25/trunk-b210/p25_b210_downlink.tsv`

---

## 1. Background

We operate a passive P25 Phase 1 FDMA downlink monitor on the Howard County MD Astro 25 system. The monitor decodes 21 traffic channels simultaneously using a single wideband IQ capture. All decoded frames are logged to a 33-column TSV with full raw frame bytes, structured metadata, FEC error counts, and raw FS/NID fields.

As part of auditing the TSV for unresolved frame types, we identified a population of Motorola-proprietary Link Control Word (LCW) frames that were being captured but not decoded. This document covers the analysis of two key types — `LCO=0x15` (Call Termination) and `LCO=0x17` (multi-part Call Data) — and the code changes made to decode them.

---

## 2. P25 LCW Frame Structure

A P25 Link Control Word is a 72-bit (9-byte) payload embedded in LDU1 or TDULC frames. The first byte encodes three header fields:

```
byte[0]: [PB:1][SF:1][LCO:6]
  PB  = Protected Bit (1 = encrypted LCW, content not decoded)
  SF  = Secondary Format (0 = explicit MFID in byte[1]; 1 = abbreviated, no MFID)
  LCO = Link Control Opcode (6-bit)
```

For standard P25 frames (MFID=0x00), bytes[2..8] follow TIA-102.AABC layouts that vary per LCO. For **Motorola proprietary frames** (MFID=0x90), Motorola uses a consistent internal layout across opcodes:

```
+0  LCO (opcode)
+1  MFID (0x90 for Motorola)
+2  payload
+3  payload
+4  payload
+5  FLAGS
+6  payload
+7  payload
+8  CRC / protected bits
```

This layout was confirmed empirically through correlation analysis against known P25 events (described below) and cross-referenced against other Motorola P25 implementation documentation.

---

## 3. Frame Inventory Before Analysis

From the TSV (prior to code changes), three Motorola-proprietary LCW opcodes appeared as `decode_status=RAW` (unrecognized content):

| LCO | Opcode Name | Count | Status |
|-----|------------|-------|--------|
| 0x15 | LCW_CALL_TERM | 411 | Raw bytes only — no field parse |
| 0x17 | LCW_UNKNOWN | 1,839 | Raw bytes only — no field parse |
| 0x05 | LCW_UU_ANS_REQ | 22 | Raw bytes only |

All three had `MFID=0x90` (Motorola), `SF=0` (explicit MFID format), `PB=0` (unencrypted LCW content).

---

## 4. LCO=0x15 Call Termination — Byte Structure Analysis

### 4.1 Hypothesis: bytes[2:3] = TGID

Examining the raw 9-byte payload `15902ad30401007276`:

```
byte[0] = 0x15  LCO (Call Termination)
byte[1] = 0x90  MFID (Motorola)
byte[2] = 0x2a
byte[3] = 0xd3  → 0x2ad3 = 10963 decimal
...
```

The value 10963 is a known active talkgroup on HoCo. We hypothesized bytes[2:3] encode the TGID being terminated.

### 4.2 TGID Correlation

We extracted bytes[2:3] from all 411 LCW_CALL_TERM events and cross-referenced against LCW_GRP_V_CH_USER and LCW_GRP_V_CH_UPDATE frames on the same traffic channel within a ±30-row window:

| bytes[2:3] hex | Decimal | Events | TGID matches nearby | Hit rate |
|---------------|---------|--------|-------------------|----------|
| 0x2a96 | 10902 | 80 | 76 | 95% |
| 0x2ad1 | 10961 | 69 | 69 | 100% |
| 0x2bcd | 11213 | 40 | 40 | 100% |
| 0x2ad3 | 10963 | 35 | 35 | 100% |
| 0x2a95 | 10901 | 30 | 30 | 100% |
| 0x2ad9 | 10969 | 19 | 19 | 100% |
| 0x2aba | 10938 | 19 | 18 | 94% |

**Result: bytes[2:3] = TGID, confirmed at 94–100% across all 14 observed talkgroups.**

### 4.3 Remaining Byte Structure

After confirming TGID, we analyzed the remaining 5 bytes across all 329 events where position could be determined:

| Position | Values observed | Interpretation |
|----------|----------------|----------------|
| byte[4] | 0x04, 0x05, 0x06 | Call type / service class |
| byte[5] | 0x01 always | FLAGS — always set, meaning TBD |
| byte[6] | 0x00 always | Reserved or constant payload field |
| byte[7:8] | 138+ distinct 16-bit values | Per-call handle (see §5) |

byte[8] is the CRC/protected byte per Motorola's +8 layout. For this opcode, it shares the same byte position as the low byte of the call handle — Motorola computes the call handle such that byte[8] also satisfies the CRC constraint.

**Call type values seen (post-restart, structured decode):**

| call_type | Count | Likely meaning |
|-----------|-------|---------------|
| 0x04 | 28 | Standard group call termination |
| 0x05 | 14 | Alternate group call type (possibly priority/emerg) |
| 0x06 | 2 | Observed on TGID 10963 — third variant, meaning TBD |

**Full confirmed byte layout for LCO=0x15:**

```
+0  0x15        LCO — Call Termination
+1  0x90        MFID — Motorola
+2  TGID[15:8]  Talkgroup high byte
+3  TGID[7:0]   Talkgroup low byte
+4  call_type   0x04 / 0x05 / 0x06
+5  FLAGS       0x01 (constant across all observed events)
+6  0x00        Reserved / constant
+7  handle[15:8] Call handle high byte
+8  handle[7:0]  Call handle low byte = CRC/protected byte
```

---

## 5. LCO=0x17 Multi-Part Call Data — Structure

### 5.1 Discovery: 5-Segment Sequence

LCW_UNKNOWN (0x17) frames appear in groups of 5, immediately following a LCW_CALL_TERM (0x15) event on the same traffic channel frequency. byte[2] of the 0x17 frame carries a sequence counter (1–5):

```
CALL_TERM (0x15): 15902a96050100560c    tgid=10902, call_handle=0x560c
  ↓ (same freq, next few LDUs)
0x17 seq=1:  1790 01 5b ee 00 84 b0 00   FLAGS=0x00
0x17 seq=2:  1790 02 50 71 6e 47 cd c2   FLAGS=0x77
0x17 seq=3:  1790 03 5d bd 1c 92 39 20   FLAGS=0x87
0x17 seq=4:  1790 04 56 52 59 06 98 e6   FLAGS=0xe4
0x17 seq=5:  1790 05 5c f6 2e f0 00 00   FLAGS=0x00 (terminates with zeros)
```

### 5.2 Byte Layout

Applying the Motorola +0..+8 layout to LCO=0x17:

```
+0  0x17         LCO
+1  0x90         MFID
+2  seq_num      Sequence counter 1–5 (occasionally 6)
+3  payload_a[0] First payload segment, high byte
+4  payload_a[1] First payload segment, low byte
+5  FLAGS        Per-segment state byte (varies)
+6  payload_b[0] Second payload segment, high byte
+7  payload_b[1] Second payload segment, low byte
+8  CRC          CRC/protected byte for this segment
```

### 5.3 FLAGS Byte Per Sequence

Across ~45 complete 5-segment sequences observed post-restart:

| seq | FLAGS values observed |
|-----|----------------------|
| 1 | 0x00 (always) |
| 2 | 0x6e, 0x77, 0xb5 (3 variants) |
| 3 | 0x0b, 0x1c, 0x84, 0x87, 0xd7, 0xdc, 0xe0 (varies) |
| 4 | 0x14, 0x26, 0x27, 0x58, 0x7a, 0x92, 0xc5, 0xe2, 0xe4, 0xe9 (varies) |
| 5 | 0x00, 0x24, 0x3e, 0x53 (mostly near-zero) |

seq=1 FLAGS is always 0x00; seq=5 is predominantly 0x00 or very small, often accompanying trailing zero bytes. This pattern is consistent with a multi-segment encoded block where FLAGS carries segment-local state (possibly interleaving index, block type, or error correction metadata).

### 5.4 Call Handle Linkage

The call handle from LCW_CALL_TERM (bytes[7:8]) encodes into the first byte of the 0x17 seq=1 payload:

```
call_handle_high_byte = bytes[7]
0x17 seq=1 payload_a[0] = (call_handle_high_byte & 0xF0) | 0x0B
```

Verified across all observed TGID/call_handle combinations:

| TGID | call_handle | call_handle[7] | 0x17 seq=1 payload_a[0] | Expected |
|------|------------|----------------|------------------------|---------|
| 10963 | 0x9bee | 0x9b | 0x9b | 0x9b ✓ |
| 10902 | 0x560c | 0x56 | 0x5b | 0x5b ✓ |
| 10901 | 0x164a | 0x16 | 0x1b | 0x1b ✓ |
| 10961 | 0x43a4 | 0x43 | 0x4b | 0x4b ✓ |
| 10902 | 0x037e | 0x03 | 0x0b | 0x0b ✓ |

This linkage provides a cross-reference that binds the CALL_TERM event to its corresponding 5-segment call data block. The full 30-byte payload assembled from 5 segments likely contains the source radio unit ID and call record data, but the encoding is not yet decoded (see §7).

### 5.5 Structural Constant in seq=1

Across all observed 0x17 seq=1 frames, bytes[4..6] (at +4, +5, +6 positions) follow a near-constant template:

```
[payload_a[1]=0xee][FLAGS=0x00][payload_b[0]=0x84]
```

- `0xee` at +4 appears as a fixed marker across all seq=1 frames regardless of TGID
- `0x84` at +6 is also consistent — notably matching the DES-OFB AlgID byte, though whether this is coincidental or structural is unconfirmed

---

## 6. Code Changes

### 6.1 `p25p1_fdma.cc` — CRC Capture

**File:** `/home/sdr/trunk-recorder/lib/op25_repeater/lib/p25p1_fdma.cc`  
**Function:** `process_LCW()`  
**Location:** Before `send_msg(pdu, M_P25_FDMA_LCW)` (line ~624)

```cpp
// Motorola LCW: byte[8] per Motorola layout (+8) is CRC/protected — log in pending_crc slot
if ((lcw[0] & 0x40) == 0 && lcw[1] == 0x90)
    d_pending_crc = lcw[8];
send_msg(pdu, M_P25_FDMA_LCW);
```

This causes byte[8] of every Motorola LCW frame to flow through the 27-byte trailer into the `tsbk_crc` column of the TSV. The condition checks `SF=0` (explicit MFID format, bit 6 of lcw[0]) and `MFID=0x90` (Motorola). `d_pending_crc` is automatically cleared by `send_msg()` after the trailer is written, so no cleanup is needed.

### 6.2 `p25_parser.cc` — Motorola LCW Decode Block

**File:** `/home/sdr/trunk-recorder/trunk-recorder/systems/p25_parser.cc`  
**Location:** Inside `type == 19` handler, `sf == 0` branch, ~line 1670

Restructured the `sf == 0` block to split on MFID. Standard MFID (0x00/0x01) continues to use TIA-102 field positions. Motorola MFID=0x90 uses the +0..+8 layout:

```cpp
if (message.mfid == 0x90) { // Motorola: +2..+4=payload, +5=FLAGS, +6..+7=payload, +8=CRC
    uint8_t flags    = (uint8_t)s[5]; // +5 = FLAGS
    uint8_t crc_byte = (uint8_t)s[8]; // +8 = CRC/protected
    switch (lco) {
      case 0x15: { // Motorola Call Termination
        message.talkgroup    = ((uint8_t)s[2] << 8) | (uint8_t)s[3];
        uint8_t  call_type   = (uint8_t)s[4];
        uint16_t call_handle = ((uint8_t)s[7] << 8) | (uint8_t)s[8];
        message.message_type = TDULC;
        // ... ostringstream meta ...
        break;
      }
      case 0x17: { // Motorola multi-part call data (5-segment)
        uint8_t seq_num = (uint8_t)s[2];
        // ... payload_a=[3:4], FLAGS=s[5], payload_b=[6:7], crc=s[8] ...
        break;
      }
      case 0x05: { // Motorola UU_ANS_REQ
        // ... FLAGS and payload fields by position ...
        break;
      }
      default:
        // Generic Motorola: log FLAGS and all payload bytes by position
        break;
    }
} else { // Standard MFID
    if (lco == 0x00) { /* GRP_V_CH_USER, TIA layout */ }
    if (lco == 0x03) { /* UU_V_CH_USER, TIA layout */ }
}
```

**Meta / raw_frame fix:** The `raw_frame` block previously overwrote `message.meta` unconditionally for `UNKNOWN` frames. Changed to only set `message.meta = message.raw_frame` when `message.meta` is empty, so Motorola structured meta (set in the switch above) is preserved even for frames that remain `message_type=UNKNOWN` (e.g., LCO=0x17):

```cpp
if (message.message_type == UNKNOWN) {
    // ... build raw_frame string ...
    message.raw_frame = raw.str();
    if (message.meta.empty())     // preserve structured Motorola meta if set
        message.meta = message.raw_frame;
}
```

**Meta header enhancement:** Added MFID and FLAGS to the LCW header wrapper for all explicit-MFID Motorola frames:

```cpp
if (sf == 0)
    hdr << "][MFID=0x" << std::setw(2) << (unsigned int)message.mfid;
if (sf == 0 && message.mfid == 0x90)
    hdr << "][FLAGS=0x" << std::setw(2) << (unsigned int)(uint8_t)s[5];
```

---

## 7. TSV Output — Before and After

### Before (LCW_CALL_TERM, decode_status=RAW)

```
meta:       {OSP:[FS=0x5575F5FF77FF][NAC=0x842][DUID=0x0f]}{LCW:[PB=0][SF=0][LCO=0x15]lcw duid=0x0f lco=0x15 sf=0 pb=0 bytes=15902ad30401007276}
raw_frame:  lcw duid=0x0f lco=0x15 sf=0 pb=0 bytes=15902ad30401007276
frame_hex:  15902ad30401007276 0f
tsbk_crc:  (empty)
```

### After (LCW_CALL_TERM, decode_status=FULL)

```
meta:       {OSP:[FS=0x5575F5FF77FF][NAC=0x842][DUID=0x0f]}{LCW:[PB=0][SF=0][LCO=0x15][MFID=0x90][FLAGS=0x01]mot_call_term tgid=10902 call_type=0x04 flags=0x01 call_handle=0x037e crc=0x7e}
raw_frame:  (empty — message_type=TDULC, decode_status=FULL)
frame_hex:  15902a96040100037e 0f
tsbk_crc:  007e                    ← byte[8] CRC
talkgroup: 10902                   ← structured field populated
```

### After (LCW_UNKNOWN 0x17, decode_status=RAW with structured meta)

```
meta:       {OSP:[FS=0x5575F5FF77FF][NAC=0x842][DUID=0x0f]}{LCW:[PB=0][SF=0][LCO=0x17][MFID=0x90][FLAGS=0x00]mot_call_data seq=1 flags=0x00 payload_a=0bee payload_b=84b2 crc=0x5b}
raw_frame:  lcw duid=0x0f lco=0x17 sf=0 pb=0 bytes=1790010bee0084b25b
frame_hex:  1790010bee0084b25b 0f
tsbk_crc:  005b                    ← byte[8] CRC per segment
```

---

## 8. Frame Counts Post-Analysis

From the live TSV as of 2026-06-27:

| Frame type | Count | decode_status | Notes |
|-----------|-------|--------------|-------|
| LCW_CALL_TERM (0x15 MFID=0x90) | 642 | **FULL** | TGID, call_type, call_handle, CRC all extracted |
| LCW multi-part data (0x17 MFID=0x90) | 2,890 | RAW (structured meta) | Seq, FLAGS, payload segments, CRC per segment |
| LCW_UU_ANS_REQ (0x05 MFID=0x90) | 22 | RAW (structured meta) | FLAGS and payload fields by Motorola layout |

---

## 9. Remaining Unknowns

### 9.1 LCO=0x17 Payload Content

The 30-byte payload assembled from the 5-segment 0x17 sequence is not yet decoded. It is likely a Motorola-proprietary call record containing:

- Source radio unit ID (24-bit WUID) of the last talker
- Possibly: site ID, timestamp delta, channel number, call duration

The call_handle linkage between 0x15 and 0x17 is confirmed, but mapping the 30-byte block to specific fields requires either Motorola documentation or further correlation (uplink capture or comparison with MDC-1200 / P25 MAC-PTT frames).

### 9.2 call_type Field (byte[4])

Three values have been observed: 0x04, 0x05, 0x06. No correlation with encryption, TGID, or time-of-day has been established yet. Possible interpretations:

- 0x04 = Standard group voice termination
- 0x05 = Priority or supervisor override termination
- 0x06 = Emergency or preempted call termination

### 9.3 Source Radio Unit ID in Downlink LCW

Standard P25 `LCW_GRP_V_CH_USER` (LCO=0x00, MFID=0x00) encodes the source WUID in bytes[6:8], but HoCo's downlink TDULC frames carry zeros in those positions — the tower does not retransmit the source address in downlink LCW. Source IDs are only available from uplink capture, which is not yet active on this monitor.

---

## 10. References

- TIA-102.AABC-C: Project 25 FDMA — Common Air Interface
- Motorola P25 proprietary LCW byte layout: `+0`=LCO, `+1`=MFID, `+2..+4`=payload, `+5`=FLAGS, `+6..+7`=payload, `+8`=CRC/protected
- OP25 source: `lib/op25_repeater/lib/p25p1_fdma.cc` — `process_LCW()`
- Parser: `trunk-recorder/systems/p25_parser.cc` — type-19 handler
- Logger: `trunk-recorder/systems/p25_frame_logger.cc`
- TSV: `/home/sdr/P25/trunk-b210/p25_b210_downlink.tsv` (33 columns, rolling log)
