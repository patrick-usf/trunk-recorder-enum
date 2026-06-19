#!/usr/bin/env python3
"""
pdu_trellis_test.py — Offline P25 PDU 3/4-rate trellis decoder test harness.

Algorithm ported from p25.rs (kchmck/p25.rs):
  - TribitStates::pair_idx table confirmed identical to DMR ENCODE_TABLE via inv_CMAP
    and to dsd-fme dmr_34.c staticX/staticY
  - Full-traceback Viterbi, 8 states, 49 steps, forced termination at state 0
  - DeinterleaveRedirector::REDIRECTS[98] for dibit-level deinterleave

Data block layout (TIA-102.BAAB confirmed data, fmt=0x16, AN=1):
  byte[0]  bits[7:1] = DBSN (data block seq num, 7 bits)
  byte[0]  bit[0]   = CRC9 MSB
  byte[1]           = CRC9 LSB (8 bits)
  bytes[2..17]      = 16 bytes user data

CRC9 covers: DBSN(7 bits) + user_data(128 bits) = 135 bits.

Usage:
  python3 pdu_trellis_test.py            # decode 50 most-recent PDU frames
  python3 pdu_trellis_test.py --all      # decode all PDU frames in DB
  python3 pdu_trellis_test.py --self-test # run self-tests only
"""

import argparse
import sqlite3
import sys
from pathlib import Path

DB_PATH = Path('/home/sdr/P25/dl_frames.db')

# ─── 1/2-rate trellis constants (from dl_frame_capture.py) ───────────────────

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

def _p25_crc16_bits(bits):
    crc = 0
    for b in bits:
        if ((crc >> 15) & 1) ^ (b & 1):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF

def decode_header_block(chan_dibits):
    """
    Decode 98 channel dibits (after status stripping) via 1/2-rate trellis.
    Returns (bytes_12, hard_errors, crc_ok).
    """
    d = [0] * 98
    for i in range(98):
        d[_P25_INTERLEAVE[i]] = chan_dibits[i]

    nibs   = [(d[i*2] << 2) | d[i*2+1] for i in range(49)]
    points = [_P25_CMAP[n] for n in nibs]

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
            hd   = [bin((nibs[i] ^ _P25_DTM[state * 4 + j]) & 0xF).count('1')
                    for j in range(4)]
            best = hd.index(min(hd))
            tdibits.append(best)
            state = best
            hard_errors += 1

    raw = bytes([
        (tdibits[i*4] << 6) | (tdibits[i*4+1] << 4) |
        (tdibits[i*4+2] << 2) | tdibits[i*4+3]
        for i in range(12)
    ])

    hdr_bits = []
    for b in raw[:10]:
        for shift in range(7, -1, -1):
            hdr_bits.append((b >> shift) & 1)
    computed = _p25_crc16_bits(hdr_bits)
    received = (raw[10] << 8) | raw[11]
    return raw, hard_errors, (computed == received)

# ─── 3/4-rate trellis constants (from p25.rs src/coding/trellis.rs) ──────────

# DeinterleaveRedirector::REDIRECTS[98]
# Semantics: deinterleaved_dibit[i] = channel_dibit[DEINTERLEAVE_34[i]]
DEINTERLEAVE_34 = [
     0,  1, 26, 27, 50, 51, 74, 75,
     2,  3, 28, 29, 52, 53, 76, 77,
     4,  5, 30, 31, 54, 55, 78, 79,
     6,  7, 32, 33, 56, 57, 80, 81,
     8,  9, 34, 35, 58, 59, 82, 83,
    10, 11, 36, 37, 60, 61, 84, 85,
    12, 13, 38, 39, 62, 63, 86, 87,
    14, 15, 40, 41, 64, 65, 88, 89,
    16, 17, 42, 43, 66, 67, 90, 91,
    18, 19, 44, 45, 68, 69, 92, 93,
    20, 21, 46, 47, 70, 71, 94, 95,
    22, 23, 48, 49, 72, 73, 96, 97,
    24, 25,
]
assert len(DEINTERLEAVE_34) == 98

# TribitStates::pair_idx — transition table: PAIR_IDX[cur_state][next_state] = pair index
PAIR_IDX = [
    [ 0,  8,  4, 12,  2, 10,  6, 14],
    [ 4, 12,  2, 10,  6, 14,  0,  8],
    [ 1,  9,  5, 13,  3, 11,  7, 15],
    [ 5, 13,  3, 11,  7, 15,  1,  9],
    [ 3, 11,  7, 15,  1,  9,  5, 13],
    [ 7, 15,  1,  9,  5, 13,  3, 11],
    [ 2, 10,  6, 14,  0,  8,  4, 12],
    [ 6, 14,  0,  8,  4, 12,  2, 10],
]

# States::pair() — PAIRS[idx] = (hi_dibit, lo_dibit) for that pair index
PAIRS_34 = [
    (0b00, 0b10), (0b10, 0b10), (0b01, 0b11), (0b11, 0b11),
    (0b11, 0b10), (0b01, 0b10), (0b10, 0b11), (0b00, 0b11),
    (0b11, 0b01), (0b01, 0b01), (0b10, 0b00), (0b00, 0b00),
    (0b00, 0b01), (0b10, 0b01), (0b01, 0b00), (0b11, 0b00),
]

# Precompute 4-bit edge codeword for each (state, next_state) transition
_EDGE_34 = [[0]*8 for _ in range(8)]
for _s in range(8):
    for _j in range(8):
        _hi, _lo = PAIRS_34[PAIR_IDX[_s][_j]]
        _EDGE_34[_s][_j] = (_hi << 2) | _lo

# Sanity: state 0 must match next_words_34[0] = {0x2,0xD,0xE,0x1,0x7,0x8,0xB,0x4}
assert _EDGE_34[0] == [0x2, 0xD, 0xE, 0x1, 0x7, 0x8, 0xB, 0x4], (
    f"EDGE_34 table mismatch row 0: {[hex(x) for x in _EDGE_34[0]]}"
)

_PDU_STATUS_IDX = frozenset([14, 50, 86])

def strip_status(block_dibits_101):
    """Strip 3 status dibits from 101-dibit raw PDU block → 98 channel dibits."""
    return [d for i, d in enumerate(block_dibits_101) if i not in _PDU_STATUS_IDX]

def viterbi_r34(chan_dibits_98):
    """
    Decode 98 channel dibits (after status stripping) via P25 3/4-rate trellis.

    Full-traceback Viterbi: 49 steps, forced termination at state 0.
    Returns (tribits_48: list[int], path_cost: int).

    chan_dibits_98: list of 98 dibit values (0..3) in raw channel order,
                   BEFORE deinterleave — the deinterleave is applied internally.
    """
    # Deinterleave: data_order[i] = chan[DEINTERLEAVE_34[i]]
    deint = [chan_dibits_98[DEINTERLEAVE_34[i]] for i in range(98)]

    INF = 10**9
    metric  = [INF] * 8
    metric[0] = 0
    back = [[0]*8 for _ in range(49)]

    for t in range(49):
        recv = (deint[t*2] << 2) | deint[t*2 + 1]
        new_metric = [INF] * 8
        for s in range(8):
            if metric[s] == INF:
                continue
            for j in range(8):
                cost = metric[s] + bin(recv ^ _EDGE_34[s][j]).count('1')
                if cost < new_metric[j]:
                    new_metric[j] = cost
                    back[t][j] = s
        metric = new_metric

    # Traceback: trellis must end at state 0 (all-zero flush)
    st = 0
    path = [0] * 49
    path[48] = st
    for t in range(48, 0, -1):
        st = back[t][st]
        path[t-1] = st

    return path[:48], metric[0]

def tribits_to_bytes(tribits_48):
    """Pack 48 tribits → 18 bytes (144 bits), MSB first per tribit."""
    out = bytearray(18)
    for i, tr in enumerate(tribits_48):
        bp = i * 3
        out[bp >> 3]       |= ((tr >> 2) & 1) << (7 - (bp & 7))
        out[(bp+1) >> 3]   |= ((tr >> 1) & 1) << (7 - ((bp+1) & 7))
        out[(bp+2) >> 3]   |=  (tr       & 1) << (7 - ((bp+2) & 7))
    return bytes(out)

def crc9_p25(data_bytes_18):
    """
    Compute P25 CRC-9 for a confirmed data block.
    Covers DBSN (bits 0-6) + user_data (bits 16-143) = 135 bits.
    Polynomial: G(x) = x^9 + x^6 + x^4 + x^3 + 1 (0x1059 / divisor 0x059)
    Returns computed CRC-9 (9 bits).
    """
    POLY = 0x059  # lower 9 bits of G(x)
    crc = 0
    dbsn = (data_bytes_18[0] >> 1) & 0x7F
    user = data_bytes_18[2:18]

    bits = []
    for i in range(7, 0, -1):
        bits.append((dbsn >> (i-1)) & 1)
    for b in user:
        for i in range(7, -1, -1):
            bits.append((b >> i) & 1)

    for bit in bits:
        msb = (crc >> 8) & 1
        crc = ((crc << 1) | bit) & 0x1FF
        if msb:
            crc ^= POLY
    for _ in range(9):
        msb = (crc >> 8) & 1
        crc = (crc << 1) & 0x1FF
        if msb:
            crc ^= POLY
    return crc

def unpack_dibits(raw_bytes, n_dibits):
    """Unpack packed bytes (4 dibits/byte, MSB first) → list of n_dibits dibit values."""
    dibits = []
    for b in raw_bytes:
        dibits.append((b >> 6) & 3)
        dibits.append((b >> 4) & 3)
        dibits.append((b >> 2) & 3)
        dibits.append(b & 3)
    return dibits[:n_dibits]

# ─── Self-tests ───────────────────────────────────────────────────────────────

def _fsm_encode_tribits(tribits_49):
    """Encode 49 tribits through FSM → 98 DATA-order dibits."""
    state = 0
    data_d = []
    for tr in tribits_49:
        hi, lo = PAIRS_34[PAIR_IDX[state][tr]]
        data_d.append(hi)
        data_d.append(lo)
        state = tr
    return data_d

def _apply_interleave(data_d):
    """
    Convert 98 DATA-order dibits to CHANNEL-order.
    The deinterleave reads: deint[i] = chan[DEINTERLEAVE_34[i]].
    So forward interleave writes: chan[DEINTERLEAVE_34[i]] = data_d[i].
    """
    chan = [0] * 98
    for i in range(98):
        chan[DEINTERLEAVE_34[i]] = data_d[i]
    return chan

def run_self_tests():
    errors = 0

    # Test 1: table cross-check against previously validated next_words_34
    expected_row0 = [0x2, 0xD, 0xE, 0x1, 0x7, 0x8, 0xB, 0x4]
    ok = (_EDGE_34[0] == expected_row0)
    print(f"  [1] EDGE_34 table row 0 vs next_words_34: {'PASS' if ok else 'FAIL'}")
    if not ok:
        errors += 1

    # Test 2: clean round-trip (encode → interleave → decode, 0 errors)
    tribits_in = [1, 2, 3, 4, 5, 6, 7, 0] * 6  # 48 data tribits
    flush = [0]                                    # FSM flush at step 48
    data_d = _fsm_encode_tribits(tribits_in + flush)
    chan_d  = _apply_interleave(data_d)
    got, cost = viterbi_r34(chan_d)
    ok = (got == tribits_in) and (cost == 0)
    print(f"  [2] Clean round-trip (0 errors): {'PASS' if ok else 'FAIL'}  cost={cost}")
    if not ok:
        errors += 1
        for i in range(48):
            if got[i] != tribits_in[i]:
                print(f"      tribit[{i}]: got {got[i]}, expected {tribits_in[i]}")

    # Test 3: round-trip with 1 injected single-bit error in channel dibits
    # (correction of ≥2 simultaneous errors depends on code free distance and pattern)
    chan_err = list(chan_d)
    chan_err[4]  ^= 1    # flip LSB of channel dibit 4
    got, cost = viterbi_r34(chan_err)
    ok = (got == tribits_in) and (cost == 1)
    print(f"  [3] Round-trip with 1-bit channel error: {'PASS' if ok else 'FAIL'}  cost={cost}")
    if not ok:
        errors += 1

    # Test 4: deinterleave is a permutation (no duplicates, no out-of-range)
    ok = (sorted(DEINTERLEAVE_34) == list(range(98)))
    print(f"  [4] DEINTERLEAVE_34 is a valid permutation of 0..97: {'PASS' if ok else 'FAIL'}")
    if not ok:
        errors += 1

    # Test 5: all-zeros tribits encode to expected state-0 codewords
    tribits_zero = [0] * 49
    data_d0 = _fsm_encode_tribits(tribits_zero)
    chan_d0  = _apply_interleave(data_d0)
    got, cost = viterbi_r34(chan_d0)
    ok = (got == [0]*48) and (cost == 0)
    print(f"  [5] All-zero tribits round-trip: {'PASS' if ok else 'FAIL'}  cost={cost}")
    if not ok:
        errors += 1

    return errors

# ─── Frame processing ─────────────────────────────────────────────────────────

def decode_frame(row):
    """
    Decode one DB row. Returns result dict.
    row: (id, ts_utc, freq_hz, n_dibits, pdu_blks, raw_bytes)
    """
    frame_id, ts_utc, freq_hz, n_dibits, pdu_blks, raw_bytes = row
    result = {
        'id': frame_id, 'ts': ts_utc, 'freq': freq_hz,
        'n_dibits': n_dibits, 'pdu_blks': pdu_blks,
        'data_blocks': [],
    }

    all_d = unpack_dibits(raw_bytes, n_dibits)

    # Header block: raw dibits [32, 133)
    if n_dibits < 133:
        result['error'] = 'too short for header'
        return result

    hdr_raw  = all_d[32:133]
    hdr_chan = strip_status(hdr_raw)
    hdr_bytes, hdr_errs, crc_ok = decode_header_block(hdr_chan)
    result['hdr_crc_ok']  = crc_ok
    result['hdr_errs']    = hdr_errs
    result['hdr_fmt']     = hdr_bytes[0] & 0x1F
    result['hdr_an']      = (hdr_bytes[0] >> 6) & 1
    result['hdr_sap']     = hdr_bytes[1] & 0x3F
    result['hdr_blks']    = hdr_bytes[6] & 0x7F

    if not crc_ok:
        result['error'] = 'header CRC16 fail'
        return result

    blks = result['hdr_blks']
    an   = result['hdr_an']
    fmt  = result['hdr_fmt']

    for bi in range(1, blks + 1):
        blk_start = 32 + bi * 101  # NID(32) + bi blocks of 101 raw dibits
        blk_end   = blk_start + 101
        if n_dibits < blk_end:
            break

        blk_chan  = strip_status(all_d[blk_start:blk_end])
        tribits, cost = viterbi_r34(blk_chan)
        blk_bytes = tribits_to_bytes(tribits)

        block_info = {
            'bi': bi, 'cost': cost,
            'hex': blk_bytes.hex(),
            'first4': blk_bytes[:4].hex(),
        }

        # CRC-9 check for confirmed data (fmt=0x16, AN=1)
        if fmt == 0x16 and an == 1:
            dbsn = (blk_bytes[0] >> 1) & 0x7F
            crc9_recv = ((blk_bytes[0] & 0x01) << 8) | blk_bytes[1]
            crc9_calc = crc9_p25(blk_bytes)
            block_info['dbsn']      = dbsn
            block_info['crc9_recv'] = crc9_recv
            block_info['crc9_calc'] = crc9_calc
            block_info['crc9_ok']   = (crc9_recv == crc9_calc)

        result['data_blocks'].append(block_info)

    return result

def main():
    ap = argparse.ArgumentParser(description='P25 PDU 3/4-rate offline trellis decoder')
    ap.add_argument('--all',       action='store_true', help='Process all frames (default: 50 most recent)')
    ap.add_argument('--self-test', action='store_true', help='Run self-tests and exit')
    ap.add_argument('--verbose',   action='store_true', help='Print full decoded bytes per block')
    args = ap.parse_args()

    print('=== P25 PDU 3/4-rate Viterbi Test Harness ===\n')
    print('Self-tests:')
    n_err = run_self_tests()
    print(f'  => {n_err} test(s) failed\n')
    if args.self_test:
        sys.exit(0 if n_err == 0 else 1)

    if not DB_PATH.exists():
        print(f'DB not found: {DB_PATH}')
        sys.exit(1)

    con = sqlite3.connect(DB_PATH)
    limit = '' if args.all else 'LIMIT 50'
    rows = con.execute(f'''
        SELECT id, ts_utc, freq_hz, n_dibits, pdu_blks, raw_bytes
        FROM dl_frames
        WHERE duid=12
        ORDER BY ts_unix DESC
        {limit}
    ''').fetchall()
    con.close()

    print(f'Processing {len(rows)} PDU frames from {DB_PATH}\n')
    print(f"{'id':>6}  {'timestamp':19}  {'MHz':9}  {'dib':4}  HDR   fmt  blks  data_blocks")
    print('-' * 90)

    hdr_ok = hdr_fail = data_total = data_cost_sum = crc9_ok = crc9_fail = 0

    for row in rows:
        r = decode_frame(row)
        hdr_stat = 'OK  ' if r.get('hdr_crc_ok') else f"FAIL"
        if r.get('hdr_crc_ok'):
            hdr_ok += 1
        else:
            hdr_fail += 1

        blocks = r.get('data_blocks', [])
        data_total    += len(blocks)
        data_cost_sum += sum(b['cost'] for b in blocks)

        blk_str = ''
        for b in blocks:
            c9 = ''
            if 'crc9_ok' in b:
                c9 = '+' if b['crc9_ok'] else '-'
                if b['crc9_ok']:  crc9_ok   += 1
                else:             crc9_fail += 1
            blk_str += f"  bi{b['bi']}:[{b['first4']}]c{b['cost']}{c9}"

        print(f"{r['id']:>6}  {r['ts'][:19]}  {r['freq']/1e6:9.4f}  "
              f"{r['n_dibits']:4d}  {hdr_stat}  "
              f"{r.get('hdr_fmt',0):02x}   "
              f"{r.get('hdr_blks',0):3d}  "
              + blk_str)

        if args.verbose:
            for b in blocks:
                print(f"         blk{b['bi']}: {b['hex']}")

    print()
    print(f"Summary: frames={len(rows)}  hdr_ok={hdr_ok}  hdr_fail={hdr_fail}")
    print(f"         data_blocks={data_total}  "
          f"avg_cost={'N/A' if not data_total else f'{data_cost_sum/data_total:.1f}'}")
    if crc9_ok + crc9_fail:
        print(f"         crc9_ok={crc9_ok}  crc9_fail={crc9_fail}  "
              f"crc9_rate={crc9_ok/(crc9_ok+crc9_fail):.1%}")
    else:
        print(f"         (no CRC-9 data — need n_dibits=537 frames; "
              f"current DB has header-only captures)")

if __name__ == '__main__':
    main()
