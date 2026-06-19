#!/usr/bin/env python3
"""
P25 Downlink Frame Capture — UHD direct capture from B210 via USB2 (bare metal)
Howard County MD P25 Phase 1 — Band B DL control channels

PURPOSE: FEC validation run — validate BCH/CRC/Trellis FEC on clean downlink
  frames from the 25-100 W tower.

  Runs at 8 MSPS (USB 2.0 limit for B210 at 4 bytes/sample = 32 MB/s).
  Steps down to lower rates if UHD reports persistent overflow events.

  NOTE: B210 must be on an Intel EHCI (USB 2.0) port — the ASMedia ASM1042
  USB 3.0 controller on this motherboard is incompatible with the B210.

Coverage (center 859.0 MHz, 8 MSPS):
  Band B DL CCs:    858.738, 859.238, 859.738, 860.238 MHz  (all within ±1.25 MHz)
  All 4 CCs sit deep in the flat passband (±4 MHz coverage at 8 MSPS).

Architecture:
  Thread 1 (usrp_rx_thread) — UHD MultiUSRP → recv loop → _rx_queue.
                               Counts overflow events; requests rate step-down when
                               OVERFLOW_THRESH overflows occur in OVERFLOW_WINDOW chunks.
  Thread 2 (main)            — pulls chunks from _rx_queue; per-chunk:
    1. FFT  → energy detection (all channels in one pass)
    2. FFT/IFFT channelizer → complex baseband per active channel
    3. FM demodulate → float32 audio @ 48 kSPS
    4. P25 sync correlation (threshold 0.75) + NAC filter (0x842)
    5. Frame decode → SQLite
    On _rate_step_event: recomputes geometry, flushes queue, recreates channels.

Setup:
  nohup python3 dl_frame_capture.py > /home/sdr/P25/dl_capture.log 2>&1 &
  # B210 must be plugged into a USB 2.0 (Intel EHCI) port — NOT the blue ASMedia USB 3.0 ports
  # USRP_ARGS env var overrides device selection (default: serial=000000347)

Outputs:
  /home/sdr/P25/dl_frames.db   SQLite frame database
  stdout/stderr                 Operational log

FEC validation expectations (downlink):
  BCH NID  : nid_bch_errs=0 expected on virtually all frames (strong tower signal).
  Trellis  : trellis_errs=0 expected on all PDU frames.
  CRC      : crc_ok=1 expected on all TSBK/PDU/TDULC frames.
  LDU IMBE : golay_errs / hamming_errs always NULL (LDU hookup not yet implemented).
"""

import os, sys, time, signal, sqlite3, threading, queue
import numpy as np

# ── Configuration ─────────────────────────────────────────────────────────────
CENTER_HZ      = 859_000_000    # 859.0 MHz: all 4 Band B DL CCs within ±1.25 MHz (flat passband)
# Tower transmitter is ~3100 Hz above nominal (measured +2930 Hz peak on CC4 spectrum).
# Tuning the B210 LO up by this amount compensates: signal lands on the correct c_bin.
# Equivalent to trunk-recorder's "error" field on the Pi's HackRF config (-2450 Hz
# corrects for HackRF hardware offset; the Pi's TuningErr residual of ~-970 Hz suggests
# the actual tower offset is ~+3100 Hz, consistent with our spectrum measurement).
FREQ_CORRECTION_HZ = 3_100      # Hz: add to LO freq before tuning B210
RX_GAIN        = 40.0
CHUNK_SAMPLES  = 131_072          # samples per FFT chunk; fixed regardless of sample rate
AUDIO_RATE     = 48_000           # target audio rate after channelization
CHANNEL_BW_HZ  = 6_250            # ±Hz around channel centre (P25 = 12.5 kHz ch)
SQUELCH_DB     = -200.0   # Disabled: CCs broadcast 100% duty cycle, squelch would learn
                           # their signal as the noise floor and never fire.
MIN_ACTIVE     = 2                # consecutive above-squelch chunks to confirm
NOISE_WINDOW   = 60
WARMUP_CHUNKS  = 120              # chunks before squelch decisions
TAIL_CHUNKS    = 30               # chunks to keep decoding after squelch drops

EXPECTED_NAC   = 0x842            # Howard County
DB_PATH        = '/home/sdr/P25/dl_frames.db'
RX_QUEUE_DEPTH = 32               # max chunks queued between rx thread and main

# B210 USB2 bare-metal direct connection via UHD.
# USB 2.0 (Intel EHCI) max is ~32 MB/s at 8 MSPS (sc16 = 4 bytes/sample); start there.
# Steps down on persistent overflow.
USRP_ARGS      = os.environ.get('USRP_ARGS', 'serial=000000347')
SAMPLE_RATES   = [8_000_000, 6_000_000, 4_000_000]
OVERFLOW_THRESH   = 5     # overflows per evaluation window before stepping down
OVERFLOW_WINDOW   = 500   # evaluation window length in chunks

# SAMPLE_RATE is the live value; updated by _setup_geometry() on each rate change.
SAMPLE_RATE = SAMPLE_RATES[0]

# ── Channels (Downlink frequencies) ───────────────────────────────────────────
# All frequencies are downlink (base station TX); corresponding UL shown in label.
# Center 859.0 MHz: all 4 CCs within ±1.25 MHz — flat passband, no rolloff distortion.
# CC4 (860.238) is +1.238 MHz from center; CC1 (858.738) is -1.262 MHz from center.
CHANNELS = [
    (858_737_500, 'BandB-DL-CC1 [UL 813.738]'),
    (859_237_500, 'BandB-DL-CC2 [UL 814.238]'),
    (859_737_500, 'BandB-DL-CC3 [UL 814.738]'),
    (860_237_500, 'BandB-DL-CC4-primary [UL 815.238]'),
]

# ── P25 constants ─────────────────────────────────────────────────────────────
SYNC_HEX       = 0x5575F5FF7765
N_SYNC_DIBITS  = 24
SYNC_THRESH    = 0.75

_DB2SYM = {0b01: +3, 0b00: +1, 0b10: -1, 0b11: -3}
_SYM2DB = {v: k for k, v in _DB2SYM.items()}

# DFE: C4FM shaping causes ~31% ISI from the previous symbol at the eye center.
# Measured empirically: +3→-3 transition reads -1.12 → α = (3-1.12)/6 = 0.313.
# Last symbol of the sync word (0x5575F5FF7765) is +3; used to seed DFE at NID start.
_ALPHA_ISI      = 0.313
_SYNC_LAST_SYM  = +3.0

DUID_NAME = {
    0x0: 'HDU', 0x3: 'TDU', 0x5: 'LDU1', 0x7: 'TSBK',
    0xA: 'LDU2', 0xC: 'PDU', 0xF: 'TDULC',
}
# Payload dibits after NID, keyed by DUID; -1 = unknown/skip
DUID_PAY_DIBITS = {
    0x0: 288, 0x3: 0, 0x5: 432, 0x7: 48,
    0xA: 432, 0xC: 505, 0xF: 72,   # PDU: 505 raw air dibits (header + up to 4 data blocks, each 101 with status @ 14,50,86)
}

# OSP = Outbound (base→mobile), ISP = Inbound (mobile→base)
TSBK_OP = {
    # OSP opcodes (base station broadcasts on downlink / repeater)
    0x00: 'GRP_V_CH_GRNT',      # Group Voice Channel Grant
    0x02: 'GRP_V_CH_GRNT_UPD',  # Group Voice Channel Grant Update
    0x03: 'GRP_V_CH_GRNT_IMBE', # Group Voice Chan Grant w/ IMBE
    0x04: 'UU_V_CH_GRNT',       # Unit-to-Unit Voice Channel Grant
    0x05: 'UU_ANS_REQ',         # Unit-to-Unit Answer Request
    0x10: 'IDEN_UP',            # Identifier Update
    0x14: 'IDEN_UP_VU',         # Identifier Update VHF/UHF
    0x18: 'LOC_REG_RSP',        # Location Registration Response
    0x19: 'GRP_AFF_RSP',        # Group Affiliation Response
    0x1A: 'U_REG_RSP',          # Unit Registration Response
    0x1B: 'U_DEREG_ACK',        # Unit Deregister Acknowledge
    0x1C: 'QUEUED_RSP',         # Queued Response
    0x1D: 'DENY_RSP',           # Deny Response
    0x1E: 'SNDCP_CH_GRNT',      # SNDCP Data Channel Grant
    0x1F: 'SNDCP_CH_ANN',       # SNDCP Data Channel Announcement
    0x20: 'GRP_V_GRANT',        # Group Voice Grant (alt opcode)
    0x21: 'GRP_V_GRANT_UPD',    # Group Voice Grant Update
    0x24: 'UU_V_CH_GRNT_UPD',   # Unit-to-Unit Voice Grant Update
    0x28: 'GRP_V_GRANT_EXP',    # Group Voice Grant Explicit
    0x2C: 'LOC_REG_RSP_2',      # Location Reg Response (alt)
    0x2F: 'TIME_DATE_ANN',      # Time and Date Announcement
    0x34: 'GRP_V_GRANT_IMBE',   # Group Voice Grant IMBE
    0x38: 'GRP_AFF_Q',          # Group Affiliation Query
    0x39: 'SEC_GRP_V_CH_USER',  # Secondary Group Voice Chan User
    0x3A: 'NET_STS_BCAST',      # Network Status Broadcast
    0x3B: 'RFSS_STS_BCAST',     # RFSS Status Broadcast
    0x3C: 'ADJ_STS_BCAST',      # Adjacent Site Status Broadcast
    0x3D: 'ID_UPD',             # RFSS Identifier Update
    # ISP opcodes (mobile→base inbound signaling)
    0x40: 'GRP_V_CH_REQ',       # ISP: Group Voice Channel Request
    0x44: 'UU_V_CH_REQ',        # ISP: Unit-to-Unit Voice Chan Req
    0x45: 'UU_ANS_RSP',         # ISP: Unit-to-Unit Answer Response
    0x54: 'IDEN_UP_VU_REQ',     # ISP: IDEN Update Request
    0x56: 'GRP_AFF_REQ',        # ISP: Group Affiliation Request
    0x57: 'U_DEREG_REQ',        # ISP: Unit Deregister Request
    0x58: 'LOC_REG_REQ',        # ISP: Location Registration Request
    0x5A: 'U_REG_REQ',          # ISP: Unit Registration Request
    0x5B: 'AUTH_RESP',          # ISP: Authentication Response
    0x5C: 'AUTH_FNE_RSP',       # ISP: Authentication FNE Response
}

# ── FEC: BCH(63,16,11) for NID — ported from op25/trunk-recorder bch.cc (KA1RBI)
# GF(2^6), primitive poly x^6 + x + 1 → exp/log tables below.
_BCH_GFEXP = [
    1,  2,  4,  8, 16, 32,  3,  6, 12, 24, 48, 35,  5, 10, 20, 40,
   19, 38, 15, 30, 60, 59, 53, 41, 17, 34,  7, 14, 28, 56, 51, 37,
    9, 18, 36, 11, 22, 44, 27, 54, 47, 29, 58, 55, 45, 25, 50, 39,
   13, 26, 52, 43, 21, 42, 23, 46, 31, 62, 63, 61, 57, 49, 33,  0,
]
_BCH_GFLOG = [
   -1,  0,  1,  6,  2, 12,  7, 26,  3, 32, 13, 35,  8, 48, 27, 18,
    4, 24, 33, 16, 14, 52, 36, 54,  9, 45, 49, 38, 28, 41, 19, 56,
    5, 62, 25, 11, 34, 31, 17, 47, 15, 23, 53, 51, 37, 44, 55, 40,
   10, 61, 46, 30, 50, 22, 39, 43, 29, 60, 42, 21, 20, 59, 57, 58,
]

def _bch_decode(cw):
    """
    BCH(63,16,11) in-place decoder.  cw = mutable list of 63 ints (0 or 1).
    Returns: errors corrected (0-11), -1 (Chien fail), -2 (too many errors).
    Directly ported from KA1RBI's bch.cc used by op25 / trunk-recorder.

    IMPORTANT — correction capability:
    BCH(63,16,11): n=63, k=16, t=11 (the "11" is the correction capability, not d_min).
    The generator polynomial covers roots α^1..α^22 (22 consecutive roots via cyclotomic
    cosets of degree 6,6,6,6,3,6,6,6,2 = 47 total), giving BCH bound d* ≥ 23 and
    t = floor((23-1)/2) = 11.  The BM algorithm uses 22 syndromes S[1..22] and the
    error-locator polynomial has degree ≤ 11 for correctable codewords.

    The original bch.cc cap was L > 11 which is CORRECT.  An earlier incorrect
    comment changed it to L > 5 (based on misreading "11" as d_min rather than t).
    L > 11 is restored here.
    """
    GFe, GFl = _BCH_GFEXP, _BCH_GFLOG

    # ── Syndrome computation ────────────────────────────────────────────────────
    # P25 transmits info bits first (NAC MSB at cw[0]), so cw[j] is the coefficient
    # of x^(62-j) in the BCH polynomial — NOT x^j.  The correct evaluation is
    # c(α^i) = Σ cw[j] · α^(i·(62−j)).  Using i·j instead produces wrong syndromes
    # for all valid P25 NIDs and causes BM to always report L > 5 → −2.
    S = [0] * 23          # S[1..22] in log form; -1 means zero
    syn_error = False
    for i in range(1, 23):
        s = 0
        for j in range(63):
            if cw[j]:
                s ^= GFe[(i * (62 - j)) % 63]
        if s:
            syn_error = True
        S[i] = GFl[s]

    if not syn_error:
        return 0

    # ── Berlekamp-Massey ───────────────────────────────────────────────────────
    elp  = [[0] * 22 for _ in range(24)]
    D    = [0]  * 23   # discrepancies, log form (-1 = zero)
    L    = [0]  * 24   # LFSR lengths
    uLu  = [0]  * 24

    elp[0][0] = 0;  D[0] = -1; L[0] = 0; uLu[0] = -1
    elp[1][0] = 1;  D[1] = S[1]; L[1] = 0; uLu[1] = 0
    for i in range(1, 22):
        elp[0][i] = -1   # sentinel: "no term" / zero element
        elp[1][i] = 0

    U = 0
    while True:
        U += 1
        if D[U] == -1:                             # discrepancy is zero
            L[U+1] = L[U]
            for i in range(L[U] + 1):
                elp[U+1][i] = elp[U][i]
                v = elp[U][i]
                elp[U][i] = GFl[v] if 0 <= v < 64 else -1   # → log form
        else:
            # find best predecessor q (max uLu, D[q] != -1)
            q = U - 1
            while D[q] == -1 and q > 0:
                q -= 1
            if q > 0:
                j = q
                while j > 0:
                    j -= 1
                    if D[j] != -1 and uLu[q] < uLu[j]:
                        q = j

            L[U+1] = max(L[U], L[q] + U - q)

            for i in range(22):
                elp[U+1][i] = 0
            for i in range(L[q] + 1):
                if elp[q][i] != -1:
                    elp[U+1][i + U - q] ^= GFe[(D[U] + 63 - D[q] + elp[q][i]) % 63]
            for i in range(L[U] + 1):
                elp[U+1][i] ^= elp[U][i]
                v = elp[U][i]
                elp[U][i] = GFl[v] if 0 <= v < 64 else -1   # → log form

        uLu[U+1] = U - L[U+1]

        # next discrepancy (element form → log form)
        if U < 22:
            D[U+1] = GFe[S[U+1]] if S[U+1] != -1 else 0
            for i in range(1, L[U+1] + 1):
                if S[U+1-i] != -1 and elp[U+1][i] != 0:
                    D[U+1] ^= GFe[(S[U+1-i] + GFl[elp[U+1][i]]) % 63]
            D[U+1] = GFl[D[U+1]]

        if U >= 22 or L[U+1] > 11:
            break

    U += 1
    if L[U] > 11:
        return -2

    # convert elp[U] to log form for Chien search
    for i in range(L[U] + 1):
        v = elp[U][i]
        elp[U][i] = GFl[v] if 0 <= v < 64 else -1

    # ── Chien search ──────────────────────────────────────────────────────────
    reg  = [elp[U][i] for i in range(12)]
    locn = []
    for i in range(1, 64):
        q = 1
        for j in range(1, L[U] + 1):
            if reg[j] != -1:
                reg[j] = (reg[j] + j) % 63
                q ^= GFe[reg[j]]
        if q == 0:
            locn.append((i - 1) % 63)   # Chien evaluates σ(α^i); root at i → array pos i−1

    if len(locn) != L[U]:
        return -1   # Chien found wrong root count → uncorrectable

    # ── Correct bit errors ────────────────────────────────────────────────────
    for loc in locn:
        cw[loc] ^= 1

    return len(locn)


# ── FEC: CRC-CCITT-16 for TSBK / TDULC ───────────────────────────────────────
# Matches P25 standard: initial=0x0000, poly=0x1021, final XOR 0xFFFF.
# Operates on individual bits (not bytes), matching dsd-fme p25_crc.c.

def _p25_crc16_bits(bits):
    """CRC-CCITT over a sequence of bits (0/1 ints). Returns 16-bit integer."""
    crc = 0
    for b in bits:
        if ((crc >> 15) & 1) ^ (b & 1):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF

def _dibits_to_bits(dibits):
    """Convert list of dibits (0-3) to list of individual bits (MSB first per dibit)."""
    bits = []
    for d in dibits:
        d = int(d)
        bits.append((d >> 1) & 1)
        bits.append(d & 1)
    return bits

def _tsbk_crc_ok(dibits):
    """Validate TSBK/TDULC CRC-CCITT-16. Input: 48 dibits (96 bits). Returns bool."""
    bits = _dibits_to_bits(dibits[:48])
    if len(bits) < 96:
        return False
    computed = _p25_crc16_bits(bits[:80])
    received = sum(bits[80 + i] << (15 - i) for i in range(16))
    return computed == received


# ── FEC: Golay(23,12,7) for IMBE voice parameter protection ──────────────────
# Used in P25 LDU1/LDU2 IMBE voice frames (TIA-102.BABA).
# Generator polynomial: x^11 + x^9 + x^7 + x^6 + x^5 + x + 1 = 0xAE3
# Systematic form: 12 info bits (high) followed by 11 parity bits (low) = 23 bits.
# Corrects up to t = floor((7-1)/2) = 3 errors per codeword.

_GOLAY_GEN = 0xAE3  # x^11+x^9+x^7+x^6+x^5+x+1 = 1010 1110 0011

def _golay_parity(info_12):
    """Compute 11 parity bits for 12-bit info word (Golay systematic encoder)."""
    rem = info_12 << 11
    for i in range(11, -1, -1):
        if (rem >> (i + 11)) & 1:
            rem ^= (_GOLAY_GEN << i)
    return rem & 0x7FF

def _golay_syndrome(received_23):
    """Compute 11-bit syndrome for 23-bit received Golay codeword."""
    rem = received_23
    for i in range(11, -1, -1):
        if (rem >> (i + 11)) & 1:
            rem ^= (_GOLAY_GEN << i)
    return rem & 0x7FF

# Precompute syndrome for a codeword with a single error at each bit position (0=MSB)
_GOLAY_BIT_SYN = [_golay_syndrome(1 << (22 - j)) for j in range(23)]

def _golay_decode(bits_23):
    """
    Decode a 23-bit Golay(23,12,7) codeword.
    bits_23: list of 23 ints (MSB first) or a 23-bit integer (MSB = bit 22).
    Returns (info_12_as_int, errors_corrected) or (None, -1) if > 3 errors.
    """
    if isinstance(bits_23, (list, tuple)):
        cw = 0
        for b in bits_23[:23]:
            cw = (cw << 1) | (b & 1)
    else:
        cw = int(bits_23) & 0x7FFFFF

    syn = _golay_syndrome(cw)
    if syn == 0:
        return cw >> 11, 0

    w = bin(syn).count('1')
    if w <= 3:
        # All errors in parity positions (bits 0-10)
        return (cw ^ syn) >> 11, w

    # Try correcting each of 23 bit positions; check if residual weight ≤ 2
    for j in range(23):
        twisted = syn ^ _GOLAY_BIT_SYN[j]
        if bin(twisted).count('1') <= 2:
            # Error at bit j (from MSB) + errors in parity indicated by twisted
            corrected = cw ^ (1 << (22 - j)) ^ twisted
            return corrected >> 11, (1 + bin(twisted).count('1'))

    return None, -1  # uncorrectable (> 3 errors)


# ── FEC: Hamming(15,11,3) for IMBE voice parameter protection ─────────────────
# Used in P25 LDU1/LDU2 IMBE frames alongside Golay for lower-priority bits.
# Standard combinatorial Hamming(15,11): parity bits at 1-indexed positions 1,2,4,8.
# Corrects 1 error, detects 2 errors.

def _hamming_15_decode(bits_15):
    """
    Decode a 15-bit Hamming(15,11,3) codeword (standard combinatorial form).
    bits_15: list of 15 ints (position 0 = 1-indexed bit 1).
    Returns (errors_corrected,) — 0 or 1. Returns -1 on uncorrectable.
    Note: info bits are not extracted here since P25 IMBE layout requires
    the caller to map bit positions from the specific IMBE frame format.
    """
    if len(bits_15) < 15:
        return -1
    # Syndrome = XOR of 1-indexed positions of all set bits
    syn = 0
    for i in range(15):
        if bits_15[i]:
            syn ^= (i + 1)
    if syn == 0:
        return 0
    if 1 <= syn <= 15:
        return 1
    return -1  # multiple uncorrectable errors (shouldn't occur for ≤2 errors)


# ── FEC: P25 1/2-rate trellis decoder (p25_12) ────────────────────────────────
# Ported from dsd-fme p25_12.c (LWVMOBILE, 2023-10).
# Input:  98 valid channel dibits (status symbols already removed from air stream).
# Output: 12 decoded bytes  (or None on hard decoding failure).
#
# Algorithm:
#   1. Deinterleave 98 input dibits using P25 interleave table
#   2. Pack pairs of dibits into 49 nibbles; map through constellation table
#   3. Walk 4-state FSM; on mismatch use Hamming distance on DTM to pick best state
#   4. Repack 48 trellis dibits (out of 49; last is state flush) into 12 bytes

_P25_INTERLEAVE = [
     0,  1,  8,  9, 16, 17, 24, 25, 32, 33, 40, 41, 48, 49, 56, 57,
    64, 65, 72, 73, 80, 81, 88, 89, 96, 97,
     2,  3, 10, 11, 18, 19, 26, 27, 34, 35, 42, 43, 50, 51, 58, 59,
    66, 67, 74, 75, 82, 83, 90, 91,
     4,  5, 12, 13, 20, 21, 28, 29, 36, 37, 44, 45, 52, 53, 60, 61,
    68, 69, 76, 77, 84, 85, 92, 93,
     6,  7, 14, 15, 22, 23, 30, 31, 38, 39, 46, 47, 54, 55, 62, 63,
    70, 71, 78, 79, 86, 87, 94, 95,
]
_P25_CMAP = [11, 12,  0,  7, 14,  9,  5,  2, 10, 13,  1,  6, 15,  8,  4,  3]
_P25_FSM  = [ 0, 15, 12,  3,  4, 11,  8,  7, 13,  2,  1, 14,  9,  6,  5, 10]
_P25_DTM  = [ 2, 12,  1, 15, 14,  0, 13,  3,  9,  7, 10,  4,  5, 11,  6,  8]

def _p25_trellis_12(chan_dibits):
    """
    Decode 98 valid P25 channel dibits → 12 bytes via 1/2-rate trellis.
    Returns (list of 12 ints 0-255, hard_error_count).
    hard_error_count: number of FSM transitions that required hard-decision
    fallback (Hamming-nearest-neighbour substitution). Each substitution
    represents one or more dibit errors at that transition step.
    A hard_error_count of 0 means the trellis decoded without any fallback.
    """
    # 1. Deinterleave
    d = [0] * 98
    for i in range(98):
        d[_P25_INTERLEAVE[i]] = chan_dibits[i]

    # 2. Pack dibit pairs → nibbles → constellation points
    nibs   = [(d[i*2] << 2) | d[i*2+1] for i in range(49)]
    points = [_P25_CMAP[n] for n in nibs]

    # 3. FSM trellis walk
    state       = 0
    tdibits     = []
    hard_errors = 0
    for i in range(49):
        found = False
        for j in range(4):
            if _P25_FSM[state * 4 + j] == points[i]:
                tdibits.append(j)
                state = j
                found = True
                break
        if not found:
            # Hard error: pick transition with minimum Hamming distance
            hd   = [bin((nibs[i] ^ _P25_DTM[state * 4 + j]) & 0xF).count('1')
                    for j in range(4)]
            best = hd.index(min(hd))
            tdibits.append(best)
            state = best
            hard_errors += 1

    # 4. Repack 48 trellis dibits → 12 bytes (tdibit[48] = trailing state, unused)
    decoded = [
        (tdibits[i*4] << 6) | (tdibits[i*4+1] << 4) |
        (tdibits[i*4+2] << 2) | tdibits[i*4+3]
        for i in range(12)
    ]
    return decoded, hard_errors


# P25 PDU status symbol positions in the raw 101-dibit air stream.
# Status symbols appear every 36 symbols; skipdibit counter = 22 at PDU data start.
# First status: position 14 (22+14=36); subsequent: +36 each (50, 86).
_PDU_STATUS_IDX = frozenset([14, 50, 86])

def _extract_pre_fec_bits(pay_dibits, blks):
    """Pack raw pre-FEC data-block dibits as a hex string.

    For each of the `blks` data blocks, strip the 3 status symbols at positions
    14, 50, 86 within each 101-dibit block, then encode each of the remaining 98
    dibits as two MSB-first bits and pack contiguously.  The result matches the
    bv34 slice passed to block_deinterleave_34 in p25p1_fdma.cc, making the hex
    string directly replayable through that function.

    Returns '' if pay_dibits is too short.
    """
    needed = 101 * (1 + blks)
    if len(pay_dibits) < needed:
        return ''
    bits = []
    for bi in range(blks):
        block_start = 101 * (bi + 1)   # skip header block (first 101 dibits)
        for pos in range(101):
            if pos in _PDU_STATUS_IDX:
                continue
            d = pay_dibits[block_start + pos]
            bits.append((d >> 1) & 1)  # MSB of dibit
            bits.append(d & 1)         # LSB of dibit
    packed = bytearray()
    for i in range(0, len(bits), 8):
        byte = 0
        for j in range(min(8, len(bits) - i)):
            byte |= bits[i + j] << (7 - j)
        packed.append(byte)
    return packed.hex()


# SAP ID names (TIA-102.BAAC)
_SAP_NAME = {
    0: 'UserData', 1: 'EncUserData', 2: 'CircuitData', 3: 'CircuitDataCtl',
    4: 'PacketData', 5: 'ARP', 6: 'SNDCPCtl', 15: 'ScanPreamble',
    29: 'PktDataEncSupp', 31: 'ExtAddr', 32: 'RegAuth', 33: 'ChReassign',
    34: 'SysConfig', 35: 'MRLoopback', 36: 'MRStats', 37: 'MROutOfSvc',
    38: 'MRPaging', 39: 'MRConfig', 40: 'UnencKeyMgmt', 41: 'EncKeyMgmt',
    48: 'LocationSvc', 61: 'TrunkCtl', 63: 'EncTrunkCtl',
}

def decode_pdu_header(dibits):
    """
    Decode P25 PDU header block (DUID=0xC).

    Input: 101 raw air dibits (as read from FM-demodulated audio, including 3 status
    symbols at positions 14, 50, 86).

    Process:
      1. Strip 3 status symbols → 98 valid channel dibits
      2. p25_12 trellis decode → 12 decoded bytes
      3. CRC-CCITT-16 over bytes 0-9; validate against bytes 10-11
      4. Extract header fields

    Header byte layout (TIA-102.BAAC):
      byte[0]  : rsv(1) | AN(1) | IO(1) | FMT(5)
      byte[1]  : SAP(6)   [or class(2)|type(3)|status(3) when FMT=3 response]
      byte[2]  : MFID (0x00=standard, 0x90=Motorola, 0xA4=Harris, ...)
      byte[3-5]: LLID / address (24-bit; source if IO=1, dest if IO=0)
      byte[6]  : FMF(1) | BLKS(7)  — data blocks to follow
      byte[7]  : pad_octets(5) | rsv(3)
      byte[8]  : NS(3) | FSNF(4) | rsv(1)
      byte[9]  : rsv(2) | data_offset(6)
      byte[10-11]: CRC-CCITT-16

    Returns dict with at minimum crc_ok; richer fields when crc_ok=True.
    """
    if len(dibits) < 101:
        return {}

    # Strip status symbols at positions 14, 50, 86
    chan = [dibits[i] for i in range(101) if i not in _PDU_STATUS_IDX]

    raw, trellis_errs = _p25_trellis_12(chan)   # 12 decoded bytes + hard error count

    # CRC-CCITT-16 over bits 0-79 (bytes 0-9)
    hdr_bits = []
    for b in raw[:10]:
        for shift in range(7, -1, -1):
            hdr_bits.append((b >> shift) & 1)
    computed = _p25_crc16_bits(hdr_bits)          # includes final XOR 0xFFFF
    received = (raw[10] << 8) | raw[11]
    crc_ok   = (computed == received)

    r = {'crc_ok': crc_ok, 'mfr_id': raw[2], 'trellis_errs': trellis_errs}

    if not crc_ok:
        return r   # CRC failed — raw bytes are likely garbage; still stored in DB

    an   = (raw[0] >> 6) & 0x1
    io   = (raw[0] >> 5) & 0x1
    fmt  = raw[0] & 0x1F
    mfid = raw[2]
    addr = (raw[3] << 16) | (raw[4] << 8) | raw[5]
    blks = raw[6] & 0x7F

    r['pdu_fmt']  = fmt
    r['pdu_blks'] = blks
    r['mfr_id']   = mfid
    r['pre_fec_bits'] = _extract_pre_fec_bits(dibits, blks)

    if fmt == 3:
        # Response packet — class/type/status in byte[1], no SAP
        r['pdu_sap'] = None
    else:
        sap = raw[1] & 0x3F
        r['pdu_sap']     = sap
        r['pdu_sap_name'] = _SAP_NAME.get(sap, f'SAP_0x{sap:02X}')

    # IO=1: inbound (mobile→base), address = source unit ID
    # IO=0: outbound (base→mobile), address = destination
    if io:
        r['src_id'] = addr
    else:
        r['dest_tg'] = addr   # dest is a unit ID for PDU, not talk group per se

    return r

# ── FFT / channelizer geometry ─────────────────────────────────────────────────
# Constants that do NOT depend on sample rate (computed once):
_SPY        = int(round(AUDIO_RATE / 4800))         # samples per P25 symbol = 10
_N_SYNC_S   = N_SYNC_DIBITS * _SPY                  # sync template length = 240
_SYNC_NORM  = float(3 * N_SYNC_DIBITS * _SPY)       # normalisation factor = 720
_FM_SCALE   = float(AUDIO_RATE / (2.0 * np.pi * 600.0))  # radian→symbol scale ≈ 12.73
_DC         = CHUNK_SAMPLES // 2
_WIN        = np.hanning(CHUNK_SAMPLES).astype(np.float32)

# Sync template: 240-sample float32 array (±3 P25 symbols at 10 sps)
def _make_sync_template():
    syms = []
    for i in range(N_SYNC_DIBITS - 1, -1, -1):
        db = (SYNC_HEX >> (i * 2)) & 0b11
        syms.append(_DB2SYM[db])
    return np.repeat(np.array(syms, dtype=np.float32), _SPY)

_SYNC_TEMPL = _make_sync_template()


# Variables that DO depend on sample rate — initialised by _setup_geometry():
_BIN_HZ    = SAMPLE_RATE / CHUNK_SAMPLES
_N_OUT     = int(round(CHUNK_SAMPLES * AUDIO_RATE / SAMPLE_RATE))
_BINS_HALF = int(round(CHANNEL_BW_HZ / _BIN_HZ))
_CH_INFO   = []
_CHAN_WIN   = np.hanning(_N_OUT).astype(np.float32)

def _setup_geometry(rate):
    """
    Recompute all sample-rate-dependent globals in-place.
    Call once at startup and again whenever SAMPLE_RATE changes.
    Returns the new _CH_INFO list (caller must recreate Channel objects).
    """
    global SAMPLE_RATE, _BIN_HZ, _N_OUT, _BINS_HALF, _CH_INFO, _CHAN_WIN
    SAMPLE_RATE = rate
    _BIN_HZ    = rate / CHUNK_SAMPLES
    _N_OUT     = int(round(CHUNK_SAMPLES * AUDIO_RATE / rate))
    _BINS_HALF = int(round(CHANNEL_BW_HZ / _BIN_HZ))
    _CH_INFO   = []
    for freq_hz, label in CHANNELS:
        off   = freq_hz - CENTER_HZ
        c_bin = int(round(off / _BIN_HZ))
        _CH_INFO.append({'freq_hz': freq_hz, 'label': label,
                         'offset_hz': off, 'c_bin': c_bin,
                         'e_lo': _DC + c_bin - _BINS_HALF,
                         'e_hi': _DC + c_bin + _BINS_HALF + 1})
    # Full-width Hanning over all N_OUT bins keeps the complex baseband amplitude
    # well away from zero (prevents FM demod blow-ups at symbol transitions).
    _CHAN_WIN = np.hanning(_N_OUT).astype(np.float32)
    return _CH_INFO

# Initialise geometry at the starting rate
_setup_geometry(SAMPLE_RATE)

# ── Logging (stderr only — frames go to DB, no log file needed) ───────────────
def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())

def log(msg):
    print(f'{ts()}  {msg}', file=sys.stderr, flush=True)

# ── SQLite ─────────────────────────────────────────────────────────────────────
def init_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute('''
        CREATE TABLE IF NOT EXISTS dl_frames (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_unix      REAL    NOT NULL,
            ts_utc       TEXT    NOT NULL,
            freq_hz      INTEGER NOT NULL,
            label        TEXT    NOT NULL,
            nac          INTEGER,
            duid         INTEGER,
            duid_name    TEXT,
            raw_bytes    BLOB    NOT NULL,
            n_dibits     INTEGER NOT NULL,
            snr_db       REAL,
            src_id       INTEGER,
            dest_tg      INTEGER,
            opcode       INTEGER,
            opcode_name  TEXT,
            crc_ok       INTEGER,
            channel      INTEGER,
            wacn         INTEGER,
            sys_id       INTEGER,
            mfr_id       INTEGER,
            nid_bch_errs INTEGER,
            trellis_errs INTEGER,
            golay_errs   INTEGER,
            hamming_errs INTEGER
        )''')
    for idx in ('ts', 'freq', 'nac', 'duid', 'src', 'tg'):
        col = {'ts': 'ts_unix', 'freq': 'freq_hz', 'nac': 'nac',
               'duid': 'duid', 'src': 'src_id', 'tg': 'dest_tg'}[idx]
        con.execute(f'CREATE INDEX IF NOT EXISTS idx_{idx} ON dl_frames({col})')
    # Migrate older DBs that are missing new columns (SQLite ALTER TABLE IF NOT EXISTS unavailable)
    existing = {r[1] for r in con.execute("PRAGMA table_info(dl_frames)")}
    for col, defn in [('crc_ok',      'INTEGER'), ('channel',     'INTEGER'),
                      ('wacn',        'INTEGER'), ('sys_id',      'INTEGER'),
                      ('mfr_id',      'INTEGER'),
                      ('pdu_fmt',      'INTEGER'), ('pdu_sap',     'INTEGER'),
                      ('pdu_blks',     'INTEGER'),
                      ('pre_fec_bits', 'TEXT'),
                      ('nid_bch_errs', 'INTEGER'), ('trellis_errs','INTEGER'),
                      ('golay_errs',   'INTEGER'), ('hamming_errs','INTEGER')]:
        if col not in existing:
            con.execute(f'ALTER TABLE dl_frames ADD COLUMN {col} {defn}')
    con.commit()
    return con

_db_lock = threading.Lock()

def store_frame(con, freq_hz, label, nac, duid, raw_bytes, n_dibits,
                snr_db=None, src_id=None, dest_tg=None, opcode=None, opcode_name=None,
                crc_ok=None, channel=None, wacn=None, sys_id=None, mfr_id=None,
                pdu_fmt=None, pdu_sap=None, pdu_blks=None, pre_fec_bits=None,
                nid_bch_errs=None, trellis_errs=None,
                golay_errs=None, hamming_errs=None):
    """
    Store a decoded P25 frame to the database.

    FEC error count semantics:
      nid_bch_errs : BCH(63,16,11) NID corrections (0-5), -1 (Chien fail),
                     -2 (uncorrectable / >5 errors), or NULL (NID not decoded)
      trellis_errs : P25 1/2-rate trellis hard-decision substitutions (0-49) for PDU
      golay_errs   : Total Golay(23,12,7) corrections across all codewords in frame
      hamming_errs : Total Hamming(15,11,3) corrections across all codewords in frame
    """
    now = time.time()
    with _db_lock:
        con.execute('''INSERT INTO dl_frames
            (ts_unix,ts_utc,freq_hz,label,nac,duid,duid_name,
             raw_bytes,n_dibits,snr_db,src_id,dest_tg,opcode,opcode_name,
             crc_ok,channel,wacn,sys_id,mfr_id,pdu_fmt,pdu_sap,pdu_blks,
             pre_fec_bits,nid_bch_errs,trellis_errs,golay_errs,hamming_errs)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (now, ts(), freq_hz, label, nac, duid, DUID_NAME.get(duid, f'?{duid:X}'),
             raw_bytes, n_dibits, snr_db, src_id, dest_tg, opcode, opcode_name,
             int(crc_ok) if crc_ok is not None else None,
             channel, wacn, sys_id, mfr_id, pdu_fmt, pdu_sap, pdu_blks,
             pre_fec_bits or None,
             nid_bch_errs, trellis_errs, golay_errs, hamming_errs))
        con.commit()

# ── Frame utilities ────────────────────────────────────────────────────────────
def dibits_to_bytes(dibits):
    bits = []
    for d in dibits:
        bits.append((d >> 1) & 1)
        bits.append(d & 1)
    while len(bits) % 8:
        bits.append(0)
    out = bytearray()
    for i in range(0, len(bits), 8):
        b = 0
        for bit in bits[i:i+8]:
            b = (b << 1) | bit
        out.append(b)
    return bytes(out)

_NID_DIAG_DONE = set()   # channels already diagnosed; keyed by freq_hz

def _bch_nonzero_syndromes(cw):
    """Return count of non-zero syndromes S[1..22] for the given 63-bit codeword list."""
    GFe = _BCH_GFEXP
    count = 0
    for i in range(1, 23):
        s = 0
        for j in range(63):
            if cw[j]:
                s ^= GFe[(i * (62 - j)) % 63]
        if s:
            count += 1
    return count

def decode_nid(dibits, raw_fm=None, threshold=1.5, freq_hz=None):
    """
    Decode the 32-dibit (64-bit) P25 NID field using BCH(63,16,11) error correction.
    Bits 0-62: BCH codeword (16 info bits + 47 parity bits).
    Bit 63: inter-symbol status dibit (ignored per TIA-102.BAAA).
    Returns (nac, duid, bch_errs) tuple always; never None (caller must NAC-filter).

    BCH path (0-11 corrected errors): decoded NAC/DUID are reliable.
    Fallback path (> 5 errors or Chien fail): raw bits read from uncorrected cw.
    """
    if len(dibits) < 32:
        return None
    # Convert 32 dibits → 64 individual bits (MSB first within each dibit)
    bits = _dibits_to_bits(dibits[:32])
    cw   = bits[:63]        # mutable: BCH corrects this in place
    errs = _bch_decode(cw)
    if errs < 0:
        nac  = sum(bits[i] << (11 - i) for i in range(12))
        duid = sum(bits[12 + i] << (3 - i) for i in range(4))
        # Diagnostic: dump raw FM values on first BCH fail for expected NAC
        if freq_hz not in _NID_DIAG_DONE and nac == EXPECTED_NAC and raw_fm is not None:
            _NID_DIAG_DONE.add(freq_hz)
            nsyn = _bch_nonzero_syndromes(bits[:63])
            dc   = sum(raw_fm) / len(raw_fm)
            rng  = max(raw_fm) - min(raw_fm)
            vals_str = ' '.join(f'{v:+.2f}' for v in raw_fm)
            log(f'  NID-DIAG: NAC={nac:#05x} DUID={duid:#03x} BCH={errs}  thresh={threshold:.2f}')
            log(f'  NID-DIAG: {nsyn}/22 non-zero syndromes')
            log(f'  NID-DIAG: raw FM: dc={dc:+.3f} range={rng:.2f}')
            log(f'  NID-DIAG: raw FM values: {vals_str}')
        return nac, duid, errs   # bch_errs = -1 or -2 (uncorrectable)
    nac  = sum(cw[i] << (11 - i) for i in range(12))
    duid = sum(cw[12 + i] << (3 - i) for i in range(4))
    if errs > 0 and nac == EXPECTED_NAC:
        log(f'    BCH NID: corrected {errs} bit error(s)')
    return nac, duid, errs   # bch_errs = 0..5

def decode_tsbk_fields(dibits):
    """
    Decode 48-dibit (96-bit) TSBK block.
    Validates CRC-CCITT-16 over data bits 0-79 vs stored CRC bits 80-95.
    Returns dict with at minimum opcode/opcode_name/crc_ok; richer fields per opcode.
    """
    if len(dibits) < 48:
        return {}

    # Build byte array (raw[0] = first byte = bits 0-7, MSB first)
    bits_int = 0
    for d in dibits[:48]:
        bits_int = (bits_int << 2) | int(d)
    raw = [(bits_int >> (8 * i)) & 0xFF for i in range(11, -1, -1)]

    op      = raw[0] & 0x3F
    crc_ok  = _tsbk_crc_ok(dibits)
    r       = {
        'opcode':      op,
        'opcode_name': TSBK_OP.get(op, f'OP_0x{op:02X}'),
        'crc_ok':      crc_ok,
        'last_block':  bool(raw[0] >> 7),
        'mfr_id':      raw[1],
    }

    if not crc_ok:
        return r
    if raw[1] not in (0x00, 0x90):   # 0x00 = standard, 0x90 = Motorola
        return r

    if op in (0x00, 0x20):       # GRP_V_CH_GRNT / GRP_V_GRANT
        r['src_id']  = (raw[3] << 16) | (raw[4] << 8) | raw[5]
        r['dest_tg'] = (raw[6] << 8)  | raw[7]
        r['channel'] = ((raw[8] & 0xF) << 8) | raw[9]

    elif op == 0x02:             # GRP_V_CH_GRNT_UPD
        r['channel'] = ((raw[2] & 0xF) << 8) | raw[3]
        r['dest_tg'] = (raw[4] << 8) | raw[5]

    elif op in (0x04, 0x24):     # UU_V_CH_GRNT / UU_V_CH_GRNT_UPD
        r['src_id']  = (raw[3] << 16) | (raw[4] << 8) | raw[5]
        r['dest_tg'] = (raw[6] << 16) | (raw[7] << 8) | raw[8]
        r['channel'] = ((raw[2] & 0xF) << 8) | raw[9]

    elif op == 0x10:             # IDEN_UP (identifier update)
        r['iden_ch_id']  = (raw[2] >> 4) & 0xF
        r['iden_bw']     = raw[2] & 0xF
        r['iden_tx_off'] = (raw[3] << 8) | raw[4]
        r['iden_ch_num'] = ((raw[5] & 0x3) << 8) | raw[6]
        r['iden_base_f'] = (raw[7] << 8) | raw[8]

    elif op in (0x18, 0x2C):     # LOC_REG_RSP
        r['src_id']  = (raw[5] << 16) | (raw[6] << 8) | raw[7]

    elif op == 0x19:             # GRP_AFF_RSP
        r['dest_tg'] = (raw[5] << 8) | raw[6]
        r['src_id']  = (raw[6] << 16) | (raw[7] << 8) | raw[8]

    elif op == 0x1A:             # U_REG_RSP
        r['src_id']  = (raw[5] << 16) | (raw[6] << 8) | raw[7]

    elif op == 0x1B:             # U_DEREG_ACK
        r['src_id']  = (raw[5] << 16) | (raw[6] << 8) | raw[7]

    elif op == 0x21:             # GRP_V_GRANT_UPD
        r['channel'] = ((raw[2] & 0xF) << 8) | raw[3]
        r['dest_tg'] = (raw[4] << 8)  | raw[5]
        r['src_id']  = (raw[6] << 16) | (raw[7] << 8) | raw[8]

    elif op == 0x34:             # GRP_V_GRANT_IMBE
        r['src_id']  = (raw[4] << 16) | (raw[5] << 8) | raw[6]
        r['dest_tg'] = (raw[7] << 8)  | raw[8]

    elif op == 0x3A:             # NET_STS_BCAST
        wacn_hi   = (raw[2] << 12) | (raw[3] << 4) | (raw[4] >> 4)
        sys_id    = ((raw[4] & 0xF) << 8) | raw[5]
        r['wacn']    = wacn_hi
        r['sys_id']  = sys_id
        r['channel'] = ((raw[6] & 0xF) << 8) | raw[7]

    elif op == 0x3B:             # RFSS_STS_BCAST
        sys_id      = ((raw[4] & 0xF) << 8) | raw[5]
        r['wacn']   = (raw[2] << 12) | (raw[3] << 4) | (raw[4] >> 4)
        r['sys_id']  = sys_id
        r['rfss_id'] = raw[6]
        r['site_id'] = raw[7]
        r['channel'] = ((raw[8] & 0xF) << 8) | raw[9]

    elif op == 0x3C:             # ADJ_STS_BCAST
        r['adj_sys_id']  = ((raw[2] & 0xF) << 8) | raw[3]
        r['adj_rfss_id'] = raw[4]
        r['adj_site_id'] = raw[5]
        r['channel']     = ((raw[6] & 0xF) << 8) | raw[7]

    elif op == 0x3D:             # ID_UPD (RFSS ID update)
        r['wacn']   = (raw[2] << 12) | (raw[3] << 4) | (raw[4] >> 4)
        r['sys_id'] = ((raw[4] & 0xF) << 8) | raw[5]

    elif op == 0x40:             # ISP: GRP_V_CH_REQ
        r['src_id']  = (raw[3] << 16) | (raw[4] << 8) | raw[5]
        r['dest_tg'] = (raw[6] << 8)  | raw[7]

    elif op == 0x44:             # ISP: UU_V_CH_REQ
        r['src_id']  = (raw[3] << 16) | (raw[4] << 8) | raw[5]
        r['dest_tg'] = (raw[6] << 16) | (raw[7] << 8) | raw[8]

    elif op == 0x56:             # ISP: GRP_AFF_REQ
        r['dest_tg'] = (raw[4] << 8) | raw[5]
        r['src_id']  = (raw[6] << 16) | (raw[7] << 8) | raw[8]

    elif op == 0x57:             # ISP: U_DEREG_REQ
        r['src_id']  = (raw[5] << 16) | (raw[6] << 8) | raw[7]

    elif op == 0x58:             # ISP: LOC_REG_REQ
        r['src_id']  = (raw[5] << 16) | (raw[6] << 8) | raw[7]

    elif op == 0x5A:             # ISP: U_REG_REQ
        r['src_id']  = (raw[5] << 16) | (raw[6] << 8) | raw[7]

    return r

# ── P25 streaming frame decoder ────────────────────────────────────────────────
class P25Decoder:
    HUNT, NID, PAYLOAD = 'H', 'N', 'P'

    def __init__(self, freq_hz, label):
        self.freq_hz   = freq_hz
        self.label     = label
        self.buf       = np.zeros(0, dtype=np.float32)
        self.state     = self.HUNT
        self.nid_off   = 0
        self.pay_off   = 0
        self.nac       = None
        self.duid      = None
        self.pay_need  = 0
        self.threshold = 1.5
        self.n_frames  = 0
        self.bch_errs  = None   # BCH error count from most recent NID decode

    def push(self, fm):
        peak = float(np.percentile(np.abs(fm), 85)) if len(fm) else 0.0
        if peak > 0.01:
            self.threshold = peak * 0.5
        self.buf = np.concatenate([self.buf, fm])
        return self._run()

    def _run(self):
        frames = []
        MAX_HUNT = 1024
        while True:
            if self.state == self.HUNT:
                if len(self.buf) < _N_SYNC_S + _SPY:
                    break
                w = min(len(self.buf) - _N_SYNC_S, MAX_HUNT)
                if w <= 0:
                    break
                corr = np.correlate(self.buf[:w + _N_SYNC_S], _SYNC_TEMPL, mode='valid')[:w]
                corr /= _SYNC_NORM
                pi = int(np.argmax(corr))
                if corr[pi] >= SYNC_THRESH:
                    self.nid_off = pi + _N_SYNC_S
                    self.state   = self.NID
                    self.buf     = self.buf[pi:]
                    self.nid_off -= pi
                else:
                    keep = _N_SYNC_S - 1
                    self.buf = self.buf[max(0, len(self.buf) - keep):]
                    break

            elif self.state == self.NID:
                need = self.nid_off + 32 * _SPY
                if len(self.buf) < need:
                    break
                dibits = self._read(self.nid_off, 32)
                raw_fm = self._read_raw(self.nid_off, 32) if self.freq_hz not in _NID_DIAG_DONE else None
                res = decode_nid(dibits, raw_fm, self.threshold, self.freq_hz)
                if res:
                    nac, duid, bch_errs = res
                    if nac != EXPECTED_NAC:
                        self.buf     = self.buf[self.nid_off:]
                        self.nid_off = 0
                        self.state   = self.HUNT
                        continue
                    self.nac      = nac
                    self.duid     = duid
                    self.bch_errs = bch_errs
                    pay = DUID_PAY_DIBITS.get(duid, -1)
                    if pay < 0:
                        self.buf     = self.buf[need:]
                        self.nid_off = 0
                        self.state   = self.HUNT
                    elif pay == 0:
                        raw = dibits_to_bytes(dibits[:32])
                        frames.append(self._emit(dibits[:32], raw, bch_errs=bch_errs))
                        self.n_frames += 1
                        self.buf     = self.buf[need:]
                        self.nid_off = 0
                        self.state   = self.HUNT
                    else:
                        self.pay_need = pay
                        self.pay_off  = need
                        self.state    = self.PAYLOAD
                else:
                    self.buf     = self.buf[self.nid_off:]
                    self.nid_off = 0
                    self.state   = self.HUNT

            elif self.state == self.PAYLOAD:
                need = self.pay_off + self.pay_need * _SPY
                if len(self.buf) < need:
                    break
                nid_d  = self._read(self.nid_off, 32)
                pay_d  = self._read(self.pay_off, self.pay_need)
                all_d  = nid_d + pay_d
                raw    = dibits_to_bytes(all_d)
                frames.append(self._emit(all_d, raw, pay_d,
                                         bch_errs=getattr(self, 'bch_errs', None)))
                self.n_frames += 1
                self.buf     = self.buf[need:]
                self.nid_off = 0
                self.pay_off = 0
                self.state   = self.HUNT
        return frames

    def _emit(self, all_dibits, raw_bytes, pay_dibits=None, bch_errs=None):
        f = {'freq_hz': self.freq_hz, 'label': self.label,
             'nac': self.nac, 'duid': self.duid,
             'duid_name': DUID_NAME.get(self.duid, f'?{self.duid:X}'),
             'dibits': all_dibits, 'raw_bytes': raw_bytes,
             'n_dibits': len(all_dibits),
             'nid_bch_errs': bch_errs}
        if pay_dibits and self.duid == 0x7:    # TSBK
            fields = decode_tsbk_fields(pay_dibits)
            f.update(fields)
        elif pay_dibits and self.duid == 0xC:  # PDU — 1/2-rate trellis header decode
            fields = decode_pdu_header(pay_dibits)
            f.update(fields)   # includes trellis_errs
        elif pay_dibits and self.duid == 0xF:  # TDULC
            if len(pay_dibits) >= 36:
                lc_bits  = _dibits_to_bits(pay_dibits[:36])
                computed = _p25_crc16_bits(lc_bits[:56])
                received = sum(lc_bits[56 + i] << (15 - i) for i in range(16))
                f['crc_ok'] = (computed == received)
        return f

    def _read(self, offset, n, prev_q=_SYNC_LAST_SYM):
        mid = _SPY // 2  # = 5, eye center for C4FM
        # After DFE, symbol amplitudes compress from ±3/±1 to ±(1-α)×{3,1}.
        # Scale the threshold to match the equalized signal level.
        thresh = self.threshold * (1.0 - _ALPHA_ISI)
        dibits = []
        for i in range(n):
            idx = offset + i * _SPY + mid
            if idx < len(self.buf):
                s = float(self.buf[idx]) - _ALPHA_ISI * prev_q  # DFE: cancel prev-symbol ISI
                if   s >=  thresh: q = +3
                elif s >=  0.0:    q = +1
                elif s >= -thresh: q = -1
                else:              q = -3
                prev_q = float(q)
                dibits.append(_SYM2DB[q])
            else:
                prev_q = 0.0
                dibits.append(0)
        return dibits

    def _read_raw(self, offset, n):
        """Return list of n raw FM values at eye center (before DFE), for diagnostics."""
        mid = _SPY // 2  # = 5, matches _read()
        vals = []
        for i in range(n):
            idx = offset + i * _SPY + mid
            vals.append(float(self.buf[idx]) if idx < len(self.buf) else 0.0)
        return vals

# ── Per-channel state ──────────────────────────────────────────────────────────
class Channel:
    def __init__(self, info):
        self.freq_hz     = info['freq_hz']
        self.label       = info['label']
        self.c_bin       = info['c_bin']
        self.e_lo        = max(0, info['e_lo'])
        self.e_hi        = min(CHUNK_SAMPLES, info['e_hi'])
        self.noise_hist  = []
        self.noise_floor = None
        self.above_count = 0
        self.is_active   = False
        self.tail_left   = 0
        self.peak_snr    = 0.0
        self.prev_samp   = np.complex64(1.0)
        self.decoder     = P25Decoder(self.freq_hz, self.label)
        self.fm_sum      = 0.0
        self.fm_n        = 0

    def update_energy(self, psd):
        ch_pwr = float(np.mean(psd[self.e_lo:self.e_hi]))

        if len(self.noise_hist) < NOISE_WINDOW:
            self.noise_hist.append(ch_pwr)
        else:
            self.noise_hist.pop(0)
            self.noise_hist.append(ch_pwr)
        if len(self.noise_hist) >= 5:
            self.noise_floor = float(np.percentile(self.noise_hist, 30))

        if self.noise_floor is None:
            return ch_pwr, False

        above = ch_pwr > self.noise_floor + SQUELCH_DB
        if above:
            self.above_count += 1
            snr = ch_pwr - self.noise_floor
            if snr > self.peak_snr:
                self.peak_snr = snr
        else:
            self.above_count = 0

        confirmed = self.above_count >= MIN_ACTIVE

        if confirmed and not self.is_active:
            self.is_active = True
            self.tail_left = 0
            log(f'  CARRIER  {self.freq_hz/1e6:.4f} MHz  {self.label}'
                f'  floor={self.noise_floor:+.1f}dBfs  pwr={ch_pwr:+.1f}dBfs'
                f'  SNR={ch_pwr - self.noise_floor:.1f}dB')
        elif not confirmed and self.is_active:
            self.tail_left = TAIL_CHUNKS

        if self.tail_left > 0:
            self.tail_left -= 1
            if self.tail_left == 0:
                self.is_active = False
                log(f'  DONE     {self.freq_hz/1e6:.4f} MHz  '
                    f'{self.decoder.n_frames} frames  peak_SNR={self.peak_snr:.1f}dB')
                self.peak_snr = 0.0

        return ch_pwr, (self.is_active or self.tail_left > 0)

    def channelize(self, spectrum_shifted):
        """
        FFT/IFFT channelizer: extract _N_OUT bins centred on this channel,
        apply a Hanning window, IFFT → complex baseband at ~48 kSPS.
        FM demodulate → float32 audio.
        """
        half  = _N_OUT // 2
        lo    = _DC + self.c_bin - half
        hi    = lo + _N_OUT
        lo    = max(0, lo)
        hi    = min(CHUNK_SAMPLES, hi)
        n     = hi - lo

        ch_bins = spectrum_shifted[lo:hi].copy()
        if n < _N_OUT:
            ch_bins = np.pad(ch_bins, (0, _N_OUT - n))

        ch_bins *= _CHAN_WIN
        baseband = np.fft.ifft(np.fft.ifftshift(ch_bins)).astype(np.complex64)

        aug      = np.concatenate([[self.prev_samp], baseband])
        self.prev_samp = baseband[-1] if len(baseband) else self.prev_samp
        fm_audio = np.angle(aug[1:] * np.conj(aug[:-1])).astype(np.float32)
        fm_audio *= _FM_SCALE

        self.fm_sum += float(np.sum(fm_audio))
        self.fm_n   += len(fm_audio)

        return fm_audio

    def fm_dc_hz(self):
        """Return long-term FM mean in Hz (= carrier offset from channelizer center)."""
        if self.fm_n < 4800:
            return None
        return (self.fm_sum / self.fm_n) * 600.0  # symbol_units × 600 Hz/unit

# ── UHD direct-capture receive thread ─────────────────────────────────────────
_rx_queue         = queue.Queue(maxsize=RX_QUEUE_DEPTH)
_rx_running       = True
_overflow_ct      = 0             # total overflow events (informational)
_rate_step_event  = threading.Event()  # set by rx thread; cleared by main after reinit
_rx_rate_lock     = threading.Lock()
_rx_rate_idx      = 0             # current index into SAMPLE_RATES

def usrp_rx_thread():
    """Open B210 via UHD and stream IQ chunks to _rx_queue.

    Tracks UHD overflow events. When overflow rate exceeds OVERFLOW_THRESH within
    a OVERFLOW_WINDOW-chunk window, increments _rx_rate_idx and sets _rate_step_event
    so main() can reinitialise geometry at the next lower rate.
    """
    global _rx_running, _overflow_ct, _rx_rate_idx

    import uhd

    while _rx_running:
        with _rx_rate_lock:
            rate_idx = _rx_rate_idx
        rate = SAMPLE_RATES[rate_idx]

        try:
            log(f'USRP: opening B210  args="{USRP_ARGS}"  rate={rate/1e6:.3f} MSPS')
            usrp = uhd.usrp.MultiUSRP(USRP_ARGS)

            usrp.set_rx_rate(rate)
            actual_rate = usrp.get_rx_rate()
            log(f'USRP: rate  requested={rate/1e6:.3f}  actual={actual_rate/1e6:.6f} MSPS')

            tune_hz = CENTER_HZ + FREQ_CORRECTION_HZ
            usrp.set_rx_freq(uhd.types.TuneRequest(tune_hz))
            actual_freq = usrp.get_rx_freq()
            log(f'USRP: freq  requested={tune_hz/1e6:.4f} (center+{FREQ_CORRECTION_HZ}Hz corr)  actual={actual_freq/1e6:.6f} MHz')

            usrp.set_rx_gain(RX_GAIN)
            usrp.set_rx_antenna('RX2')
            log(f'USRP: gain={usrp.get_rx_gain():.1f} dB  antenna={usrp.get_rx_antenna()}')

            st_args          = uhd.usrp.StreamArgs('fc32', 'sc16')
            st_args.channels = [0]
            rx_streamer      = usrp.get_rx_stream(st_args)

            stream_cmd             = uhd.types.StreamCMD(uhd.types.StreamMode.start_cont)
            stream_cmd.stream_now  = True
            rx_streamer.issue_stream_cmd(stream_cmd)
            log(f'USRP: streaming started  max_samps_per_pkt={rx_streamer.get_max_num_samps()}')

            recv_buf    = np.zeros((1, CHUNK_SAMPLES), dtype=np.complex64)
            metadata    = uhd.types.RXMetadata()
            ovf_window  = 0   # overflow count in current evaluation window
            chunk_count = 0   # chunks processed in current window

            while _rx_running:
                # Yield if main() requested a rate change
                with _rx_rate_lock:
                    if _rx_rate_idx != rate_idx:
                        break

                num_samps = rx_streamer.recv(recv_buf, metadata, timeout=0.5)

                if metadata.error_code == uhd.types.RXMetadataErrorCode.overflow:
                    ovf_window  += 1
                    _overflow_ct += 1
                    log(f'USRP: OVERFLOW #{_overflow_ct} at {rate/1e6:.1f} MSPS'
                        f'  (window {ovf_window}/{OVERFLOW_WINDOW})')
                elif metadata.error_code != uhd.types.RXMetadataErrorCode.none:
                    log(f'USRP: RX error: {metadata.strerror()}')

                if num_samps > 0:
                    chunk = recv_buf[0, :num_samps].copy()
                    if num_samps < CHUNK_SAMPLES:
                        chunk = np.pad(chunk, (0, CHUNK_SAMPLES - num_samps))
                    try:
                        _rx_queue.put_nowait(chunk)
                    except queue.Full:
                        pass
                    chunk_count += 1

                if chunk_count >= OVERFLOW_WINDOW:
                    if ovf_window > OVERFLOW_THRESH:
                        with _rx_rate_lock:
                            next_idx = rate_idx + 1
                            if next_idx < len(SAMPLE_RATES):
                                log(f'USRP: {ovf_window} overflows/{OVERFLOW_WINDOW} chunks '
                                    f'at {rate/1e6:.1f} MSPS → stepping to '
                                    f'{SAMPLE_RATES[next_idx]/1e6:.1f} MSPS')
                                _rx_rate_idx = next_idx
                                _rate_step_event.set()
                            else:
                                log(f'USRP: {ovf_window} overflows at minimum rate '
                                    f'{rate/1e6:.1f} MSPS — continuing anyway')
                    else:
                        log(f'USRP: STABLE  {ovf_window} overflows in {OVERFLOW_WINDOW} chunks'
                            f'  rate={rate/1e6:.1f} MSPS')
                    ovf_window  = 0
                    chunk_count = 0

            # Stop stream before reinit or shutdown
            stop_cmd = uhd.types.StreamCMD(uhd.types.StreamMode.stop_cont)
            rx_streamer.issue_stream_cmd(stop_cmd)
            log('USRP: stream stopped')

        except Exception as e:
            log(f'USRP: error: {e}  — retry in 3s')
            time.sleep(3)

# ── Signal handler ─────────────────────────────────────────────────────────────
running = True
def handle_sig(sig, frame):
    global running
    running = False
signal.signal(signal.SIGTERM, handle_sig)
signal.signal(signal.SIGINT,  handle_sig)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    global _rx_running

    log(f'=== P25 DL Frame Capture (UHD bare-metal USB2) start — FEC VALIDATION RUN ===')
    log(f'  B210   : USRP_ARGS="{USRP_ARGS}"')
    log(f'  Rates  : {[r/1e6 for r in SAMPLE_RATES]} MSPS  (starting at {SAMPLE_RATE/1e6:.1f} MSPS)')
    log(f'  Center : {CENTER_HZ/1e6:.1f} MHz   Chunk: {CHUNK_SAMPLES} samples')
    log(f'  FFT bin: {_BIN_HZ:.1f} Hz  N_OUT: {_N_OUT}  SPY: {_SPY}  NAC: {EXPECTED_NAC:#05x}')
    log(f'  Expect: BCH=0 on all NIDs, CRC=OK on all TSBKs (tower TX quality)')
    for info in _CH_INFO:
        log(f'  {info["freq_hz"]/1e6:.4f} MHz  offset={info["offset_hz"]/1e6:+.4f} MHz  '
            f'{info["label"]}')

    con = init_db(DB_PATH)
    log(f'Database: {DB_PATH}')

    rxt = threading.Thread(target=usrp_rx_thread, daemon=True, name='usrp_rx')
    rxt.start()
    log('USRP RX thread started')

    channels       = [Channel(info) for info in _CH_INFO]
    total_chunks   = 0
    total_frames   = 0
    warmup_done    = False
    adc_peak_warmup = 0.0   # max |sample| seen during warmup (0..1 relative to ADC full scale)
    # Running FEC error totals for STATUS reporting and BER estimation
    fec_bch_errs   = 0
    fec_bch_frames = 0
    fec_bch_fail   = 0
    fec_bch_clean  = 0
    fec_trl_errs   = 0
    fec_trl_frames = 0
    fec_nid_total  = 0
    fec_pdu_total  = 0

    log(f'Warming up ({WARMUP_CHUNKS} chunks ≈ '
        f'{WARMUP_CHUNKS * CHUNK_SAMPLES / SAMPLE_RATE:.1f}s)...')

    while running:
        # ── Rate step-down handling ──────────────────────────────────────────
        if _rate_step_event.is_set():
            with _rx_rate_lock:
                new_idx  = _rx_rate_idx
            new_rate = SAMPLE_RATES[new_idx]
            log(f'RATE CHANGE: reinitialising geometry at {new_rate/1e6:.1f} MSPS')
            _setup_geometry(new_rate)
            # Flush stale chunks from previous rate
            while not _rx_queue.empty():
                try: _rx_queue.get_nowait()
                except queue.Empty: break
            channels      = [Channel(info) for info in _CH_INFO]
            total_chunks  = 0
            warmup_done   = False
            _rate_step_event.clear()
            log(f'RATE CHANGE: active at {SAMPLE_RATE/1e6:.1f} MSPS  '
                f'bin={_BIN_HZ:.1f} Hz  N_OUT={_N_OUT}')

        try:
            chunk = _rx_queue.get(timeout=2.0)
        except queue.Empty:
            continue

        total_chunks += 1

        spectrum = np.fft.fftshift(np.fft.fft(chunk * _WIN, n=CHUNK_SAMPLES))
        psd      = 20.0 * np.log10(np.abs(spectrum) / CHUNK_SAMPLES + 1e-12)

        if not warmup_done:
            adc_peak_warmup = max(adc_peak_warmup, float(np.max(np.abs(chunk))))
            for ch in channels:
                ch.update_energy(psd)
            if total_chunks >= WARMUP_CHUNKS:
                warmup_done = True
                nf_str = '  '.join(
                    f'{ch.freq_hz/1e6:.4f}={ch.noise_floor:+.1f}dBfs'
                    for ch in channels if ch.noise_floor is not None
                )
                log(f'WARMUP COMPLETE  noise floors: {nf_str}')
                # ADC utilisation + per-channel peak-bin power
                adc_headroom_db = -20.0 * np.log10(adc_peak_warmup + 1e-12)
                log(f'  ADC: peak={adc_peak_warmup:.4f} ({20*np.log10(adc_peak_warmup+1e-12):+.1f} dBfs)'
                    f'  headroom={adc_headroom_db:.1f} dB  gain={RX_GAIN:.0f} dB')
                for ch in channels:
                    info = next(i for i in _CH_INFO if i['freq_hz'] == ch.freq_hz)
                    peak_bin_db = float(np.max(psd[info['e_lo']:info['e_hi']]))
                    mean_bin_db = float(ch.noise_floor) if ch.noise_floor else 0.0
                    log(f'  SIG: {ch.freq_hz/1e6:.4f} MHz'
                        f'  peak_bin={peak_bin_db:+.1f} dBfs'
                        f'  mean_ch={mean_bin_db:+.1f} dBfs'
                        f'  ({ch.label})')
            continue

        for ch in channels:
            _, active = ch.update_energy(psd)
            if not active:
                continue

            fm_audio = ch.channelize(spectrum)

            # One-shot FM amplitude diagnostic for CC4 on first active chunk
            if total_chunks == WARMUP_CHUNKS + 1 and ch.freq_hz == 860_237_500:
                fm_std  = float(np.std(fm_audio))
                fm_mean = float(np.mean(fm_audio))
                fm_p85  = float(np.percentile(np.abs(fm_audio), 85))
                adc_now = float(np.max(np.abs(chunk)))
                log(f'  FM-AMP CC4: mean={fm_mean:+.3f} std={fm_std:.3f}'
                    f' p85={fm_p85:.3f}  (ideal std≈2.0, p85≈3.0 for C4FM)'
                    f'  adc_peak={adc_now:.4f} ({20*np.log10(adc_now+1e-12):+.1f} dBfs)')

            frames   = ch.decoder.push(fm_audio)

            for fr in frames:
                total_frames += 1
                src  = fr.get('src_id',  '')
                dst  = fr.get('dest_tg', '')
                op   = fr.get('opcode_name', '')
                crc  = fr.get('crc_ok')
                ch_n = fr.get('channel', '')
                wacn = fr.get('wacn', '')
                sys  = fr.get('sys_id', '')
                sap  = fr.get('pdu_sap_name', '')
                bch  = fr.get('nid_bch_errs')
                trl  = fr.get('trellis_errs')

                # Accumulate FEC stats
                fec_nid_total += 1
                if bch is None or bch == 0:
                    fec_bch_clean  += 1
                elif bch > 0:
                    fec_bch_errs   += bch
                    fec_bch_frames += 1
                else:  # bch < 0: -1 Chien fail, -2 uncorrectable
                    fec_bch_fail   += 1
                if fr['duid'] == 0xC:   # PDU
                    fec_pdu_total += 1
                    if trl is not None and trl > 0:
                        fec_trl_errs   += trl
                        fec_trl_frames += 1

                crc_tag = ('' if crc is None
                           else '  CRC=OK' if crc
                           else '  CRC=BAD')
                bch_tag = (f'  BCH={bch}err' if (bch is not None and bch > 0)
                           else f'  BCH=FAIL({bch})' if (bch is not None and bch < 0)
                           else '')
                trl_tag = (f'  TRL={trl}hard' if (trl is not None and trl > 0) else '')
                log(f'  FRAME  {fr["freq_hz"]/1e6:.4f} MHz  '
                    f'NAC={fr["nac"]:03X}  {fr["duid_name"]}'
                    + (f'  {op}' if op else '')
                    + (f'  SAP={sap}' if sap else '')
                    + crc_tag
                    + bch_tag
                    + trl_tag
                    + (f'  src={src}' if src else '')
                    + (f'  dst={dst}' if dst else '')
                    + (f'  ch={ch_n:#05x}' if ch_n else '')
                    + (f'  WACN={wacn:#07x}' if wacn else '')
                    + (f'  SYS={sys:#05x}' if sys else ''))
                store_frame(
                    con,
                    freq_hz      = fr['freq_hz'],
                    label        = fr['label'],
                    nac          = fr['nac'],
                    duid         = fr['duid'],
                    raw_bytes    = fr['raw_bytes'],
                    n_dibits     = fr['n_dibits'],
                    snr_db       = ch.peak_snr if ch.peak_snr > 0 else None,
                    src_id       = fr.get('src_id'),
                    dest_tg      = fr.get('dest_tg'),
                    opcode       = fr.get('opcode'),
                    opcode_name  = fr.get('opcode_name'),
                    crc_ok       = fr.get('crc_ok'),
                    channel      = fr.get('channel'),
                    wacn         = fr.get('wacn'),
                    sys_id       = fr.get('sys_id'),
                    mfr_id       = fr.get('mfr_id'),
                    pdu_fmt      = fr.get('pdu_fmt'),
                    pdu_sap      = fr.get('pdu_sap'),
                    pdu_blks     = fr.get('pdu_blks'),
                    pre_fec_bits = fr.get('pre_fec_bits'),
                    nid_bch_errs = bch,
                    trellis_errs = trl,
                )

        if total_chunks % 1000 == 0:
            mins = total_chunks * CHUNK_SAMPLES / SAMPLE_RATE / 60
            act  = ', '.join(f'{ch.freq_hz/1e6:.4f}'
                             for ch in channels if ch.is_active) or 'none'
            nid_str = ''
            if fec_nid_total > 0:
                bch_ber = fec_bch_errs / max(fec_bch_frames * 63, 1)
                nid_str = (f'  BCH: {fec_bch_clean}clean'
                           f'/{fec_bch_frames}corr(BER≈{bch_ber:.1e})'
                           f'/{fec_bch_fail}fail'
                           f'  ({100*fec_bch_fail//fec_nid_total}%uncorr)')
            trl_str = ''
            if fec_pdu_total > 0:
                trl_ber = fec_trl_errs / (fec_pdu_total * 49)
                trl_str = f'  TRL≈{trl_ber:.2e}({fec_trl_errs}hard/{fec_pdu_total}pdu)'
            fm_dc_parts = []
            for ch in channels:
                dc_hz = ch.fm_dc_hz()
                if dc_hz is not None:
                    fm_dc_parts.append(f'{ch.freq_hz/1e6:.4f}={dc_hz:+.0f}Hz')
            fm_dc_str = '  FM_DC: ' + ', '.join(fm_dc_parts) if fm_dc_parts else ''
            log(f'STATUS  {mins:.1f} min  chunks={total_chunks}  '
                f'frames={total_frames}  overflows={_overflow_ct}'
                f'  rate={SAMPLE_RATE/1e6:.1f}MSPS'
                + nid_str + trl_str
                + f'  active={act}'
                + fm_dc_str)

    _rx_running = False
    con.close()
    log(f'=== Stopped  chunks={total_chunks}  frames={total_frames}  '
        f'final_rate={SAMPLE_RATE/1e6:.1f}MSPS ===')

if __name__ == '__main__':
    main()
