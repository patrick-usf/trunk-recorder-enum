#!/usr/bin/env python3
"""
P25 Uplink Frame Capture — TCP IQ stream from Windows-hosted B210
Howard County MD P25 Phase 1 — Band A + Band B uplinks

Connects to b210_server.py running on the Windows host, which opens the B210
natively (no VMware xHCI emulation) and streams raw complex64 IQ over TCP.
Eliminates the ~10% overflow rate caused by VMware USB passthrough.

Coverage (center 810 MHz, 12 MSPS, ±6 MHz):
  Band A UL:  806.5750, 807.0375, 807.6375, 808.0625 MHz
  Band B UL:  813.7375, 814.2375, 814.7375, 815.2375 MHz

Architecture:
  Thread 1 (tcp_rx_thread) — TCP recv from Windows host b210_server.py;
                              pushes raw complex64 chunks to queue.
  Thread 2 (main)          — pulls chunks; per-chunk:
    1. FFT  → energy detection / noise floor (all 8 channels in one pass)
    2. FFT/IFFT channelizer → baseband audio per active channel
    3. FM demodulation → float32 audio @ ~48 kSPS
    4. P25 sync correlation (threshold 0.75) + NAC filter (0x842)
    5. Frame byte extraction → SQLite

Setup:
  1. Windows host:  python b210_server.py          (opens B210 natively; listens on 0.0.0.0:9876)
  2. Linux VM:      nohup python3 ul_frame_capture.py > /dev/null 2>&1 &
                    (connects to 192.168.42.1:9876 via VMXNET3/vmnet1 — ~90 MB/s path)

Outputs:
  /home/sdr/P25/ul_frames.db   SQLite frame database (all frames + decoded fields)
  stderr                        Operational log (use nohup + redirect or journalctl)
"""

import os, sys, time, signal, sqlite3, threading, queue, socket, struct
import numpy as np

# ── Configuration ─────────────────────────────────────────────────────────────
CENTER_HZ      = 811_400_000    # ±4 MHz covers 807.4–815.4 MHz: all 4 CCs + 807.6/808.1 MHz voice channels
SAMPLE_RATE    =  8_000_000    # 8 MSPS — stable on Windows hybrid-CPU scheduler (32 MB/s sc16 on USB)
RX_GAIN        = 40.0             # set on server side; noted here for reference
CHUNK_SAMPLES  = 131_072          # ~10.9 ms per chunk; must match b210_server.py
AUDIO_RATE     = 48_000           # target audio rate after channelization
CHANNEL_BW_HZ  = 6_250            # ±Hz around channel centre (P25 = 12.5 kHz ch)
SQUELCH_DB     = 10.0
MIN_ACTIVE     = 2                # consecutive above-squelch chunks to confirm
NOISE_WINDOW   = 60
WARMUP_CHUNKS  = 120              # ~1.3 s warmup before squelch decisions
TAIL_CHUNKS    = 30               # chunks to keep decoding after squelch drops

EXPECTED_NAC   = 0x842            # Howard County
DB_PATH        = '/home/sdr/P25/ul_frames.db'
RX_QUEUE_DEPTH = 32               # chunks in flight between tcp_thread and main

# TCP source: b210_server.py running on Windows host (192.168.1.17)
# Override with env var B210_SERVER=host:port
_B210_SERVER   = os.environ.get('B210_SERVER', '192.168.42.1:9876')  # vmnet1 VMXNET3 path
_SERVER_HOST, _SERVER_PORT = _B210_SERVER.rsplit(':', 1)
_SERVER_PORT   = int(_SERVER_PORT)

# Wire protocol (matches b210_server.py)
_MAGIC         = 0xB210
_HDR_FMT       = '<HI'           # magic(uint16) + n_samples(uint32)
_HDR_SIZE      = struct.calcsize(_HDR_FMT)
_CHUNK_BYTES   = CHUNK_SAMPLES * 8  # complex64 = 8 bytes/sample

# ── Channels ──────────────────────────────────────────────────────────────────
CHANNELS = [
    (806_575_000, 'BandA-UL1 [DL 851.575 TG10963 Fire/EMS]'),
    (807_037_500, 'BandA-UL2 [DL 852.038 traffic]'),
    (807_637_500, 'BandA-UL3 [DL 852.638 traffic]'),
    (808_062_500, 'BandA-UL4 [DL 853.063 TG11041 Enc]'),
    (813_737_500, 'BandB-UL1 [DL 858.738 CC1]'),
    (814_237_500, 'BandB-UL2 [DL 859.238 CC2]'),
    (814_737_500, 'BandB-UL3 [DL 859.738 CC3]'),
    (815_237_500, 'BandB-UL4 [DL 860.238 CC4-primary]'),
]

# ── P25 constants ─────────────────────────────────────────────────────────────
SYNC_HEX       = 0x5575F5FF7765
N_SYNC_DIBITS  = 24
SYNC_THRESH    = 0.75

_DB2SYM = {0b01: +3, 0b00: +1, 0b10: -1, 0b11: -3}
_SYM2DB = {v: k for k, v in _DB2SYM.items()}

DUID_NAME = {
    0x0: 'HDU', 0x3: 'TDU', 0x5: 'LDU1', 0x7: 'TSBK',
    0xA: 'LDU2', 0xC: 'PDU', 0xF: 'TDULC',
}
# Payload dibits after NID, keyed by DUID; -1 = unknown/skip
DUID_PAY_DIBITS = {
    0x0: 288, 0x3: 0, 0x5: 432, 0x7: 48,
    0xA: 432, 0xC: 101, 0xF: 72,   # PDU: 101 raw air dibits (98 channel + 3 status @ pos 14,50,86)
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
    Returns: errors corrected (0-5), -1 (Chien fail), -2 (too many errors).
    Directly ported from KA1RBI's bch.cc used by op25 / trunk-recorder.

    IMPORTANT — correction capability cap:
    BCH(63,16,11) has minimum distance d_min=11, so it guarantees correction of
    at most  t = floor((d_min - 1) / 2) = 5  errors per codeword.

    The original bch.cc checks L > 11 (the degree of the generator polynomial),
    but this is WRONG for the correction guarantee.  Berlekamp-Massey will find
    an error-locator polynomial of degree 6-11 for codewords with ≥ 6 genuine
    errors AND for codewords that are pure noise (false sync hits).  When that
    polynomial happens to have the right number of roots in GF(2^6), the Chien
    search "succeeds" and we flip up to 11 bits — almost certainly producing a
    DIFFERENT wrong codeword, not the transmitted one.

    Real-world symptom: every P25 sync correlation hit on noise generates a
    "corrected 10/11 bit error(s)" log message (14 000+ per hour observed) and
    feeds corrupted NAC/DUID bits into the frame decoder.  The NAC filter
    (EXPECTED_NAC == 0x842) catches most of these, but the log spam masks
    genuine correction events and the corrupted decode path is unsafe.

    Fix: cap BM at t=5.  Anything the algorithm identifies as needing > 5
    corrections is returned as -2 (uncorrectable) and handled by the fallback
    raw read in decode_nid(), which the NAC filter then rejects with p=4095/4096.
    """
    GFe, GFl = _BCH_GFEXP, _BCH_GFLOG

    # ── Syndrome computation ────────────────────────────────────────────────────
    S = [0] * 23          # S[1..22] in log form; -1 means zero
    syn_error = False
    for i in range(1, 23):
        s = 0
        for j in range(63):
            if cw[j]:
                s ^= GFe[(i * j) % 63]
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

        if U >= 22 or L[U+1] > 5:    # cap at t=5; was > 11 (see docstring)
            break

    U += 1
    if L[U] > 5:                  # cap at t=5; was > 11 (see docstring)
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
            locn.append(63 - i)

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
_BIN_HZ     = SAMPLE_RATE / CHUNK_SAMPLES          # Hz per FFT bin (~91.55 Hz)
_N_OUT      = int(round(CHUNK_SAMPLES * AUDIO_RATE / SAMPLE_RATE))  # 524 samples out
_SPY        = int(round(AUDIO_RATE / 4800))        # samples per P25 symbol ≈ 10
_N_SYNC_S   = N_SYNC_DIBITS * _SPY                 # sync template length in samples (240)
_SYNC_NORM  = float(3 * N_SYNC_DIBITS * _SPY)

# FM-to-symbol scale: np.angle() returns radians/sample; we need P25 symbol units
# where ±600 Hz → ±1 and ±1800 Hz → ±3.
# scale = 3 / (2π × 1800 Hz / AUDIO_RATE) = AUDIO_RATE / (2π × 600)
_FM_SCALE   = float(AUDIO_RATE / (2.0 * np.pi * 600.0))   # ≈ 12.73
_DC         = CHUNK_SAMPLES // 2
_BINS_HALF  = int(round(CHANNEL_BW_HZ / _BIN_HZ)) # ±bins for energy detection
_WIN        = np.hanning(CHUNK_SAMPLES).astype(np.float32)

# Per-channel: centre bin (signed offset from DC), energy detection range
_CH_INFO = []
for freq_hz, label in CHANNELS:
    off   = freq_hz - CENTER_HZ
    c_bin = int(round(off / _BIN_HZ))
    _CH_INFO.append({'freq_hz': freq_hz, 'label': label,
                     'offset_hz': off, 'c_bin': c_bin,
                     'e_lo': _DC + c_bin - _BINS_HALF,
                     'e_hi': _DC + c_bin + _BINS_HALF + 1})

# Sync template: 240-sample float32 array (±3 symbols at 10 sps)
def _make_sync_template():
    syms = []
    for i in range(N_SYNC_DIBITS - 1, -1, -1):
        db = (SYNC_HEX >> (i * 2)) & 0b11
        syms.append(_DB2SYM[db])
    return np.repeat(np.array(syms, dtype=np.float32), _SPY)

_SYNC_TEMPL = _make_sync_template()

# Frequency-domain Hanning window for the channelizer IFFT slice
_CHAN_WIN = np.hanning(_N_OUT).astype(np.float32)

# ── Logging (stderr only — frames go to DB, no log file needed) ───────────────
def ts():
    return time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())

def log(msg):
    print(f'{ts()}  {msg}', file=sys.stderr, flush=True)

# ── SQLite ─────────────────────────────────────────────────────────────────────
def init_db(path):
    con = sqlite3.connect(path, check_same_thread=False)
    con.execute('''
        CREATE TABLE IF NOT EXISTS ul_frames (
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
        con.execute(f'CREATE INDEX IF NOT EXISTS idx_{idx} ON ul_frames({col})')
    # Migrate older DBs that are missing new columns (SQLite ALTER TABLE IF NOT EXISTS unavailable)
    existing = {r[1] for r in con.execute("PRAGMA table_info(ul_frames)")}
    for col, defn in [('crc_ok',      'INTEGER'), ('channel',     'INTEGER'),
                      ('wacn',        'INTEGER'), ('sys_id',      'INTEGER'),
                      ('mfr_id',      'INTEGER'),
                      ('pdu_fmt',     'INTEGER'), ('pdu_sap',     'INTEGER'),
                      ('pdu_blks',    'INTEGER'),
                      ('nid_bch_errs','INTEGER'), ('trellis_errs','INTEGER'),
                      ('golay_errs',  'INTEGER'), ('hamming_errs','INTEGER')]:
        if col not in existing:
            con.execute(f'ALTER TABLE ul_frames ADD COLUMN {col} {defn}')
    con.commit()
    return con

_db_lock = threading.Lock()

def store_frame(con, freq_hz, label, nac, duid, raw_bytes, n_dibits,
                snr_db=None, src_id=None, dest_tg=None, opcode=None, opcode_name=None,
                crc_ok=None, channel=None, wacn=None, sys_id=None, mfr_id=None,
                pdu_fmt=None, pdu_sap=None, pdu_blks=None,
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
        con.execute('''INSERT INTO ul_frames
            (ts_unix,ts_utc,freq_hz,label,nac,duid,duid_name,
             raw_bytes,n_dibits,snr_db,src_id,dest_tg,opcode,opcode_name,
             crc_ok,channel,wacn,sys_id,mfr_id,pdu_fmt,pdu_sap,pdu_blks,
             nid_bch_errs,trellis_errs,golay_errs,hamming_errs)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
            (now, ts(), freq_hz, label, nac, duid, DUID_NAME.get(duid, f'?{duid:X}'),
             raw_bytes, n_dibits, snr_db, src_id, dest_tg, opcode, opcode_name,
             int(crc_ok) if crc_ok is not None else None,
             channel, wacn, sys_id, mfr_id, pdu_fmt, pdu_sap, pdu_blks,
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

def decode_nid(dibits):
    """
    Decode the 32-dibit (64-bit) P25 NID field using BCH(63,16,11) error correction.
    Bits 0-62: BCH codeword (16 info bits + 47 parity bits).
    Bit 63: inter-symbol status dibit (ignored per TIA-102.BAAA).
    Returns (nac, duid) tuple always; never None (caller must NAC-filter).

    BCH path (0-5 corrected errors): decoded NAC/DUID are reliable.
    Fallback path (> 5 errors or Chien fail): raw bits read from uncorrected cw.

    FALSE SYNC RISK — why the fallback exists and why it is safe:
        The P25 sync correlator fires at threshold 0.75, which noise can satisfy
        with low but non-zero probability (~1 hit per few thousand chunks at 12 MSPS).
        When the correlator fires on noise, the 64 NID bits are effectively random,
        BCH returns -2 (uncorrectable), and we fall back to reading the raw bits.
        The raw NAC from noise is uniformly distributed over [0, 4095]; the
        probability of accidentally matching EXPECTED_NAC (0x842) is 1/4096 ≈ 0.024%.
        The NAC filter in _run() discards the frame silently, so false syncs impose
        only a tiny log-free overhead.  No frame-storage risk.

        Before the _bch_decode cap was lowered from 11 to 5, BM would "find" a
        degree-6 to degree-11 error-locator polynomial for these noisy codewords,
        Chien search would confirm its (false) roots, and the decoder would flip
        up to 11 bits — producing a corrupted codeword logged as
        "corrected 10/11 bit error(s)".  This generated 14,000+ log lines per hour
        and masked legitimate correction events.  See _bch_decode() docstring.
    """
    if len(dibits) < 32:
        return None
    # Convert 32 dibits → 64 individual bits (MSB first within each dibit)
    bits = _dibits_to_bits(dibits[:32])
    cw   = bits[:63]        # mutable: BCH corrects this in place
    errs = _bch_decode(cw)
    if errs < 0:
        # BCH cannot correct (> 5 errors, Chien fail, or no syndrome).
        # Fall back to raw read so genuine P25 frames in deep fade still pass
        # the NAC filter.  False-sync hits will produce random NAC ≠ 0x842
        # and be silently dropped by the caller.  See FALSE SYNC RISK above.
        # errs < 0: -1 = Chien search fail, -2 = too many errors (> 5)
        nac  = sum(bits[i] << (11 - i) for i in range(12))
        duid = sum(bits[12 + i] << (3 - i) for i in range(4))
        return nac, duid, errs   # bch_errs = -1 or -2 (uncorrectable)
    if errs > 0:
        # 1-5 errors: BCH correction is guaranteed valid for BCH(63,16,11).
        log(f'    BCH NID: corrected {errs} bit error(s)')
    nac  = sum(cw[i] << (11 - i) for i in range(12))
    duid = sum(cw[12 + i] << (3 - i) for i in range(4))
    return nac, duid, errs   # bch_errs = 0..5

def decode_tsbk_fields(dibits):
    """
    Decode 48-dibit (96-bit) TSBK block.
    Validates CRC-CCITT-16 over data bits 0-79 vs stored CRC bits 80-95.
    Returns dict with at minimum opcode/opcode_name/crc_ok; richer fields per opcode.

    TSBK frame layout (96 bits, MSB-first):
      [0]   Last-Block flag (1 bit)
      [1]   Reserved       (1 bit)
      [2:7] Opcode         (6 bits)  ← raw[0] & 0x3F
      [8:15] MFR ID        (8 bits)  ← raw[1]
      [16:79] Info         (64 bits) ← raw[2..9]
      [80:95] CRC-CCITT-16 (16 bits) ← raw[10..11]
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

    # Discard obviously corrupt frames (bad CRC, non-standard manufacturer)
    if not crc_ok:
        return r
    if raw[1] not in (0x00, 0x90):   # 0x00 = standard, 0x90 = Motorola
        return r

    # ── Per-opcode info field decoding ─────────────────────────────────────────
    # raw[2..9] = 8 info bytes (64 bits).  Fields described per TIA-102.AABC.

    if op in (0x00, 0x20):       # GRP_V_CH_GRNT / GRP_V_GRANT
        # raw[2]: options (8 bits)
        # raw[3..5]: source unit ID (24 bits)
        # raw[6..7]: group ID / talk group (16 bits)
        # raw[8..9]: channel (ch_id 4 | ch_num 12)
        r['src_id']  = (raw[3] << 16) | (raw[4] << 8) | raw[5]
        r['dest_tg'] = (raw[6] << 8)  | raw[7]
        r['channel'] = ((raw[8] & 0xF) << 8) | raw[9]

    elif op == 0x02:             # GRP_V_CH_GRNT_UPD
        # raw[2..3]: channel A, raw[4..5]: group A, raw[6..7]: channel B, raw[8..9]: group B
        r['channel'] = ((raw[2] & 0xF) << 8) | raw[3]
        r['dest_tg'] = (raw[4] << 8) | raw[5]

    elif op in (0x04, 0x24):     # UU_V_CH_GRNT / UU_V_CH_GRNT_UPD
        r['src_id']  = (raw[3] << 16) | (raw[4] << 8) | raw[5]
        r['dest_tg'] = (raw[6] << 16) | (raw[7] << 8) | raw[8]   # dest unit id (24 bits)
        r['channel'] = ((raw[2] & 0xF) << 8) | raw[9]

    elif op == 0x10:             # IDEN_UP (identifier update)
        # raw[2]: channel_id (upper nibble), spacing index (lower nibble)
        # raw[3..4]: base frequency (16 bits × 125 kHz)
        r['iden_ch_id']  = (raw[2] >> 4) & 0xF
        r['iden_bw']     = raw[2] & 0xF
        r['iden_tx_off'] = (raw[3] << 8) | raw[4]   # TX offset in units
        r['iden_ch_num'] = ((raw[5] & 0x3) << 8) | raw[6]
        r['iden_base_f'] = (raw[7] << 8) | raw[8]   # base freq × 125 kHz step

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
        # raw[2..4]: WACN ID (20 bits) + SYS ID upper 4 bits
        # raw[5..6]: SYS ID (12 bits)
        # raw[7..8]: channel
        wacn_hi   = (raw[2] << 12) | (raw[3] << 4) | (raw[4] >> 4)
        sys_id    = ((raw[4] & 0xF) << 8) | raw[5]
        r['wacn']    = wacn_hi
        r['sys_id']  = sys_id
        r['channel'] = ((raw[6] & 0xF) << 8) | raw[7]

    elif op == 0x3B:             # RFSS_STS_BCAST
        # raw[2..4]: WACN(20) + SYS_ID(4 upper)
        # raw[5]: SYS_ID lower 8 → sys_id = raw[4]&F << 8 | raw[5]
        # raw[6]: RFSS_ID(8)  raw[7]: SITE_ID(8)
        # raw[8..9]: channel
        sys_id      = ((raw[4] & 0xF) << 8) | raw[5]
        r['wacn']   = (raw[2] << 12) | (raw[3] << 4) | (raw[4] >> 4)
        r['sys_id']  = sys_id
        r['rfss_id'] = raw[6]
        r['site_id'] = raw[7]
        r['channel'] = ((raw[8] & 0xF) << 8) | raw[9]

    elif op == 0x3C:             # ADJ_STS_BCAST
        # Adjacent site info: LRA, RFSS, SITE, channel, SYS_ID, WACN
        r['adj_sys_id']  = ((raw[2] & 0xF) << 8) | raw[3]
        r['adj_rfss_id'] = raw[4]
        r['adj_site_id'] = raw[5]
        r['channel']     = ((raw[6] & 0xF) << 8) | raw[7]

    elif op == 0x3D:             # ID_UPD (RFSS ID update)
        r['wacn']   = (raw[2] << 12) | (raw[3] << 4) | (raw[4] >> 4)
        r['sys_id'] = ((raw[4] & 0xF) << 8) | raw[5]

    # ── ISP (mobile→base) opcodes ──────────────────────────────────────────────
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
                res = decode_nid(dibits)
                if res:
                    nac, duid, bch_errs = res   # 3-tuple now includes BCH error count
                    if nac != EXPECTED_NAC:
                        self.buf     = self.buf[self.nid_off:]
                        self.nid_off = 0
                        self.state   = self.HUNT
                        continue
                    self.nac      = nac
                    self.duid     = duid
                    self.bch_errs = bch_errs   # stash for PAYLOAD state
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
             'nid_bch_errs': bch_errs}   # BCH NID error count (0-5, -1, or -2)
        if pay_dibits and self.duid == 0x7:    # TSBK
            fields = decode_tsbk_fields(pay_dibits)
            f.update(fields)
        elif pay_dibits and self.duid == 0xC:  # PDU — 1/2-rate trellis header decode
            fields = decode_pdu_header(pay_dibits)
            f.update(fields)   # includes trellis_errs
        elif pay_dibits and self.duid == 0xF:  # TDULC
            # LC word: 56 data bits + 16 CRC-CCITT = 72 bits = 36 dibits
            if len(pay_dibits) >= 36:
                lc_bits  = _dibits_to_bits(pay_dibits[:36])
                computed = _p25_crc16_bits(lc_bits[:56])
                received = sum(lc_bits[56 + i] << (15 - i) for i in range(16))
                f['crc_ok'] = (computed == received)
        return f

    def _read(self, offset, n):
        mid = _SPY // 2
        dibits = []
        for i in range(n):
            idx = offset + i * _SPY + mid
            if idx < len(self.buf):
                s = float(self.buf[idx])
                if   s >=  self.threshold: q = +3
                elif s >=  0.0:            q = +1
                elif s >= -self.threshold: q = -1
                else:                      q = -3
                dibits.append(_SYM2DB[q])
            else:
                dibits.append(0)
        return dibits

# ── Per-channel state ──────────────────────────────────────────────────────────
class Channel:
    def __init__(self, info):
        self.freq_hz     = info['freq_hz']
        self.label       = info['label']
        self.c_bin       = info['c_bin']   # signed centre bin offset from DC
        self.e_lo        = max(0, info['e_lo'])
        self.e_hi        = min(CHUNK_SAMPLES, info['e_hi'])
        self.noise_hist  = []
        self.noise_floor = None
        self.above_count = 0
        self.is_active   = False
        self.tail_left   = 0
        self.peak_snr    = 0.0
        self.prev_samp   = np.complex64(1.0)  # for FM inter-chunk continuity
        self.decoder     = P25Decoder(self.freq_hz, self.label)

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
        # Clamp (shouldn't be needed for our channel geometry, but be safe)
        lo    = max(0, lo)
        hi    = min(CHUNK_SAMPLES, hi)
        n     = hi - lo

        ch_bins = spectrum_shifted[lo:hi].copy()
        if n < _N_OUT:
            ch_bins = np.pad(ch_bins, (0, _N_OUT - n))

        # Hanning taper in frequency domain (brick-wall → smooth sidelobe roll-off)
        ch_bins *= _CHAN_WIN

        # IFFT → time domain baseband (unshift so DC is at index 0)
        baseband = np.fft.ifft(np.fft.ifftshift(ch_bins)).astype(np.complex64)

        # FM demodulate with inter-chunk phase continuity
        aug      = np.concatenate([[self.prev_samp], baseband])
        self.prev_samp = baseband[-1] if len(baseband) else self.prev_samp
        fm_audio = np.angle(aug[1:] * np.conj(aug[:-1])).astype(np.float32)

        # Scale radians/sample → P25 symbol space (±1800 Hz → ±3, ±600 Hz → ±1)
        # Required so sync template correlation reaches threshold 0.75
        fm_audio *= _FM_SCALE

        return fm_audio

# ── TCP receive thread ────────────────────────────────────────────────────────
# Reads raw IQ chunks from b210_server.py running on the Windows host.
# The B210 is opened natively there (no VMware xHCI), eliminating USB overflow.
_rx_queue    = queue.Queue(maxsize=RX_QUEUE_DEPTH)
_rx_running  = True
_overflow_ct = 0   # not used for TCP; kept for status formatting compatibility

def _recvall(sock, n):
    """Receive exactly n bytes from sock, or raise EOFError."""
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise EOFError('TCP connection closed by server')
        buf.extend(chunk)
    return bytes(buf)

def tcp_rx_thread():
    """Connect to b210_server.py on Windows host and receive IQ chunks."""
    global _rx_running
    log(f'TCP RX: connecting to {_SERVER_HOST}:{_SERVER_PORT} ...')
    while _rx_running:
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            # Large recv buffer — matches server send buffer (32 MB)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024 * 1024)
            sock.settimeout(10.0)
            sock.connect((_SERVER_HOST, _SERVER_PORT))
            sock.settimeout(None)   # blocking after connect
            log(f'TCP RX: connected to {_SERVER_HOST}:{_SERVER_PORT}')

            while _rx_running:
                hdr   = _recvall(sock, _HDR_SIZE)
                magic, n_samp = struct.unpack(_HDR_FMT, hdr)
                if magic != _MAGIC:
                    log(f'TCP RX: bad magic {magic:#06x} — reconnecting')
                    break
                raw   = _recvall(sock, n_samp * 8)
                chunk = np.frombuffer(raw, dtype=np.complex64).copy()
                try:
                    _rx_queue.put_nowait(chunk)
                except queue.Full:
                    pass  # drop; main thread can't keep up

        except (EOFError, ConnectionRefusedError, OSError, socket.timeout) as e:
            log(f'TCP RX: {e} — retry in 5s')
            time.sleep(5)
        finally:
            try: sock.close()
            except Exception: pass

# ── Signal handler ─────────────────────────────────────────────────────────────
running = True
def handle_sig(sig, frame):
    global running
    running = False
signal.signal(signal.SIGTERM, handle_sig)
signal.signal(signal.SIGINT,  handle_sig)

# ── Main ───────────────────────────────────────────────────────────────────────
def main():
    log(f'=== P25 UL Frame Capture (TCP mode) start ===')
    log(f'  Source : b210_server.py @ {_SERVER_HOST}:{_SERVER_PORT}')
    log(f'  Center : {CENTER_HZ/1e6:.1f} MHz   Rate: {SAMPLE_RATE/1e6:.0f} MSPS   '
        f'Chunk: {CHUNK_SAMPLES} samples')
    log(f'  Channels: {len(CHANNELS)}  FFT bin: {_BIN_HZ:.1f} Hz  '
        f'N_OUT: {_N_OUT}  SPY: {_SPY}  NAC: {EXPECTED_NAC:#05x}')
    for info in _CH_INFO:
        log(f'  {info["freq_hz"]/1e6:.4f} MHz  offset={info["offset_hz"]/1e6:+.4f} MHz  '
            f'{info["label"]}')

    con = init_db(DB_PATH)
    log(f'Database: {DB_PATH}')

    global _rx_running
    rxt = threading.Thread(target=tcp_rx_thread, daemon=True)
    rxt.start()
    log(f'TCP RX thread started — waiting for connection to {_SERVER_HOST}:{_SERVER_PORT}')

    channels       = [Channel(info) for info in _CH_INFO]
    total_chunks   = 0
    total_frames   = 0
    warmup_done    = False
    # Running FEC error totals for STATUS reporting and BER estimation
    fec_bch_errs   = 0   # total BCH bit corrections (NID)
    fec_bch_frames = 0   # frames where BCH corrected ≥1 error (errs 1-5)
    fec_bch_fail   = 0   # frames where BCH was uncorrectable (errs -1 or -2)
    fec_bch_clean  = 0   # frames where BCH needed 0 corrections
    fec_trl_errs   = 0   # total trellis hard substitutions (PDU)
    fec_trl_frames = 0   # PDU frames with ≥1 trellis hard error
    fec_nid_total  = 0   # total NID decode attempts (for BER denominator)
    fec_pdu_total  = 0   # total PDU trellis decode attempts

    log(f'Warming up ({WARMUP_CHUNKS} chunks ≈ '
        f'{WARMUP_CHUNKS * CHUNK_SAMPLES / SAMPLE_RATE:.1f}s)...')

    while running:
        try:
            chunk = _rx_queue.get(timeout=2.0)
        except queue.Empty:
            continue

        total_chunks += 1

        # FFT — shared for all channels
        spectrum = np.fft.fftshift(np.fft.fft(chunk * _WIN, n=CHUNK_SAMPLES))
        psd      = 20.0 * np.log10(np.abs(spectrum) / CHUNK_SAMPLES + 1e-12)

        if not warmup_done:
            for ch in channels:
                ch.update_energy(psd)
            if total_chunks >= WARMUP_CHUNKS:
                warmup_done = True
                nf_str = '  '.join(
                    f'{ch.freq_hz/1e6:.4f}={ch.noise_floor:+.1f}dBfs'
                    for ch in channels if ch.noise_floor is not None
                )
                log(f'WARMUP COMPLETE  noise floors: {nf_str}')
            continue

        for ch in channels:
            _, active = ch.update_energy(psd)
            if not active:
                continue

            fm_audio = ch.channelize(spectrum)
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
                    nid_bch_errs = bch,
                    trellis_errs = trl,
                )

        if total_chunks % 1000 == 0:
            mins = total_chunks * CHUNK_SAMPLES / SAMPLE_RATE / 60
            act  = ', '.join(f'{ch.freq_hz/1e6:.4f}'
                             for ch in channels if ch.is_active) or 'none'
            # FEC quality breakdown every 1000 chunks (~2.2 min at 8 MSPS):
            #   BCH: clean=0-errors | corr=1-5 errors corrected | fail=>5 (uncorrectable)
            #   TRL: trellis hard substitutions / (pdu_frames × 49 steps)
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
            log(f'STATUS  {mins:.1f} min  chunks={total_chunks}  '
                f'frames={total_frames}  overflows={_overflow_ct}'
                + nid_str + trl_str
                + f'  active={act}')

    _rx_running = False
    con.close()
    log(f'=== Stopped  chunks={total_chunks}  frames={total_frames} ===')

if __name__ == '__main__':
    main()
