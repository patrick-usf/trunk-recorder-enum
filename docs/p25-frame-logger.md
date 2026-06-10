# P25 Control Frame Logger

## Purpose

Standard trunk-recorder is built around voice call recording. It decodes P25 control channel frames only insofar as they are needed to manage recordings — frames that don't drive a trunking decision are discarded. For network analysis, this means a large portion of the control channel traffic is lost.

This modification adds a structured frame logger that captures **every control frame** as it is decoded, writing a TSV record to disk for offline analysis. The goal is network enumeration: understanding what a P25 system is doing at the protocol level, surfacing proprietary vendor extensions, and building a complete picture of control channel traffic.

---

## What Was Changed and Why

### Problem 1 — Motorola MFID Erasure (Phase 2)

**File:** `lib/op25_repeater/lib/p25p2_tdma.cc`

When a Phase 2 TDMA system sends an abbreviated MAC PDU that gr-op25 converts to TSBK format for the parser, the MFID (Manufacturer ID) byte was unconditionally overwritten with `0x00`:

```cpp
// before
tsbk[3] = 0x00;   // mfrid  ← discards actual MFID

// after
tsbk[3] = mfid;   // mfrid  ← preserves network value
```

The `mfid` parameter was already being extracted and passed to the function — it just wasn't being used. This meant Motorola Phase 2 abbreviated MAC PDUs arrived at the parser with MFID `0x00` and were decoded as standard frames, losing the vendor identification.

---

### Problem 2 — Non-MBT PDUs Silently Dropped

**File:** `lib/op25_repeater/lib/p25p1_fdma.cc`

`process_PDU()` only forwarded Multi-Block Trunking PDUs (`SAP == 61`, format `0x15` or `0x17`) to the message queue. Every other PDU type — data channel headers, SNDCP packets, Motorola proprietary data PDUs — was logged to stderr at debug level 10 and discarded:

```cpp
// before
} else if (d_debug >= 10) {
    fprintf(stderr, "...non-MBT message ignored\n");
}

// after
} else {
    if (d_debug >= 10)
        fprintf(stderr, "...non-MBT PDU: SAP=%02x fmt=%02x forwarded\n", sap, fmt);
    process_duid(M_P25_RAW_PDU, framer->nac, deinterleave_buf[0].data(), 12);
}
```

A new message type constant `M_P25_RAW_PDU = 20` was added to `op25_msg_types.h` and a corresponding handler was added to `parse_message()`.

---

### Problem 3 — Unknown Opcodes Returned as Empty

**Files:** `trunk-recorder/systems/p25_parser.cc`

Both `decode_tsbk()` and `decode_mbt_data()` ended their opcode dispatch with:

```cpp
} else {
    BOOST_LOG_TRIVIAL(debug) << "tsbk other " << opcode;
    return messages;   // ← empty vector, frame gone
}
```

Any opcode not explicitly handled — including all undocumented Motorola proprietary opcodes — was silently discarded. These are exactly the frames most valuable for network analysis.

The fix pushes a `UNKNOWN`-type message with `raw_frame` populated with the hex-encoded frame bytes, so the frame appears in the log with its full raw content even if it has no named decode.

---

### Problem 4 — Phase 2 Manufacturer MAC PDUs Had No Handler

**File:** `trunk-recorder/systems/p25_parser.cc`

Phase 2 TDMA manufacturer-specific MAC PDUs arrive as message type 18 (`M_P25_MAC_PDU`). The `parse_message()` function had no `else if (type == 18)` branch, so these fell through to a generic push with no field population. A handler was added that extracts MFID, opcode, and hex payload.

---

### Problem 5 — MFID Not Preserved in Decoded Messages

**File:** `trunk-recorder/systems/parser.h`

`TrunkMessage` had no field for the Manufacturer ID byte. The MFID was extracted locally in the decode functions to branch on vendor, but was not stored — the logger and any downstream consumer had no way to know whether a frame was standard APCO or Motorola/M/A-COM/etc.

Three fields were added to `TrunkMessage`:

```cpp
unsigned long mfid;        // 0x00=standard, 0x90=Motorola, 0xA4=M/A-COM, ...
FrameDirection direction;  // DIR_OSP=downlink, DIR_ISP=uplink, DIR_UNKNOWN
std::string raw_frame;     // hex bytes for unknown/undecoded frames
```

And a new enum:

```cpp
enum FrameDirection { DIR_OSP = 0, DIR_ISP = 1, DIR_UNKNOWN = 2 };
```

The `direction` field exists because P25 uses the same 6-bit opcode field for both OSP (tower → radio, downlink) and ISP (radio → tower, uplink) frames, and the same opcode number means different things in each direction. All current captures are tagged `DIR_OSP`. The field is the hook for a future uplink receiver.

---

### New: P25FrameLogger

**Files:** `trunk-recorder/systems/p25_frame_logger.h`, `trunk-recorder/systems/p25_frame_logger.cc`

A thread-safe singleton logger that appends a TSV record for every message that passes through `parse_message()`. No opcode filter — every frame is logged regardless of whether trunk-recorder needs it for trunking.

Activated by adding `"controlFrameLog": "/path/to/p25_frames.tsv"` to the JSON config.

Log rolling is enabled automatically: when the current log file reaches **50 MB**, it is closed and renamed with a UTC timestamp suffix, and a new file is opened with a fresh header.

---

## TSV Column Reference

| Column | Description |
|--------|-------------|
| `timestamp` | ISO 8601 UTC with milliseconds (`2026-06-10T03:27:55.241Z`) |
| `sys_name` | Short system name from config (`HowardCounty`) |
| `nac` | Network Access Code (`0x842`) |
| `direction` | `OSP` (tower→radio) or `ISP` (radio→tower) |
| `frame_type` | `TSBK`, `MBT`, `MAC_PDU`, `RAW_PDU` |
| `mfid` | Manufacturer ID byte (`0x00`=standard, `0x90`=Motorola, `0xA4`=M/A-COM) |
| `opcode_hex` | Raw opcode value (`0x3b`) |
| `opcode_name` | Named opcode string (`TSBK_NET_STS_BCAST`) |
| `decode_status` | `FULL`=all fields decoded, `RAW`=unknown opcode hex captured, `PARTIAL`=identified but incomplete |
| `talkgroup` | Talk group ID (`0` if not applicable) |
| `source_id` | Source unit ID (`-1` if not applicable) |
| `freq_mhz` | Associated frequency in MHz (`851.5125`, `0` if not applicable) |
| `emergency` | Emergency flag (`1`/`0`) |
| `encrypted` | Encryption flag (`1`/`0`) |
| `phase2_tdma` | Phase 2 TDMA flag (`1`/`0`) |
| `tdma_slot` | TDMA slot number |
| `wacn` | Wide Area Communication Network ID |
| `sys_id` | System ID |
| `rfss_id` | RF Sub-System ID |
| `site_id` | Site ID |
| `raw_frame` | Hex-encoded raw bytes (populated for `RAW` and `MAC_PDU` frames) |
| `meta` | Human-readable decode summary string from the parser |

---

## Opcode Naming Convention

Opcode names in the logger follow this pattern:

- `TSBK_` — standard APCO TSBK opcode, MFID 0x00
- `TSBK_MOT_` — Motorola proprietary TSBK opcode, MFID 0x90
- `TSBK_MACOM_` — M/A-COM proprietary TSBK opcode, MFID 0xA4
- `MBT_` — Multi-Block Trunking PDU opcode
- `MBT_MOT_` — Motorola MBT opcode

Frames with `decode_status=RAW` have opcode names ending in `_UNKNOWN` and their raw bytes in `raw_frame`.

---

## Configuration

Add to your trunk-recorder JSON config:

```json
{
    "controlFrameLog": "/path/to/p25_frames.tsv"
}
```

The file is created on first run. If it already exists, new records are appended. The TSV header is written only when the file is empty. Log rolling creates files named `p25_frames_YYYYMMDD_HHMMSSZ.tsv` when the active file exceeds 50 MB.

---

## Development Status

### Complete
- [x] `P25FrameLogger` singleton with TSV output and log rolling (50 MB threshold)
- [x] MFID preservation fix in Phase 2 abbreviated MAC conversion
- [x] Non-MBT PDU forwarding (`M_P25_RAW_PDU = 20`)
- [x] Unknown opcode raw capture in `decode_tsbk` and `decode_mbt_data`
- [x] Phase 2 manufacturer MAC PDU handler (type 18)
- [x] `mfid`, `direction`, `raw_frame` fields on `TrunkMessage`
- [x] `controlFrameLog` JSON config key
- [x] B210 sc16 OTW format for USB2 operation

### In Progress / Known Issues
- [ ] **Opcode 0x16 raw bytes not captured** — when MFID is 0x90 and opcode is 0x16, the standard `else if (opcode == 0x16)` branch fires before the MFID check, so no raw_frame is populated. MFID guards need to precede standard opcode dispatch for all opcodes where Motorola uses the same opcode number.
- [ ] **MFID 0x90 opcode 0x0b not identified** — appears as constant all-zero payload at ~150 ms cadence. Believed to be a Motorola network status or heartbeat. Identity to be confirmed against boatbod/op25 and published research.
- [ ] **MFID 0x90 opcode 0x16 not identified** — believed to be a Motorola variant of SNDCP channel announcement.

### Planned
- [ ] Look up 0x0b and 0x16 in boatbod/op25 and DEFCON P25 research papers; add named decodes
- [ ] Fix MFID-before-opcode dispatch ordering for all overlapping opcode numbers
- [ ] ISP (uplink) opcode decode table — gated on `direction == DIR_ISP`; requires uplink receiver hardware
- [ ] Log compression (deferred by design — add gzip rotation when file retention becomes a concern)
- [ ] Additional Motorola MFID 0x90 decodes as opcodes are identified from live captures

---

## Live Capture Sample (Howard County MD, 2026-06-10)

Opcode distribution from ~1 minute of control channel capture:

| Count | Frame | MFID | Opcode | Name | Status |
|-------|-------|------|--------|------|--------|
| 1374 | TSBK | 0x00 | 0x39 | TSBK_SCCB | FULL |
| 1030 | TSBK | 0x00 | 0x33 | TSBK_IDEN_UP_TDMA | RAW |
| 1029 | TSBK | 0x00 | 0x3d | TSBK_IDEN_UP | RAW |
| 694 | TSBK | 0x00 | 0x16 | TSBK_SNDCP_CH_ANNOUNCE_EXP | RAW |
| 686 | TSBK | 0x90 | 0x09 | TSBK_MOT_OSP_SYSTEM_LOADING | RAW |
| 686 | TSBK | 0x90 | 0x05 | TSBK_MOT_OSP_TRAFFIC_CH_ID | RAW |
| 686 | TSBK | 0x00 | 0x3b | TSBK_NET_STS_BCAST | FULL |
| 686 | TSBK | 0x00 | 0x3a | TSBK_RFSS_STS_BCAST | FULL |
| 686 | TSBK | 0x00 | 0x30 | TSBK_TDMA_SYNC_BCAST | RAW |
| 685 | TSBK | 0x90 | 0x16 | TSBK_MOT_UNKNOWN | RAW |
| 368 | TSBK | 0x90 | 0x0b | TSBK_MOT_UNKNOWN | RAW |
| 393 | TSBK | 0x00 | 0x02 | TSBK_GRP_V_CH_GRANT_UPDT | FULL |
| 149 | TSBK | 0x00 | 0x15 | TSBK_SNDCP_CH_REQ | RAW |
| 135 | TSBK | 0x00 | 0x14 | TSBK_SNDCP_CH_GRANT | FULL |
| 52 | TSBK | 0x00 | 0x00 | TSBK_GRP_V_CH_GRANT | FULL |
| 18 | TSBK | 0x00 | 0x2c | TSBK_U_REG_RSP | FULL |
| 18 | TSBK | 0x00 | 0x20 | TSBK_ACK_RSP_FNE | FULL |

Note: `TSBK_SNDCP_CH_REQ` (0x15) instances are likely ISP frames from subscriber units captured on the downlink receiver — these are radio-to-tower requests visible because the SDR is receiving both directions on the control channel frequency.
