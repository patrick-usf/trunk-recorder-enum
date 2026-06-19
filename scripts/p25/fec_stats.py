#!/usr/bin/env python3
"""
fec_stats.py — P25 FEC error rate statistics from p25_downlink.tsv

Usage:
  fec_stats.py [tsv_path] [window_minutes]

Defaults: /home/sdr/P25/trunk-b210/p25_downlink.tsv, 30 minutes

BER estimates:
  GLY  — d = actual bit corrections from gly24128Dec(); BER = d / (frames × bits/frame)
  HMG  — d = Hamming codewords with any error; c = single-bit correctable.
           min bit errors = 2d-c (uncorrectable codewords contribute ≥2 each);
           BER_min = (2d-c) / (frames × 240)
  TRL  — d = trellis blocks decoded; cost is path metric, not bit count; BER N/A
  RS*  — symbol-level (6-bit hexbits); not converted to BER
"""

import sys, re, csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TSV_DEFAULT  = "/home/sdr/P25/trunk-b210/p25_downlink.tsv"
WIN_DEFAULT  = 30

# Bits entering the first-stage decoder per frame
FIRST_STAGE_BITS = {
    ("LCW",     "HMG"): 240,    # LDU1: 24 × 10-bit Hamming codewords
    ("ESS",     "HMG"): 240,    # LDU2: same chain
    ("LCW",     "GLY"): 288,    # TDULC: 12 × 24-bit Golay codewords
    ("HDU",     "GLY"): 864,    # HDU:   36 × 24-bit Golay codewords
    ("RAW_PDU", "TRL"): None,   # Viterbi: no bit-level count
}


def parse_fec(s):
    """Return list of {type, d, c, r} dicts from 'HMG(d=0,c=0,r=0)|RS8(d=0,c=0,r=0)'."""
    return [
        {"type": m[0], "d": int(m[1]), "c": int(m[2]), "r": int(m[3])}
        for m in re.findall(r'([A-Z0-9]+)\(d=(-?\d+),c=(-?\d+),r=(\d+)\)', s)
    ]


def ber_for(key, d1, c1, n):
    bits = FIRST_STAGE_BITS.get(key)
    if bits is None or n == 0:
        return None
    total = n * bits
    if key[1] == "GLY":
        return d1 / total          # d = actual bit corrections
    if key[1] == "HMG":
        return max(0, 2*d1 - c1) / total   # minimum estimate


def fmt_ber(v):
    if v is None:  return "   N/A   "
    if v == 0.0:   return "  0.00e+00"
    return f"{v:10.3e}"


def main():
    path   = sys.argv[1] if len(sys.argv) > 1 else TSV_DEFAULT
    window = int(sys.argv[2]) if len(sys.argv) > 2 else WIN_DEFAULT

    now    = datetime.now(timezone.utc)
    cutoff = now - timedelta(minutes=window)

    # keyed by (frame_type, first_seg_type)
    stats = defaultdict(lambda: {
        "n": 0,
        "d1": 0, "c1": 0, "r1": 0,   # first FEC stage totals
        "d2": 0, "c2": 0, "r2": 0,   # second FEC stage totals
        "bad": 0,                      # frames with any r=1
    })

    total = skipped = 0

    with open(path, newline="") as f:
        for row in csv.reader(f, delimiter="\t"):
            if len(row) < 24 or row[0] == "timestamp":
                continue
            fec_str = row[23].strip()
            if not fec_str:
                continue
            try:
                ts = datetime.fromisoformat(row[0].replace("Z", "+00:00"))
            except ValueError:
                skipped += 1
                continue
            if ts < cutoff:
                continue

            segs = parse_fec(fec_str)
            if not segs:
                continue

            total += 1
            ft  = row[5]
            key = (ft, segs[0]["type"])
            s   = stats[key]
            s["n"]  += 1
            s["d1"] += segs[0]["d"];  s["c1"] += segs[0]["c"]
            if segs[0]["r"]: s["r1"] += 1
            if len(segs) > 1:
                s["d2"] += segs[1]["d"];  s["c2"] += segs[1]["c"]
                if segs[1]["r"]: s["r2"] += 1
            if any(sg["r"] for sg in segs):
                s["bad"] += 1

    w0 = cutoff.strftime("%H:%M:%S")
    w1 = now.strftime("%H:%M:%S")
    print(f"\nP25 FEC Error Statistics — last {window} min  ({w0}–{w1} UTC)")
    print(f"Frames with FEC data: {total}\n")

    # ── Channel BER summary ───────────────────────────────────────────────
    print("── Channel BER estimates (pre-correction) ──────────────────────────────")
    print(f"  {'Source':<18}  {'Frames':>6}  {'Bits total':>12}  {'BER estimate':>12}  Note")
    print(f"  {'-'*18}  {'-'*6}  {'-'*12}  {'-'*12}  ----")
    any_ber = False
    for key in sorted(stats):
        s = stats[key]
        n, d1, c1 = s["n"], s["d1"], s["c1"]
        if n == 0:
            continue
        bits = FIRST_STAGE_BITS.get(key)
        ber  = ber_for(key, d1, c1, n)
        if ber is None:
            continue
        label = f"{key[0]}/{key[1]}"
        note  = "(min; 2d-c/bits)" if key[1] == "HMG" else "(d=bit corrections)"
        print(f"  {label:<18}  {n:>6}  {n*bits:>12,}  {fmt_ber(ber)}  {note}")
        any_ber = True
    if not any_ber:
        print("  (no GLY or HMG frames in window)")
    print()

    # ── Per-frame-type breakdown ──────────────────────────────────────────
    # Second-stage codec name by frame type
    stage2_name = {"ESS": "RS8", "LCW": "RS12", "HDU": "RS16", "RAW_PDU": "—"}

    print("── Per frame type ───────────────────────────────────────────────────────")
    # Header
    print(f"  {'Frame':<8}  {'Chain':<18}  {'Frames':>6}"
          f"  {'Stage-1':^20}  {'Stage-2':^20}  {'Bad frms':>8}")
    print(f"  {'':<8}  {'':<18}  {'':<6}"
          f"  {'det':>6} {'cor':>6} {'bad':>5}  {'det':>6} {'cor':>6} {'bad':>5}  {'':<8}")
    print(f"  {'-'*8}  {'-'*18}  {'-'*6}"
          f"  {'-'*6} {'-'*6} {'-'*5}  {'-'*6} {'-'*6} {'-'*5}  {'-'*8}")

    for key in sorted(stats):
        ft, st = key
        s  = stats[key]
        n  = s["n"]
        if n == 0:
            continue
        s2 = stage2_name.get(ft, "?")
        chain = f"{st}|{s2}" if s2 != "—" else st

        # Stage-2 meaningful only when present
        has2 = s["d2"] + s["c2"] + s["r2"] > 0 or ft in ("ESS","LCW","HDU")

        d1, c1, r1 = s["d1"], s["c1"], s["r1"]
        d2, c2, r2 = s["d2"], s["c2"], s["r2"]
        bad = s["bad"]

        if ft == "RAW_PDU":
            # TRL: d/c are block counts, not bit errors; no stage-2
            print(f"  {ft:<8}  {chain:<18}  {n:>6}"
                  f"  {d1:>6} {c1:>6} {r1:>5}  "
                  f"{'(blocks, not bits)':^20}  {bad:>8}")
        elif has2:
            print(f"  {ft:<8}  {chain:<18}  {n:>6}"
                  f"  {d1:>6} {c1:>6} {r1:>5}  {d2:>6} {c2:>6} {r2:>5}  {bad:>8}")
        else:
            print(f"  {ft:<8}  {chain:<18}  {n:>6}"
                  f"  {d1:>6} {c1:>6} {r1:>5}  {'—':>6} {'—':>6} {'—':>5}  {bad:>8}")
    print()

    # ── Anomaly flags ─────────────────────────────────────────────────────
    flags = []
    for key, s in stats.items():
        if s["bad"] > 0:
            ft = key[0]
            pct = 100.0 * s["bad"] / s["n"] if s["n"] else 0
            flags.append(f"  {ft}: {s['bad']}/{s['n']} frames untrusted ({pct:.1f}%)"
                         + (" ← check algid values" if ft == "ESS" else ""))
    if flags:
        print("── Untrusted frame flags (any r=1) ─────────────────────────────────────")
        for f in flags:
            print(f)
        print()


if __name__ == "__main__":
    main()
