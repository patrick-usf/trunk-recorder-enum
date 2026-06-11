#!/usr/bin/env python3
"""
Decode IDEN_UP / IDEN_UP_TDMA channel tables from p25_frames.tsv CC log.

Bit extraction: trunk-recorder stores the TSBK in a boost::dynamic_bitset<>
with a 16-bit pre-shift.  bitset_shift_mask(tsbk, S, mask) extracts starting
at bit S.  The auto-fill raw_frame encodes bytes via tmp = tsbk >> 16:

    tmp[T] = bit (T%8) of bytes[11 - T//8]      where T = S - 16

So to extract a field starting at tsbk bit S0 with nbits bits:

    for n in 0..nbits-1:
        T = (S0 + n) - 16
        k = 11 - T // 8        # byte index in bytes= string
        j = T % 8              # bit position within byte (0 = LSB)
        bit = (bytes_str[k] >> j) & 1
        value |= bit << n
"""

import sys, csv, re, argparse
from pathlib import Path

# ─── bit extraction ──────────────────────────────────────────────────────────

def bsm(b: bytes, S0: int, nbits: int) -> int:
    r = 0
    for n in range(nbits):
        T = (S0 + n) - 16
        k = 11 - T // 8
        j = T % 8
        if 0 <= k < len(b):
            r |= ((b[k] >> j) & 1) << n
    return r

# ─── IDEN_UP (0x3d) decoder ─────────────────────────────────────────────────

def decode_iden_up(raw_bytes: str) -> dict | None:
    """Decode one IDEN_UP bytes= string.  Returns a dict or None on error."""
    try:
        b = bytes.fromhex(raw_bytes)
    except ValueError:
        return None
    if len(b) < 12:
        return None
    iden   = bsm(b, 76, 4)
    bw     = bsm(b, 67, 9)
    toff0  = bsm(b, 58, 9)
    spac   = bsm(b, 48, 10)
    freq   = bsm(b, 16, 32)
    sign   = (toff0 >> 8) & 1
    toff   = toff0 & 0xff
    if sign == 0:
        toff = -toff
    return {
        'iden':      iden,
        'base_hz':   freq * 5,           # Hz
        'step_hz':   spac * 125,         # Hz
        'txoff_hz':  toff * 250_000,     # Hz (negative = base TX above mobile TX)
        'bw_hz':     bw * 125,           # Hz
        'base_mhz':  freq * 5 / 1e6,
        'step_khz':  spac * 125 / 1e3,
        'txoff_mhz': toff * 0.25,
        'bw_khz':    bw * 0.125,
    }

def chan_to_freq(table: dict, chan: int) -> float:
    return table['base_hz'] + table['step_hz'] * chan

# ─── scan TSV for all unique IDEN_UP entries ─────────────────────────────────

def load_iden_table(tsv_path: str) -> dict[int, dict]:
    seen = set()
    table: dict[int, dict] = {}
    pat = re.compile(r'bytes=([0-9a-f]+)')
    with open(tsv_path) as f:
        reader = csv.reader(f, delimiter='\t')
        for row in reader:
            if len(row) < 9:
                continue
            opcode_name = row[8] if len(row) > 8 else ''
            if opcode_name != 'TSBK_IDEN_UP':
                continue
            raw_frame = row[21] if len(row) > 21 else ''
            m = pat.search(raw_frame)
            if not m:
                continue
            hexstr = m.group(1)
            if hexstr in seen:
                continue
            seen.add(hexstr)
            entry = decode_iden_up(hexstr)
            if entry is not None:
                iden = entry['iden']
                if iden not in table:
                    table[iden] = entry
    return table

# ─── main ────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description='Decode IDEN_UP channel table from CC TSV log')
    ap.add_argument('tsv', nargs='?', default='/home/sdr/P25/trunk-b210/p25_frames.tsv',
                    help='path to p25_frames.tsv (CC log)')
    ap.add_argument('--chan', type=int, nargs='+', metavar='N',
                    help='also show freq for these channel numbers')
    ap.add_argument('--freq', type=float, nargs='+', metavar='MHZ',
                    help='also show chan for these frequencies (MHz)')
    args = ap.parse_args()

    print(f'Scanning {args.tsv} for IDEN_UP entries ...')
    table = load_iden_table(args.tsv)

    if not table:
        print('No IDEN_UP entries found.')
        sys.exit(1)

    print(f'\nIDEN_UP channel table ({len(table)} identifiers found):')
    print(f'  {"iden":>4}  {"base MHz":>11}  {"step kHz":>8}  {"txoff MHz":>9}  {"bw kHz":>6}')
    for iden in sorted(table):
        e = table[iden]
        print(f'  {iden:>4}  {e["base_mhz"]:>11.5f}  {e["step_khz"]:>8.3f}  {e["txoff_mhz"]:>+9.3f}  {e["bw_khz"]:>6.3f}')

    if args.chan:
        print('\nChannel number -> frequency:')
        for iden in sorted(table):
            e = table[iden]
            for ch in args.chan:
                f = chan_to_freq(e, ch) / 1e6
                print(f'  iden={iden}  chan={ch:6d}  ->  {f:.5f} MHz')

    if args.freq:
        print('\nFrequency (MHz) -> channel number:')
        for iden in sorted(table):
            e = table[iden]
            for f in args.freq:
                ch = round((f * 1e6 - e['base_hz']) / e['step_hz'])
                check = chan_to_freq(e, ch) / 1e6
                ok = '✓' if abs(check - f) < 0.001 else f'(nearest: {check:.5f} MHz)'
                print(f'  iden={iden}  {f:.4f} MHz  ->  chan {ch:6d}  {ok}')

if __name__ == '__main__':
    main()
