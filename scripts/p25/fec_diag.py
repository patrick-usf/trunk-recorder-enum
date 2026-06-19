#!/usr/bin/env python3
"""
fec_diag.py — P25 payload FEC diagnostic
Re-examines stored high-SNR TSBK and PDU frames from ul_frames.db,
testing multiple hypotheses to find why CRC is universally failing.

Run on the DragonOS VM:
    python3 /home/sdr/P25/fec_diag.py
"""

import sqlite3, struct, itertools

DB_PATH = '/home/sdr/P25/ul_frames.db'

# ── Helpers ─────────────────────────────────────────────────────────────────────

def bytes_to_dibits(raw):
    """Unpack raw_bytes → list of dibits (2 bits each, MSB-first per dibit)."""
    bits = []
    for b in raw:
        for shift in range(7, -1, -1):
            bits.append((b >> shift) & 1)
    # Pack bit pairs back into dibits
    dibits = []
    for i in range(0, len(bits) - 1, 2):
        dibits.append((bits[i] << 1) | bits[i+1])
    return dibits

def dibits_to_bits(dibits):
    bits = []
    for d in dibits:
        bits.append((d >> 1) & 1)
        bits.append(d & 1)
    return bits

def p25_crc16(bits):
    """CRC-CCITT-16: init=0, poly=0x1021, final XOR=0xFFFF. Over list of bits."""
    crc = 0
    for b in bits:
        if ((crc >> 15) & 1) ^ (b & 1):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF
        else:
            crc = (crc << 1) & 0xFFFF
    return crc ^ 0xFFFF

def tsbk_crc_ok(dibits_48):
    """Check TSBK CRC-CCITT-16 over 48 payload dibits (96 bits)."""
    bits = dibits_to_bits(dibits_48[:48])
    if len(bits) < 96:
        return False
    computed = p25_crc16(bits[:80])
    received = sum(bits[80 + i] << (15 - i) for i in range(16))
    return computed == received

def pdu_crc_ok(dibits_48):
    """
    Check PDU header CRC after trellis decode.
    Returns (crc_ok, decoded_bytes).
    """
    # Strip status symbols at positions 14, 50, 86 of 101 raw air dibits
    STATUS_IDX = frozenset([14, 50, 86])
    if len(dibits_48) < 101:
        return False, None
    chan = [dibits_48[i] for i in range(101) if i not in STATUS_IDX]
    raw = p25_trellis_12(chan)
    hdr_bits = []
    for b in raw[:10]:
        for shift in range(7, -1, -1):
            hdr_bits.append((b >> shift) & 1)
    computed = p25_crc16(hdr_bits)
    received = (raw[10] << 8) | raw[11]
    return computed == received, raw

# ── Trellis decoder (from ul_frame_capture.py) ─────────────────────────────────

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

def p25_trellis_12(chan_dibits):
    d = [0] * 98
    for i in range(98):
        d[_P25_INTERLEAVE[i]] = chan_dibits[i]
    nibs   = [(d[i*2] << 2) | d[i*2+1] for i in range(49)]
    points = [_P25_CMAP[n] for n in nibs]
    state, tdibits = 0, []
    for i in range(49):
        found = False
        for j in range(4):
            if _P25_FSM[state * 4 + j] == points[i]:
                tdibits.append(j); state = j; found = True; break
        if not found:
            hd   = [bin((nibs[i] ^ _P25_DTM[state * 4 + j]) & 0xF).count('1') for j in range(4)]
            best = hd.index(min(hd))
            tdibits.append(best); state = best
    return [(tdibits[i*4] << 6)|(tdibits[i*4+1] << 4)|(tdibits[i*4+2] << 2)|tdibits[i*4+3]
            for i in range(12)]

# ── Dibit transform variants to test ────────────────────────────────────────────

def invert_dibits(d):
    """Flip both bits of every dibit: 00↔11, 01↔10 (polarity inversion)."""
    return [d_^0b11 for d_ in d]

def swap_dibits(d):
    """Swap bit order within each dibit: 01→10, 10→01."""
    return [((d_ & 1) << 1) | ((d_ >> 1) & 1) for d_ in d]

def gray_dibits(d):
    """Gray-decode each dibit."""
    gray = {0b00: 0b00, 0b01: 0b01, 0b11: 0b10, 0b10: 0b11}
    return [gray[d_] for d_ in d]

def bit_rotate(raw_bytes, offset_bits):
    """
    Shift the entire bit stream by offset_bits positions (simulate timing offset).
    offset_bits in [-4, +4].
    """
    bits = []
    for b in raw_bytes:
        for shift in range(7, -1, -1):
            bits.append((b >> shift) & 1)
    bits = bits[offset_bits:] + [0] * abs(offset_bits)
    dibits = [(bits[i] << 1) | bits[i+1] for i in range(0, len(bits)-1, 2)]
    return dibits

TSBK_OP = {
    0x00: 'GRP_V_CH_GRNT', 0x02: 'GRP_V_CH_GRNT_UPD', 0x04: 'UU_V_CH_GRNT',
    0x10: 'IDEN_UP', 0x18: 'LOC_REG_RSP', 0x19: 'GRP_AFF_RSP', 0x1A: 'U_REG_RSP',
    0x1B: 'U_DEREG_ACK', 0x1D: 'DENY_RSP', 0x1E: 'SNDCP_CH_GRNT',
    0x20: 'GRP_V_GRANT', 0x3A: 'NET_STS_BCAST', 0x3B: 'RFSS_STS_BCAST',
    0x3C: 'ADJ_STS_BCAST', 0x40: 'ISP:GRP_V_CH_REQ', 0x44: 'ISP:UU_V_CH_REQ',
    0x56: 'ISP:GRP_AFF_REQ', 0x57: 'ISP:U_DEREG_REQ', 0x5A: 'ISP:U_REG_REQ',
}

# ── Main diagnostic ─────────────────────────────────────────────────────────────

def main():
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row

    # ── 1. Overall CRC pass rate ──────────────────────────────────────────────
    print("=" * 70)
    print("SECTION 1: CRC pass rates by frame type")
    print("=" * 70)
    for row in db.execute("""
        SELECT duid_name,
               COUNT(*) total,
               SUM(CASE WHEN crc_ok=1 THEN 1 ELSE 0 END) passed,
               AVG(snr_db) avg_snr,
               MAX(snr_db) max_snr
        FROM ul_frames
        WHERE duid_name IN ('TSBK','PDU','TDULC')
        GROUP BY duid_name ORDER BY duid_name
    """):
        print(f"  {row['duid_name']:6s}  total={row['total']:4d}  "
              f"CRC_OK={row['passed']:4d}  "
              f"avg_snr={row['avg_snr']:.1f}dB  max_snr={row['max_snr']:.1f}dB")

    # ── 2. Bit error statistics for high-SNR TSBK ─────────────────────────────
    print()
    print("=" * 70)
    print("SECTION 2: Hamming distance to valid CRC for high-SNR TSBKs")
    print("Testing 8 dibit transform variants + bit-shift offsets")
    print("=" * 70)

    variants = [
        ('original',       lambda d: d),
        ('invert',         invert_dibits),
        ('swap_bits',      swap_dibits),
        ('gray',           gray_dibits),
        ('invert+swap',    lambda d: swap_dibits(invert_dibits(d))),
    ]

    # Per-variant counters
    v_pass = {v[0]: 0 for v in variants}
    v_total = 0
    shift_pass = {s: 0 for s in range(-4, 5)}

    tsbk_rows = list(db.execute("""
        SELECT id, ts_utc, freq_hz, snr_db, raw_bytes, n_dibits
        FROM ul_frames
        WHERE duid_name='TSBK' AND snr_db > 20
        ORDER BY snr_db DESC LIMIT 50
    """))

    print(f"\n  Testing {len(tsbk_rows)} TSBK frames with SNR > 20 dB\n")

    # Track if any variant passes
    any_pass = []

    for row in tsbk_rows:
        raw = bytes(row['raw_bytes'])
        n_dibits = row['n_dibits']
        all_dibits = bytes_to_dibits(raw)

        # NID is first 32 dibits, TSBK payload is next 48
        if len(all_dibits) < 80:
            continue
        pay_dibits = all_dibits[32:80]

        v_total += 1
        frame_pass = []

        for name, fn in variants:
            transformed = fn(pay_dibits)
            if tsbk_crc_ok(transformed):
                v_pass[name] += 1
                frame_pass.append(name)

        # Also try bit-shift offsets on full raw bytes
        for shift in range(-4, 5):
            if shift == 0:
                continue
            shifted_all = bit_rotate(raw, shift)
            if len(shifted_all) >= 80:
                shifted_pay = shifted_all[32:80]
                if tsbk_crc_ok(shifted_pay):
                    shift_pass[shift] += 1

        if frame_pass:
            any_pass.append((row['id'], row['snr_db'], frame_pass))

    print("  Variant CRC pass rates:")
    for name, _ in variants:
        pct = 100 * v_pass[name] / v_total if v_total else 0
        print(f"    {name:20s}  {v_pass[name]:3d}/{v_total}  ({pct:.1f}%)")

    print("\n  Bit-shift CRC pass rates (timing offset in bits):")
    for shift, cnt in shift_pass.items():
        pct = 100 * cnt / v_total if v_total else 0
        if cnt > 0:
            print(f"    shift={shift:+d}  {cnt:3d}/{v_total}  ({pct:.1f}%)")
        else:
            print(f"    shift={shift:+d}  0")

    if any_pass:
        print(f"\n  Frames that passed with some variant:")
        for fid, snr, methods in any_pass:
            print(f"    id={fid}  snr={snr:.1f}dB  methods={methods}")

    # ── 3. Raw byte inspection of 5 best TSBK frames ──────────────────────────
    print()
    print("=" * 70)
    print("SECTION 3: Raw byte dump of 10 highest-SNR TSBK frames")
    print("=" * 70)
    for row in tsbk_rows[:10]:
        raw = bytes(row['raw_bytes'])
        all_d = bytes_to_dibits(raw)
        pay_d = all_d[32:80] if len(all_d) >= 80 else []

        bits = dibits_to_bits(pay_d)
        opcode = 0
        if len(bits) >= 8:
            opcode = sum(bits[2 + i] << (5 - i) for i in range(6))
        op_name = TSBK_OP.get(opcode, f'0x{opcode:02X}')

        # CRC detail
        computed = p25_crc16(bits[:80]) if len(bits) >= 96 else -1
        received = sum(bits[80+i] << (15-i) for i in range(16)) if len(bits) >= 96 else -1
        hd = bin(computed ^ received).count('1') if computed >= 0 else -1

        print(f"\n  id={row['id']}  {row['ts_utc']}  {row['freq_hz']/1e6:.4f}MHz"
              f"  snr={row['snr_db']:.1f}dB")
        print(f"    raw_hex (NID+pay): {raw.hex()}")
        print(f"    pay dibits [0:16]: {pay_d[:16]}")
        print(f"    opcode=0x{opcode:02X} ({op_name})  "
              f"CRC computed={computed:#06x} received={received:#06x}  "
              f"Hamming dist={hd}")

        # Try inverted
        inv_d = invert_dibits(pay_d)
        inv_bits = dibits_to_bits(inv_d)
        inv_op = sum(inv_bits[2+i] << (5-i) for i in range(6)) if len(inv_bits) >= 8 else 0
        inv_crc_c = p25_crc16(inv_bits[:80]) if len(inv_bits) >= 96 else -1
        inv_crc_r = sum(inv_bits[80+i] << (15-i) for i in range(16)) if len(inv_bits) >= 96 else -1
        inv_hd = bin(inv_crc_c ^ inv_crc_r).count('1') if inv_crc_c >= 0 else -1
        inv_ok = '*** CRC OK ***' if inv_crc_c == inv_crc_r else ''
        print(f"    INVERTED: opcode=0x{inv_op:02X} ({TSBK_OP.get(inv_op, '?')})  "
              f"CRC computed={inv_crc_c:#06x} received={inv_crc_r:#06x}  "
              f"Hamming dist={inv_hd}  {inv_ok}")

    # ── 4. PDU diagnostic ─────────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("SECTION 4: PDU trellis decode diagnostic (top 10 by SNR)")
    print("=" * 70)
    pdu_rows = list(db.execute("""
        SELECT id, ts_utc, freq_hz, snr_db, raw_bytes, n_dibits
        FROM ul_frames WHERE duid_name='PDU' AND snr_db > 20
        ORDER BY snr_db DESC LIMIT 10
    """))

    for row in pdu_rows:
        raw = bytes(row['raw_bytes'])
        all_d = bytes_to_dibits(raw)
        # PDU: NID=32, payload=101
        pay_d = all_d[32:133] if len(all_d) >= 133 else all_d[32:]
        print(f"\n  id={row['id']}  {row['freq_hz']/1e6:.4f}MHz  snr={row['snr_db']:.1f}dB")
        print(f"    n_dibits={row['n_dibits']}  recovered={len(all_d)}")

        for label, d in [('original', pay_d), ('inverted', invert_dibits(pay_d))]:
            ok, dec = pdu_crc_ok(d)
            if dec:
                fmt = dec[0] & 0x1F
                sap = dec[1] & 0x3F
                mfid = dec[2]
                print(f"    [{label}]  CRC={'OK ***' if ok else 'BAD'}  "
                      f"fmt={fmt:#04x}  sap={sap:#04x}  mfid={mfid:#04x}  "
                      f"bytes={bytes(dec).hex()}")
            else:
                print(f"    [{label}]  not enough dibits")

    # ── 5. NID sanity check ───────────────────────────────────────────────────
    print()
    print("=" * 70)
    print("SECTION 5: NID dibit sanity — verifying stored NAC matches 0x842")
    print("=" * 70)
    _BCH_GFEXP = [
        1,  2,  4,  8, 16, 32,  3,  6, 12, 24, 48, 35,  5, 10, 20, 40,
       19, 38, 15, 30, 60, 59, 53, 41, 17, 34,  7, 14, 28, 56, 51, 37,
        9, 18, 36, 11, 22, 44, 27, 54, 47, 29, 58, 55, 45, 25, 50, 39,
       13, 26, 52, 43, 21, 42, 23, 46, 31, 62, 63, 61, 57, 49, 33,  0,
    ]
    nac_ok = 0; nac_wrong = 0
    for row in db.execute("""
        SELECT id, raw_bytes FROM ul_frames
        WHERE duid_name='TSBK' AND snr_db > 20 LIMIT 50
    """):
        raw = bytes(row['raw_bytes'])
        nid_dibits = bytes_to_dibits(raw)[:32]
        bits = dibits_to_bits(nid_dibits)[:64]
        nac_raw = sum(bits[i] << (11 - i) for i in range(12))
        if nac_raw == 0x842:
            nac_ok += 1
        else:
            nac_wrong += 1
            print(f"  id={row['id']} NAC from raw NID bits = {nac_raw:#05x} (expected 0x842)")

    print(f"  NID raw NAC check: {nac_ok} correct, {nac_wrong} wrong out of "
          f"{nac_ok+nac_wrong} high-SNR TSBKs")

    print()
    print("=" * 70)
    print("DIAGNOSIS COMPLETE")
    print("=" * 70)

if __name__ == '__main__':
    main()
