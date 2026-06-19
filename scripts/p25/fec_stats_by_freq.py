#!/usr/bin/env python3
"""
fec_stats_by_freq.py — P25 FEC error statistics binned by frequency

Usage:
  fec_stats_by_freq.py [tsv_path] [window_minutes]

Defaults: /home/sdr/P25/trunk-b210/p25_downlink.tsv, 30 minutes
"""

import sys, re, csv
from datetime import datetime, timezone, timedelta
from collections import defaultdict

TSV_DEFAULT = "/home/sdr/P25/trunk-b210/p25_downlink.tsv"
WIN_DEFAULT = 30
# Optional 3rd arg: ISO timestamp "2026-06-18T23:25:22" sets cutoff directly

FIRST_STAGE_BITS = {
    ("ESS", "HMG"): 240,
    ("LCW", "HMG"): 240,
    ("LCW", "GLY"): 288,
    ("HDU", "GLY"): 864,
}


def parse_fec(s):
    return [
        {"type": m[0], "d": int(m[1]), "c": int(m[2]), "r": int(m[3])}
        for m in re.findall(r'([A-Z0-9]+)\(d=(-?\d+),c=(-?\d+),r=(\d+)\)', s)
    ]


def ber_for(fec_key, d1, c1, n):
    bits = FIRST_STAGE_BITS.get(fec_key)
    if bits is None or n == 0:
        return None
    total = n * bits
    if fec_key[1] == "GLY":
        return d1 / total
    if fec_key[1] == "HMG":
        return max(0, 2 * d1 - c1) / total


def fmt_ber(v):
    if v is None:  return "   N/A   "
    if v == 0.0:   return "  0.00e+00"
    return f"{v:10.3e}"


def main():
    path   = sys.argv[1] if len(sys.argv) > 1 else TSV_DEFAULT
    now    = datetime.now(timezone.utc)
    if len(sys.argv) > 2 and sys.argv[2].startswith("20"):
        # 3rd arg is an ISO timestamp (e.g. "2026-06-18T23:25:22")
        cutoff = datetime.fromisoformat(sys.argv[2]).replace(tzinfo=timezone.utc)
        window = f"since {sys.argv[2]}"
    else:
        window = int(sys.argv[2]) if len(sys.argv) > 2 else WIN_DEFAULT
        cutoff = now - timedelta(minutes=window)
        window = f"{window} min"

    # stats[freq][fec_key] = {n, d1, c1, r1, d2, c2, r2, bad}
    stats = defaultdict(lambda: defaultdict(lambda: {
        "n": 0,
        "d1": 0, "c1": 0, "r1": 0,
        "d2": 0, "c2": 0, "r2": 0,
        "bad": 0,
    }))

    total = 0

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
                continue
            if ts < cutoff:
                continue

            segs = parse_fec(fec_str)
            if not segs:
                continue

            freq    = row[12].strip()   # freq_mhz col
            ft      = row[5].strip()    # frame_type col
            fec_key = (ft, segs[0]["type"])

            total += 1
            s = stats[freq][fec_key]
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
    print(f"\nP25 FEC Statistics by Frequency — {window}  ({w0}–{w1} UTC)")
    print(f"Frames with FEC data: {total}\n")

    stage2_name = {"ESS": "RS8", "LCW": "RS12", "HDU": "RS16", "RAW_PDU": "—"}

    col_w = 10  # freq column width

    for freq in sorted(stats, key=lambda f: float(f) if f else 0):
        freq_stats = stats[freq]

        # aggregate BER-capable measurements for this freq
        ber_lines = []
        for fec_key in sorted(freq_stats):
            s = freq_stats[fec_key]
            ber = ber_for(fec_key, s["d1"], s["c1"], s["n"])
            if ber is not None:
                ber_lines.append((fec_key, s["n"], ber))

        # total frames this freq
        total_freq = sum(s["n"] for s in freq_stats.values())

        ber_summary = ""
        if ber_lines:
            parts = []
            for fec_key, n, ber in ber_lines:
                parts.append(f"{fec_key[0]}/{fec_key[1]} BER≈{ber:.2e}")
            ber_summary = "  " + "  ".join(parts)

        print(f"{'─'*72}")
        print(f"  {freq} MHz    {total_freq} frames{ber_summary}")
        print(f"{'─'*72}")

        # Per fec_key row
        print(f"  {'Frame':<8}  {'Chain':<16}  {'Frames':>6}"
              f"  {'det-1':>6} {'cor-1':>6} {'bad-1':>5}"
              f"  {'det-2':>6} {'cor-2':>6} {'bad-2':>5}"
              f"  {'untrust':>7}  {'BER est':>10}")

        for fec_key in sorted(freq_stats):
            ft, st = fec_key
            s = freq_stats[fec_key]
            n = s["n"]
            if n == 0:
                continue

            s2    = stage2_name.get(ft, "?")
            chain = f"{st}|{s2}" if s2 != "—" else st
            ber   = ber_for(fec_key, s["d1"], s["c1"], n)

            bad_pct = f"{100*s['bad']/n:.0f}%" if s["bad"] else "  0%"

            if ft == "RAW_PDU":
                print(f"  {ft:<8}  {chain:<16}  {n:>6}"
                      f"  {s['d1']:>6} {s['c1']:>6} {s['r1']:>5}"
                      f"  {'blks':>6} {'only':>6} {'':>5}"
                      f"  {s['bad']:>4}/{n:<3}       N/A")
            else:
                print(f"  {ft:<8}  {chain:<16}  {n:>6}"
                      f"  {s['d1']:>6} {s['c1']:>6} {s['r1']:>5}"
                      f"  {s['d2']:>6} {s['c2']:>6} {s['r2']:>5}"
                      f"  {s['bad']:>4}/{n:<3} {fmt_ber(ber)}")
        print()


if __name__ == "__main__":
    main()
