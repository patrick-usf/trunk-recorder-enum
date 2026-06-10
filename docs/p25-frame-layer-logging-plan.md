# P25 Frame Layer Logging — Expansion Plan

## What Changed in This Commit

Before writing the expansion plan, three gaps in the existing logging were closed:

### 1. TSBK stub opcode raw_frame capture

Seventeen opcode branches in `decode_tsbk()` previously did only a `BOOST_LOG_TRIVIAL(debug)` call and fell to the final push with `raw_frame` empty and `message_type == UNKNOWN`. These frames were technically logged but the payload bytes were invisible.

**Fix:** A single auto-fill block added at the bottom of `decode_tsbk()`, just before the final push, checks `message_type == UNKNOWN && raw_frame.empty()` and populates `raw_frame` with the full 12 hex-encoded TSBK bytes plus `op=` and `mfid=` tags. Also sets `meta` to `stub_tsbk op=0x...` if meta was empty. This catches all current stub opcodes and any future ones added without raw_frame handling, without touching 20+ individual branches.

Affected opcodes now capturing raw bytes: `0x08, 0x09` (non-Motorola), `0x0a, 0x15, 0x16, 0x18, 0x1a, 0x1c, 0x1d, 0x1f, 0x21, 0x24, 0x27, 0x2a, 0x2d, 0x2e, 0x30` (non-MACOM), `0x31, 0x32, 0x33` (non-standard mfrid), `0x35, 0x36, 0x37, 0x38, 0x3c` (partial decode), and any opcode-value/frequency combination that resolves UNKNOWN.

The same pattern was applied to `decode_mbt_data()` for UNKNOWN MBT frames (opcode 0x02 non-Motorola, opcode 0x3c, opcode 0x3b when frequency IDs don't resolve).

### 2. TDULC (type 15) logging

`parse_message()` handled type 15 by setting `message_type = TDULC` and then falling through to the bottom push — without ever calling the logger. TDULC frames on the control channel (received at ~handful per minute as calls terminate) were silently absent from the TSV.

**Fix:** Explicit type 15 handler now captures up to 12 raw bytes from the payload, calls `P25FrameLogger::instance().log_messages()`, and returns. The `TDULC` message_type is preserved in the returned vector so the trunking logic (channel retune) still functions correctly.

### 3. DUID column added to TSV

The DUID (Data Unit Identifier, 4-bit NID field) is the lowest-layer frame type identifier on the P25 air interface — every decoded frame has one. Added `duid` as column 4 in the TSV (between `nac` and `direction`). The value is derived from the GNURadio message queue type, which equals the DUID decimal value for standard FDMA types:

| frame_type | DUID   | Name  |
|-----------|--------|-------|
| 7         | `0x07` | TSBK  |
| 12        | `0x0c` | PDU/MBC (MBT format) |
| 15        | `0x0f` | TDULC |
| 20        | `0x0c` | PDU/MBC (non-MBT format) |
| 18, other | `0xff` | Phase 2 / synthesized — no standard FDMA DUID |

**Note:** The TSV schema is now 23 columns (was 22). Existing capture files from before this change have one fewer column and will misalign if loaded without accounting for this.

---

## Expansion Plan: Full P25 Frame Layer Logging

### Background — P25 Phase 1 FDMA frame taxonomy

Every P25 FDMA frame carries a 4-bit DUID in its NID (Network ID), decoded by the BCH(63,16,23) framer. The DUID identifies the frame body format:

| DUID | Decimal | Name | Content |
|------|---------|------|---------|
| 0x00 | 0  | HDU   | Header Data Unit — starts a voice call; contains crypto parameters (Algorithm ID, Key ID, MI) |
| 0x03 | 3  | TDU   | Terminator Data Unit — silent call end, no link control |
| 0x05 | 5  | LDU1  | Logical Data Unit 1 — voice frame + Link Control Word (first portion) |
| 0x07 | 7  | TSBK  | Trunking Signaling Block — control frames (**currently logged**) |
| 0x09 | 9  | LDU2  | Logical Data Unit 2 — voice frame + Link Control Word (second portion) + Encryption Sync Sequence |
| 0x0C | 12 | PDU   | Packet Data Unit — data/MBT (**currently logged via types 12 and 20**) |
| 0x0F | 15 | TDULC | Terminator with Link Control — call end with LC word (**now logged**) |

P25 Phase 1 has no DUID 0x01, 0x02, 0x04, 0x06, 0x08, 0x0A, 0x0B, 0x0D, 0x0E — these are reserved or DMR/NXDN specific.

gr-op25 currently delivers frames to trunk-recorder via the message queue for DUIDs 0x07, 0x0C, and 0x0F. DUIDs 0x00, 0x05, and 0x09 are processed internally for voice recording but their data is not forwarded.

---

### Plan A — LCW Logging (Link Control Word from voice frames)

**What it is:** The Link Control Word is a 72-bit field carried across LDU1, LDU2, and TDULC frames. It identifies the active call and is the primary source of per-call metadata on voice channels. Its opcode (LCCO — Link Control Channel Opcode, 6 bits) determines the field layout.

**Why it matters:** LCW contains:
- Active talkgroup / called subscriber address
- Source subscriber address
- Encryption state per call
- The `Encryption Control (LCCO 0x10)` opcode carries Algorithm ID and Key ID explicitly — this is the primary per-call crypto identification source
- RFSS/Site/System status embedded in voice calls

**Message type already reserved:** `M_P25_FDMA_LCW = 19` is defined in `op25_msg_types.h`.

**What needs to happen in gr-op25:**

In `p25p1_fdma.cc`, the LCW assembly is spread across multiple processes but the complete word is assembled in `process_LDU1()` and `process_LDU2()`. After assembly and RS12 decode, add:

```cpp
// In process_LDU1(), after LC word is assembled and validated:
std::string lcw_payload(10, '\0');
lcw_payload[0] = (uint8_t)(nac >> 8);
lcw_payload[1] = (uint8_t)(nac & 0xff);
// encode 9 bytes of LC word + 1-byte source indicator (0=LDU1, 1=LDU2)
for (int i = 0; i < 9; i++) lcw_payload[2 + i] = lc[i];
lcw_payload[9] = 0x01;  // source: LDU1
send_msg(lcw_payload, M_P25_FDMA_LCW);
```

Note: The NAC prefix is already stripped in `parse_message()` before type dispatch, so bytes `[0..1]` of the payload carry the NAC as all other message types do.

**What needs to happen in parse_message:**

```cpp
} else if (type == 19) { // LCW from LDU1 or LDU2
    if (s.length() >= 9) {
      uint8_t ldu_source = (s.length() >= 10) ? (uint8_t)s[9] : 0;
      uint8_t lcco       = (uint8_t)s[0] & 0x3f;  // LC opcode
      message.direction  = DIR_OSP;
      message.opcode     = lcco;
      // decode key LCCO types
      // LCCO 0x00: Group Voice Channel User
      //   tgid = bytes[4..5], source = bytes[6..8]
      // LCCO 0x03: Unit to Unit Voice Channel User
      //   source = bytes[3..5], target = bytes[6..8]
      // LCCO 0x10: Encryption Control
      //   algorithm_id = byte[2], key_id = bytes[3..4]
      // etc.
      std::ostringstream raw;
      raw << "lcw ldu=" << (int)ldu_source << " lcco=0x"
          << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)lcco
          << " bytes=";
      for (size_t i = 0; i < 9 && i < s.length(); i++)
        raw << std::setw(2) << (unsigned int)(uint8_t)s[i];
      message.raw_frame = raw.str();
      message.meta      = message.raw_frame;
    }
    message.message_type = UNKNOWN;
    messages.push_back(message);
    P25FrameLogger::instance().log_messages(messages, system, 19);
    return messages;
}
```

**LCCO name table** to add to `p25_frame_logger.cc` `opcode_name()` (new case for frame_type == 19):

| LCCO | Name |
|------|------|
| 0x00 | LCW_GRP_V_CH_USER |
| 0x03 | LCW_UU_V_CH_USER |
| 0x08 | LCW_TEL_INT_V_CH_USER |
| 0x10 | LCW_ENC_CTRL |
| 0x0F | LCW_ENC_PROD_CTRL |
| 0x15 | LCW_CALL_TERM |
| 0x1C | LCW_RFSS_STS_BCAST |
| 0x1D | LCW_NET_STS_BCAST |
| 0x1F | LCW_CALL_ALERT |
| 0x20 | LCW_ACK_RSP |
| 0x34 | LCW_IDEN_UP_TDMA |
| 0x35 | LCW_TIME_DATE |
| 0x39 | LCW_SEC_RFSS_BCAST |
| 0x3A | LCW_ADJ_STS_BCAST |
| 0x3B | LCW_NET_STS_BCAST_EXP |

**DUID column value for frame_type 19:** `0x05` (LDU1) or `0x09` (LDU2) — need the `ldu_source` byte from the payload to distinguish. Update `format_record()`:
```cpp
case 19: duid_str = (/* ldu_source byte */ == 1) ? "0x05" : "0x09"; break;
```
Since format_record doesn't currently have access to the payload, pass `ldu_source` via `msg.meta` prefix or add a `duid` field to `TrunkMessage`.

**Recommended approach:** Add `uint8_t duid` to `TrunkMessage` in `parser.h` and populate it in each parse_message handler. This eliminates the derivation hack and lets future LDU1/LDU2/HDU frames carry their correct DUID naturally.

---

### Plan B — HDU Logging (Header Data Unit — encryption header)

**What it is:** The HDU is the first frame of every voice call on a traffic channel. It contains:
- **MI (Message Indicator):** 72-bit crypto nonce/IV — unique per call
- **Algorithm ID:** 8-bit encryption algorithm identifier
  - 0x80 = unencrypted (clear)
  - 0x84 = AES-256 (CAP)
  - 0x85 = DES-OFB
  - 0x88 = Triple-DES (2-key)
  - 0xAA = RC4 (ADP)
  - Others are vendor-specific
- **Key ID:** 16-bit key identifier
- **Talkgroup ID:** 16-bit TGID

**Why it matters:** Every encrypted call announcement appears first in the HDU. Capturing all HDUs gives a complete picture of encryption deployment across the network — which TGIDs use which algorithms and keys, whether key IDs are rotated, MI patterns, etc.

**New message type:** `M_P25_HDU = 22` — add to `op25_msg_types.h`.

**What needs to happen in gr-op25:**

In `p25p1_fdma.cc::process_HDU()`, after the RS16 and Golay decodes, the MI, Algorithm ID, Key ID, and TGID are available in local variables. Add a send:

```cpp
// After HDU decode (existing rs16 and golay error correction):
std::string hdu_payload(14, '\0');
hdu_payload[0] = (uint8_t)(nac >> 8);
hdu_payload[1] = (uint8_t)(nac & 0xff);
// MI: 9 bytes
for (int i = 0; i < 9; i++) hdu_payload[2 + i] = mi[i];
// Algorithm ID: 1 byte
hdu_payload[11] = alg_id;
// Key ID: 2 bytes
hdu_payload[12] = (uint8_t)(key_id >> 8);
hdu_payload[13] = (uint8_t)(key_id & 0xff);
// TGID: 2 more bytes (extend payload to 16)
send_msg(hdu_payload, M_P25_HDU);
```

**What needs to happen in parse_message:**

```cpp
} else if (type == 22) { // HDU — voice call crypto header (DUID 0x00)
    if (s.length() >= 12) {
      uint8_t alg_id = (uint8_t)s[9];
      uint16_t key_id = ((uint8_t)s[10] << 8) | (uint8_t)s[11];
      std::ostringstream raw;
      raw << "hdu alg_id=0x" << std::hex << std::setfill('0') << std::setw(2) << (unsigned int)alg_id
          << " key_id=0x" << std::setw(4) << key_id << " mi=";
      for (int i = 0; i < 9; i++)
        raw << std::setw(2) << (unsigned int)(uint8_t)s[i];
      message.raw_frame = raw.str();
      message.meta      = message.raw_frame;
      message.encrypted = (alg_id != 0x80);
    }
    message.message_type = UNKNOWN;
    messages.push_back(message);
    P25FrameLogger::instance().log_messages(messages, system, 22);
    return messages;
}
```

**DUID column for HDU:** `0x00`.

**Separate crypto log option:** Given the analytical value of HDU data, a dedicated `p25_hdu.tsv` with columns `timestamp, sys_name, nac, tgid, algorithm_id, key_id, mi_hex` might be more useful than embedding it in the main frame log. Both can be written simultaneously.

---

### Plan C — PDU SAP Expansion

**Current state:** Non-MBT PDU frames (type 20) are captured as raw bytes with `sap=0x...` in the raw_frame string. The SAP value is not decoded to a name, and PDU field structure beyond the first 12 bytes is not captured.

**PDU Header Block field layout (DUID 0x0C):**
```
Bit  87-80: Reserved + MFID (8b)
Bit  79:    A/D flag (Acknowledged/Data)
Bit  78-76: Reserved
Bit  75-71: Pad Octet Count (5b)
Bit  70:    Reserved
Bit  69:    R/T flag
Bit  68-61: Sequence Number (8b)
Bit  60-53: Reserved
Bit  52-45: Data Unit Count (8b) — number of data blocks to follow
Bit  44-21: Source Logical Link ID (24b)
Bit  20:    Group flag
Bit  19-13: Reserved
Bit  12-0+: Destination LLID (varies)
// SAP is in a different position depending on format
```

**SAP name table** to add to `p25_frame_logger.cc`:

| SAP | Name |
|-----|------|
| 0x01 | UDT (Unconfirmed Data Transfer) |
| 0x02 | CLNP (Connectionless Network Protocol) |
| 0x03 | Circuit Data |
| 0x04 | Circuit Data Control |
| 0x05 | Data Radio Bearer |
| 0x06 | Circuit Data Service |
| 0x08 | Packet Data (Unconfirmed) |
| 0x09 | Packet Data (Confirmed) |
| 0x3D | MBT (already handled as type 12) |
| 0x80 | SNDCP (IP encapsulation) |
| 0x81 | SNDCP extended |
| 0xFF | Broadcast |

**Changes needed:**

1. In `p25_frame_logger.cc`, add `sap_name(uint8_t sap)` helper function.
2. In the type 20 handler in `parse_message()`, extract the SAP byte from `s[1]` (already in the raw_frame string as `sap=0x...`) and populate `message.meta` with a descriptive string including SAP name and format type.
3. Add `block_count` field extraction — currently discarded.

**Fragmentation tracking:**

PDU data blocks (the blocks following the header) are reassembled inside `p25p1_fdma::process_PDU()` and either forwarded as a complete MBT message (type 12) or, for non-MBT SAPs, forwarded as the raw header only (type 20). To log individual data blocks, a new approach is needed:

- New message type `M_P25_PDU_DATA = 23` sent for each data block in sequence
- Payload: NAC(2) + block_sequence(1) + block_count(1) + data(up to 18 bytes)
- This enables reassembly tracking and fragmentation analysis at the logger layer

This is the most invasive PDU change and should be done after A and B.

---

### Plan D — ESS (Encryption Sync Sequence from LDU2)

The ESS is embedded in LDU2 and updates the algorithm/key state for an in-progress encrypted call. It contains Algorithm ID and Key ID in the same format as the HDU. ESS captures mid-call key changes (rare but detectable with logging).

ESS data is available in `p25p1_fdma::process_LDU2()` after the RS8 decode. It can be sent as part of the LCW message (type 19) with a flag byte indicating ESS-sourced data, or as a separate type 24. Given the analytical similarity to HDU data, folding it into the LCW payload is simpler.

---

### Implementation Order

| Phase | Work | Files changed | Difficulty |
|-------|------|--------------|------------|
| **Done** | TSBK/MBT stub opcode raw_frame capture | `p25_parser.cc` | trivial |
| **Done** | TDULC (type 15) logging | `p25_parser.cc` | trivial |
| **Done** | DUID column in TSV | `p25_frame_logger.cc` | trivial |
| **1** | Add `uint8_t duid` to `TrunkMessage` in `parser.h` | `parser.h`, `p25_parser.cc`, `p25_frame_logger.cc` | simple |
| **2** | LCW logging: send_msg in `p25p1_fdma.cc` + type 19 handler + LCCO name table | `p25p1_fdma.cc`, `p25_parser.cc`, `p25_frame_logger.cc` | moderate |
| **3** | HDU logging: new type 22, send_msg in `process_HDU()`, handler, crypto fields | `p25p1_fdma.cc`, `op25_msg_types.h`, `p25_parser.cc`, `p25_frame_logger.cc` | moderate |
| **4** | PDU SAP name table + block_count field in type 20 handler | `p25_parser.cc`, `p25_frame_logger.cc` | simple |
| **5** | ESS folded into LCW type 19 payload | `p25p1_fdma.cc`, `p25_parser.cc` | simple |
| **6** | PDU data block logging (type 23) | `p25p1_fdma.cc`, `op25_msg_types.h`, `p25_parser.cc` | complex |

---

### TrunkMessage Schema Addition (Phase 1)

Add to `parser.h` `TrunkMessage`:

```cpp
uint8_t duid;          // raw DUID from NID (0x07=TSBK, 0x0c=PDU, 0x0f=TDULC, etc.)
uint8_t algorithm_id;  // encryption algorithm ID (from HDU/LCW ESS; 0x80=clear)
uint16_t key_id;       // encryption key ID (from HDU/LCW ESS)
```

Populate `duid` in each type handler in `parse_message()`, then read it in `format_record()` to replace the current derived `duid_str` switch.

---

### TSV Column Reference (Post-Expansion Target)

| Column | Source | Notes |
|--------|--------|-------|
| `timestamp` | logger | ISO 8601 UTC ms |
| `sys_name` | system | |
| `nac` | message | hex |
| `duid` | message.duid | hex |
| `direction` | message.direction | OSP/ISP/UNK |
| `frame_type` | frame_type arg | TSBK/MBT/TDULC/LCW/HDU/MAC_PDU/RAW_PDU |
| `mfid` | message.mfid | hex |
| `opcode_hex` | message.opcode | hex |
| `opcode_name` | logger lookup | named constant |
| `decode_status` | derived | FULL/PARTIAL/RAW |
| `talkgroup` | message.talkgroup | |
| `source_id` | message.source | |
| `freq_mhz` | message.freq | |
| `emergency` | message.emergency | 1/0 |
| `encrypted` | message.encrypted | 1/0 |
| `phase2_tdma` | message.phase2_tdma | 1/0 |
| `tdma_slot` | message.tdma_slot | |
| `wacn` | message.wacn | |
| `sys_id` | message.sys_id | |
| `rfss_id` | message.sys_rfss | |
| `site_id` | message.sys_site_id | |
| `raw_frame` | message.raw_frame | hex bytes + tagged fields |
| `meta` | message.meta | human-readable summary |

The `algorithm_id` and `key_id` fields will live in `meta` as `alg_id=0x... key_id=0x...` for HDU and LCW frames rather than dedicated columns, keeping the column count stable after Phase 1.
