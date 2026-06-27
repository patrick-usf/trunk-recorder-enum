# P25 PDU Data Block Decode — Research & Implementation Log

**System under study:** Howard County, MD — Motorola Astro 25 P25 Phase 1 FDMA  
**NAC:** 0x842, WACN: 0xBEE00, SYS: 0x84B  
**Goal:** Decode SNDCP IP payload from multi-block PDU frames (DUID=0x0C, fmt=0x16)

---

## Background

P25 PDUs carry SNDCP (IP-over-radio) data packets. Each PDU consists of a header block
followed by 1–N data blocks. The header block uses **1/2 rate trellis coding** and decodes
to 12 bytes. Data blocks use **3/4 rate trellis coding** and decode to 18 bytes each.

Howard County grants SNDCP data channels frequently (~2,500+ grants observed). Typical
SNDCP PDU: `fmt=0x16, sap=0x0b (user_data), blks=4` → header (12 B) + 4 data blocks (72 B) =
84 bytes total payload carrying an IP packet.

---

## Phase 1: PDU Frames Not Appearing at All

### Symptom
Zero PDU (DUID=0x0C) frames captured despite abundant SNDCP channel grants.

### Investigation
- SNDCP grants observed on CC: 2,468+ over monitoring period
- Granted channels (851.x, 852.x MHz) within SDR capture range
- Other DUIDs (TSBK, LDU1, TDU) decoded fine on same monitor

### Root Cause Found
`process_PDU` in `p25p1_fdma.cc` silently discarded ALL PDU frames because `process_blocks`
returned -1 whenever any block failed trellis decode, and the caller checked `rc == 0`
before doing anything:

```cpp
// OLD — discarded even valid header blocks
if (process_blocks(fr, fr_len, deinterleave_buf) != 0 || deinterleave_buf.size() == 0)
    return;
```

For multi-block PDUs, the framer accumulated `fr_len=962` bits (the old `max_frame_lengths[12]`),
which is enough for 4 blocks but data blocks 2–4 always failed trellis decode (wrong coding
rate — see Phase 2). `process_blocks` returned -1 after the first data block failure, even
though the header block decoded correctly.

### Fix
Changed `process_PDU` to accept partial decode: proceed if at least the header block
decoded, regardless of `process_blocks` return value:

```cpp
process_blocks(fr, fr_len, deinterleave_buf);
if (deinterleave_buf.size() > 0) {  // ignore return code
    if (crc16(deinterleave_buf[0].data(), 12) != 0)
        return;  // validate header CRC
    // ... proceed with header data
```

Also increased `max_frame_lengths[12]` in `p25_framer.cc` from 962 → 1152 bits to allow
full accumulation of up to 5 blocks (header + 4 data):

```
5 blocks × 101 raw dibits/block + 57 sync/NID dibits = 562 dibits = 1124 bits → 1152 ≥ 1124
```

### Result
PDU header blocks now logged. Debug confirmed: `decoded=1` for all `blks≥1` frames,
meaning the header decoded but data blocks still failed.

---

## Phase 2: Data Blocks Fail Trellis Decode — Status Symbol Misalignment

### Symptom
`[PDU_BLK] nac=0x842 fmt=16 blks=4 decoded=1` — header only, no data blocks.

### Initial Hypothesis
Motorola proprietary encoding for `fmt=0x16` data blocks.

### Investigation
Examined status symbol removal in `process_blocks`. The existing code removed status
dibits at global period-36 positions: `(d+1) % 36 == 0`.

Cross-referenced `ul_frame_capture.py` which confirmed the actual status positions:
```python
_PDU_STATUS_IDX = frozenset([14, 50, 86])  # block-relative positions
```

Each PDU block is **101 raw dibits** with status at positions 14, 50, 86 within that block.
The global period-36 removal works correctly for Block 0 (body starts at d=57):
- 57+14=71, 57+50=107, 57+86=143 → (72)%36=0, (108)%36=0, (144)%36=0 ✓

But drifts by **−7 dibits per subsequent block** because 101 ≠ 100 (not a multiple of 36):

| Block | Actual status dibits | period-36 removes at | Drift |
|-------|---------------------|----------------------|-------|
| 0     | 71, 107, 143        | 71, 107, 143         | 0     |
| 1     | 172, 208, 244       | 179, 215, 251        | −7    |
| 2     | 273, 309, 345       | 287, 323, 359        | −14   |
| 3     | 374, 410, 446       | 395, 431, 467        | −21   |

The wrong bits were being removed, corrupting blocks 1+.

### Fix
Replaced global period-36 removal with per-block block-relative status removal:

```cpp
for (unsigned int d=0; d < fr_len >> 1; d++) {
    if (d < 57) {
        if ((d+1) % 36 == 0) continue;   // NID-area status symbols
    } else {
        unsigned int block_off = (d - 57) % 101;
        if (block_off == 14 || block_off == 50 || block_off == 86) continue;
    }
    bv.push_back(fr[d*2]);
    bv.push_back(fr[d*2+1]);
}
```

### Result
Fix confirmed in binary (`strings libgnuradio-op25_repeater.so` showed new code).
Debug still showed `decoded=1` — status fix was necessary but insufficient.
Data blocks still fail, but now for the right reason: wrong trellis coding rate.

---

## Phase 3: Wrong Trellis Rate — 1/2 Rate vs 3/4 Rate

### Root Cause
`process_blocks` called `block_deinterleave` (1/2 rate) for ALL blocks including data blocks.

Per TIA-102.BAAA:
- **Header block**: 1/2 rate trellis → 196 channel bits → 48 dibits → 12 bytes
- **Data blocks**: 3/4 rate trellis → 196 channel bits → 48 **tribits** → 144 bits → **18 bytes**

The 1/2 rate decoder uses a 4-state machine (`next_words[4][4]`):
- 4 states × 4 dibits, output 4 bits/step
- `next_state = dibit` (dibit is 2 bits → 4 states)

The 3/4 rate decoder needs an 8-state machine (`next_words_34[8][8]`):
- 8 states × 8 tribits, output 4 bits/step  
- `next_state = tribit` (tribit is 3 bits → 8 states)

---

## Phase 4: Finding the 3/4 Rate State Table

### Candidates Examined

**YSF trellis in `ysf_tx_sb_impl.cc`**  
Uses `trellis_interleave(result, pre_trellis, 20, 5)` and `(20, 9)` — block dimensions
20×5=100 and 20×9=180 bits, not P25's 196-bit blocks. **Not applicable.**

**CDMRTrellis in `trellis.cc`**  
DMR 3/4 rate implementation. Key properties:
- `INTERLEAVE_TABLE[98]` — **identical to `_P25_INTERLEAVE[98]`** in our Python code
- `ENCODE_TABLE[64]` — 8 states × 8 tribits → point value 0–15
- `checkCode()` — 49-step Viterbi, `state = tribits[i]` (full tribit → 8 states)
- `dibitsToPoints()` — converts analog ±1,±3 dibit pairs to points 0–15

Identical INTERLEAVE_TABLE is strong evidence P25 and DMR share the same 3/4 rate code.

### Verifying the CMAP Identity

P25 uses binary dibits (0–3); DMR uses analog dibits (±1, ±3). The mapping from P25
binary to DMR analog:

```
P25 dibit 0 (0b00) → analog +1
P25 dibit 1 (0b01) → analog +3
P25 dibit 2 (0b10) → analog −1
P25 dibit 3 (0b11) → analog −3
```

Applying this to all 16 P25 nibble values and looking up DMR `dibitsToPoints()`:

| P25 nibble | analog pair  | DMR point |
|-----------|-------------|-----------|
| 0         | (+1, +1)    | 11        |
| 1         | (+1, +3)    | 12        |
| 2         | (+1, −1)    | 0         |
| 3         | (+1, −3)    | 7         |
| ...       | ...         | ...       |

This gives exactly `_P25_CMAP = [11,12,0,7,14,9,5,2,10,13,1,6,15,8,4,3]`.

**This proves the P25 1/2 rate table and DMR ENCODE_TABLE operate in the same "point space"
via CMAP.** The P25 CMAP is not an arbitrary table — it is precisely the binary-to-analog
constellation mapping. Verified for all 16 entries of the 1/2 rate `next_words` table:
`CMAP[next_words[s][j]] == FSM[s*4+j]` holds for all 16 combinations.

### Deriving `next_words_34`

Since CMAP bridges P25 binary and DMR analog, applying the inverse CMAP to DMR's
ENCODE_TABLE gives the P25 3/4 rate codewords directly:

```
inv_CMAP[v] = index i where CMAP[i] == v
inv_CMAP = [2,10,7,15,14,6,11,3,13,5,8,0,1,9,4,12]
```

Full table derived by: `next_words_34[s][j] = inv_CMAP[ENCODE_TABLE[s*8+j]]`

```python
ENCODE_TABLE = [
  0, 8, 4,12, 2,10, 6,14,   # state 0
  4,12, 2,10, 6,14, 0, 8,   # state 1
  1, 9, 5,13, 3,11, 7,15,   # state 2
  5,13, 3,11, 7,15, 1, 9,   # state 3
  3,11, 7,15, 1, 9, 5,13,   # state 4
  7,15, 1, 9, 5,13, 3,11,   # state 5
  2,10, 6,14, 0, 8, 4,12,   # state 6
  6,14, 0, 8, 4,12, 2,10,   # state 7
]
```

Result:

```
next_words_34[8][8]:
State 0: {0x2, 0xD, 0xE, 0x1, 0x7, 0x8, 0xB, 0x4}
State 1: {0xE, 0x1, 0x7, 0x8, 0xB, 0x4, 0x2, 0xD}
State 2: {0xA, 0x5, 0x6, 0x9, 0xF, 0x0, 0x3, 0xC}
State 3: {0x6, 0x9, 0xF, 0x0, 0x3, 0xC, 0xA, 0x5}
State 4: {0xF, 0x0, 0x3, 0xC, 0xA, 0x5, 0x6, 0x9}
State 5: {0x3, 0xC, 0xA, 0x5, 0x6, 0x9, 0xF, 0x0}
State 6: {0x7, 0x8, 0xB, 0x4, 0x2, 0xD, 0xE, 0x1}
State 7: {0xB, 0x4, 0x2, 0xD, 0xE, 0x1, 0x7, 0x8}
```

**Note on state count**: Early in this investigation we hypothesized P25 3/4 rate uses
4 states with `next_state = tribit & 3`. This was discarded after examining CDMRTrellis
more carefully: `state = tribits[i]` (not `& 3`), and the ENCODE_TABLE has 8 rows (all
reachable). The 4-state hypothesis was never confirmed in TIA-102.BAAA — if the 8-state
implementation does not produce valid SNDCP decodes, the 4-state variant using only rows
0–3 of `next_words_34` should be tested next.

---

## Phase 5: Implementation

### `block_deinterleave_34()` — new function in `p25p1_fdma.cc`

Added immediately after `block_deinterleave()`:

- Same `deinterleave_tb[196]` (identical permutation to 1/2 rate)
- Same 4-bit codeword formation per step
- 8-state machine, 49 steps, `next_state = find_min(hd, 8)`
- Output: 48 tribits × 3 bits = 144 bits packed MSB-first into 18 bytes
- Returns 0 on success, −1 if `find_min` ties at any step

### Decision: separate `block_vector` from data block decode

`block_array` is `std::array<uint8_t, 12>` — fixed size. Changing it to accommodate 18-byte
data blocks would require type surgery across all callsites and would break the MBT path
(which uses 12-byte blocks for all block indices via 1/2 rate trellis).

**Solution**: Leave `block_vector` and `process_blocks` untouched. In the non-MBT branch
of `process_PDU`, rebuild the status-stripped `bv` independently and call
`block_deinterleave_34` for each data block, accumulating results in a
`std::vector<std::vector<uint8_t>>`:

```cpp
// Non-MBT branch: rebuild bv for 3/4 rate decode
bit_vector bv34;
for (unsigned int d = 0; d < fr_len >> 1; d++) {
    if (d < 57) {
        if ((d+1) % 36 == 0) continue;
    } else {
        unsigned int block_off = (d - 57) % 101;
        if (block_off == 14 || block_off == 50 || block_off == 86) continue;
    }
    bv34.push_back(fr[d*2]);
    bv34.push_back(fr[d*2+1]);
}
std::vector<std::vector<uint8_t>> data_blks;
for (uint8_t bi = 1; bi <= blks; bi++) {
    unsigned int start = 48 + 64 + bi * 196;
    if (start + 196 > bv34.size()) break;
    std::vector<uint8_t> blk(18, 0);
    if (block_deinterleave_34(bv34, start, blk.data()) == 0)
        data_blks.push_back(std::move(blk));
    else
        break;
}
// Payload: 12-byte header + N × 18-byte data blocks
std::vector<uint8_t> payload;
payload.insert(payload.end(), deinterleave_buf[0].begin(), deinterleave_buf[0].end());
for (const auto& blk : data_blks)
    payload.insert(payload.end(), blk.begin(), blk.end());
```

**MBT path**: completely unchanged. MBT data blocks (fmt=0x17/0x15, sap=61) use 1/2 rate
trellis for all block indices, which already works. The non-MBT branch with 3/4 rate
decode is taken only when the fmt/sap combination indicates SNDCP data.

### Build status
Clean compile, zero warnings. Binary confirmed to contain new format string `data_decoded=`.

---

## Current Status (as of 2026-06-14)

- Monitors restarted with new library
- SNDCP traffic confirmed active (sndcp_req, mot_sndcp_announce visible in CC log)
- Waiting for first multi-block PDU frame to confirm `data_decoded > 0`
- Debug line will show: `[PDU_BLK] nac=0x842 fmt=16 blks=4 data_decoded=4` if 3/4 rate table is correct

---

## If 3/4 Rate Table Is Wrong — Next Steps

If `data_decoded=0` persists after the table is confirmed in the binary, test in order:

1. **4-state variant**: use only rows 0–3 of `next_words_34`, set `next_state = tribit & 3`.
   This is the other plausible interpretation of TIA-102.BAAA if P25 uses a 2-bit shift register.

2. **Bit reversal**: P25 CMAP nibble formation is `(d0<<2)|d1`; if it should be `(d1<<2)|d0`,
   the codewords would be different. Test by inverting the nibble bit order before the table lookup.

3. **Interleave direction**: the `deinterleave_tb` reads source at `bv[start + deinterleave_tb[b]]`.
   If the deinterleave is inverted (writing to `deinterleave_tb[b]` rather than reading from it),
   the bit input to the trellis would be scrambled. Verify against the Python `_P25_INTERLEAVE`
   usage in `ul_frame_capture.py`.

4. **SNDCP payload CRC**: even if the decode appears successful, validate SNDCP/IP headers
   in the decoded bytes to confirm correctness (magic bytes, protocol version, length fields).

---

## Key Files Modified

| File | Change |
|------|--------|
| `lib/op25_repeater/lib/p25p1_fdma.cc` | `block_deinterleave_34()`, non-MBT PDU 3/4 rate decode, status symbol fix |
| `lib/op25_repeater/lib/p25_framer.cc` | `max_frame_lengths[12]`: 962 → 1152 |

---

## Phase 6: p25.rs Analysis — Table Confirmed, Algorithm Diagnosis, Offline Plan

**Date:** 2026-06-14

### What We Found in kchmck/p25.rs

The [p25.rs project](https://github.com/kchmck/p25.rs) is a complete P25 implementation in Rust
with both 1/2-rate (`DibitDecoder`) and 3/4-rate (`TribitDecoder`) convolutional decoders, adapted
from *Coding Theory and Cryptography: The Essentials* (Hankerson, Hoffman, et al., 2000). Source
files examined: `src/coding/trellis.rs`, `src/data/interleave.rs`, `src/consts.rs`,
`src/data/coder.rs`.

#### The `next_words_34` table is correct

`TribitStates::pair_idx` in p25.rs gives an 8×8 table of **pair indices** (0–15) into a shared
`PAIRS` table of `(hi_dibit, lo_dibit)` values. Converting:

```rust
const PAIRS: [(u8, u8); 16] = [
    (0b00,0b10),(0b10,0b10),(0b01,0b11),(0b11,0b11),
    (0b11,0b10),(0b01,0b10),(0b10,0b11),(0b00,0b11),
    (0b11,0b01),(0b01,0b01),(0b10,0b00),(0b00,0b00),
    (0b00,0b01),(0b10,0b01),(0b01,0b00),(0b11,0b00),
];
```

Forming nibble `(hi << 2) | lo` for each `TribitStates::pair_idx[s][j]` entry produces:

```
State 0: {0x2, 0xD, 0xE, 0x1, 0x7, 0x8, 0xB, 0x4}  ← exactly matches our derived table
State 1: {0xE, 0x1, 0x7, 0x8, 0xB, 0x4, 0x2, 0xD}
State 2: {0xA, 0x5, 0x6, 0x9, 0xF, 0x0, 0x3, 0xC}
...
```

**The `next_words_34` table derived from DMR via CMAP is confirmed identical to p25.rs's authoritative
P25 implementation.** The table is not the bug.

#### The deinterleave table is also confirmed

`DeinterleaveRedirector::REDIRECTS[98]` in p25.rs matches `_P25_INTERLEAVE` used in our Python
capture code (same permutation, just expressed at the dibit level rather than the bit level).
`CODING_DIBITS = 98` confirms each block contributes exactly 98 channel dibits (101 raw − 3 status).

#### Three algorithmic differences from our implementation

| Property | Our greedy/full-traceback | p25.rs |
|----------|--------------------------|--------|
| Viterbi type | Greedy (`find_min`, requires unique min) OR full traceback (49-step) | Truncated Viterbi, sliding window = 4 steps |
| On ambiguity | Hard abort, returns -1 for whole block | Emits `Err(())` per symbol, continues |
| Input format | 4-bit nibbles assembled from deinterleaved bits | Dibit stream, consumed 2 at a time |

#### Why the greedy decoder always failed: parity ties

In `next_words_34`, all 8 codewords in rows 0, 1, 6, 7 have **odd parity**; rows 2–5 have
**even parity**. When the received nibble has the opposite parity from the table row (which happens
with any single-bit error, a common RF occurrence), every codeword is at Hamming distance ≥ 1,
and exactly 4 of the 8 entries sit at distance 1 — creating a 4-way tie. `find_min(hd, 8)` returns
-1 on every step, so the decoder never produces a single tribit. Even on a clean channel, a single
bit flip anywhere in the nibble causes this.

The full-traceback Viterbi avoids the per-step abort but there is an undiagnosed bug in the C++
implementation: `[PDU_BLK34]` diagnostic lines never appear in the log despite `blks > 0`, meaning
the inner loop does not execute — likely a `bv34.size()` vs. `fr_len` mismatch that needs a
`[PDU_DBG]` diagnostic print to isolate.

#### Why truncated Viterbi (window=4) works where greedy doesn't

Instead of making a final decision at each step, the truncated Viterbi:
1. Maintains 8 survivor paths (one per state) each with a 4-step history
2. At each step, updates all 8 paths from all possible predecessors
3. Yields the decision from 4 steps ago, when the path has had time to converge
4. Never aborts — emits `Err(())` on a tie but continues decoding the rest

This handles burst errors that would cause consecutive greedy failures, and eliminates the parity-tie
abort because path metrics accumulate across steps rather than being decided locally.

---

### Offline Development Plan

**Motivation:** Each iteration of the C++ Viterbi requires a full rebuild (~30 s) and a trunk-recorder
restart to pick up the new `.so`, then waiting for live PDU traffic to exercise the code. The offline
harness reduces that to a Python script that runs in under 1 second against a captured dataset.

#### Step 1 — Capture full multi-block PDU frames

`dl_frame_capture.py` currently captures only the header block (`DUID_PAY_DIBITS[0xC] = 101`).
Changing this to `505` (= 5 × 101 raw air dibits) captures the header + up to 4 data blocks per
frame. Each DB row then holds:

```
raw_bytes: 32 NID + 505 body = 537 dibits → ceil(1074/8) = 135 bytes packed (4 dibits/byte)
n_dibits: 537
pdu_blks: N from decoded header (how many of the 4 available blocks are valid data)
```

Block offsets within `all_dibits` (NID + body):

| Block | Dibit range | Content |
|-------|-------------|---------|
| Header | [32, 133) | 101 raw → 98 chan → 1/2-rate trellis → 12 bytes |
| Data 1 | [133, 234) | 101 raw → 98 chan → 3/4-rate trellis → 18 bytes |
| Data 2 | [234, 335) | same |
| Data 3 | [335, 436) | same |
| Data 4 | [436, 537) | same |

#### Step 2 — Python test harness (`/home/sdr/P25/pdu_trellis_test.py`)

Ports the p25.rs algorithm directly to Python:
- `PAIR_IDX[8][8]` from `TribitStates::pair_idx`
- `PAIRS[16]` dibit-pair table
- `DEINTERLEAVE[98]` from `DeinterleaveRedirector::REDIRECTS`
- Full-traceback Viterbi (49 steps, forced termination at state 0)
- Self-test using the p25.rs `test_tribit_decoder` encode→error→decode round-trip
- Live DB query: decode each captured frame, print per-block Viterbi cost and first 4 decoded bytes
- CRC9 validation per confirmed data block (polynomial TBD from first clean decode)

Runs instantly. Edit algorithm, re-run immediately — no rebuild needed.

#### Step 3 — Fix C++ `process_PDU` inner loop bug (separate, parallel)

The `[PDU_DBG]` diagnostic (already added to C++ but not yet built) will print `fr_len` and
`bv34.size()` to reveal why the data-block decode loop never executes. Fix independently of the
trellis algorithm work.

#### Step 4 — Port proven Python decoder back to C++

Once the Python harness decodes data blocks with passing CRC9, port the truncated Viterbi
(window=4, 8 states) to C++ as the final `block_deinterleave_34()` implementation.

---

## Key Files Modified

| File | Change |
|------|--------|
| `lib/op25_repeater/lib/p25p1_fdma.cc` | `block_deinterleave_34()`, non-MBT PDU 3/4 rate decode, status symbol fix |
| `lib/op25_repeater/lib/p25_framer.cc` | `max_frame_lengths[12]`: 962 → 1152 |
| `P25/dl_frame_capture.py` | `DUID_PAY_DIBITS[0xC]`: 101 → 505 (capture full multi-block PDU) |
| `P25/pdu_trellis_test.py` | New: offline 3/4-rate trellis test harness (p25.rs algorithm in Python) |
