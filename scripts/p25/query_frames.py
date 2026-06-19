#!/usr/bin/env python3
"""
Quick DB inspector — run on DragonOS VM.

Usage:
  python3 query_frames.py                         # UL DB + ul_frames table (default)
  python3 query_frames.py dl                      # DL DB + dl_frames table
  python3 query_frames.py /path/to/custom.db tbl  # custom DB path + table name
"""
import sqlite3, sys

# ── DB / table selection ────────────────────────────────────────────────────────
arg = sys.argv[1] if len(sys.argv) > 1 else 'ul'

if arg == 'ul':
    db_path = '/home/sdr/P25/ul_frames.db'
    tbl     = 'ul_frames'
elif arg == 'dl':
    db_path = '/home/sdr/P25/dl_frames.db'
    tbl     = 'dl_frames'
elif arg.endswith('.db'):
    db_path = arg
    tbl     = sys.argv[2] if len(sys.argv) > 2 else 'ul_frames'
else:
    print(f"Usage: {sys.argv[0]} [ul|dl|/path/to.db [table]]", file=sys.stderr)
    sys.exit(1)

print(f"DB: {db_path}  table: {tbl}\n")
db = sqlite3.connect(db_path)
db.row_factory = sqlite3.Row

print("=== Frame type counts ===")
for r in db.execute(f"SELECT duid_name, COUNT(*) n FROM {tbl} GROUP BY duid_name ORDER BY n DESC"):
    print(f"  {r['duid_name']:8s}  {r['n']}")

print("\n=== FEC error correction summary ===")
for r in db.execute(f"""
    SELECT duid_name,
           COUNT(*) n,
           SUM(CASE WHEN nid_bch_errs > 0 THEN 1 ELSE 0 END) bch_frames,
           SUM(CASE WHEN nid_bch_errs > 0 THEN nid_bch_errs ELSE 0 END) bch_bits,
           SUM(CASE WHEN nid_bch_errs < 0 THEN 1 ELSE 0 END) bch_uncorr,
           SUM(CASE WHEN trellis_errs > 0 THEN 1 ELSE 0 END) trl_frames,
           SUM(COALESCE(trellis_errs,0)) trl_total
    FROM {tbl} GROUP BY duid_name ORDER BY n DESC"""):
    bch_ber = r['bch_bits'] / (r['n'] * 63) if r['n'] else 0
    trl_ber = r['trl_total'] / (r['n'] * 49) if r['n'] else 0
    print(f"  {r['duid_name']:8s}  n={r['n']:4d}  "
          f"BCH: {r['bch_frames']}frm/{r['bch_bits']}bits({r['bch_uncorr']}uncorr)  "
          f"BER_NID≈{bch_ber:.2e}  "
          f"TRL: {r['trl_frames']}frm/{r['trl_total']}hard  BER_TRL≈{trl_ber:.2e}")

print("\n=== BCH correction distribution (NID error counts) ===")
for r in db.execute(f"""
    SELECT nid_bch_errs, COUNT(*) n FROM {tbl}
    WHERE nid_bch_errs IS NOT NULL
    GROUP BY nid_bch_errs ORDER BY nid_bch_errs"""):
    label = {0:'no errors', 1:'1 bit', 2:'2 bits', 3:'3 bits',
             4:'4 bits', 5:'5 bits', -1:'Chien fail', -2:'uncorrectable'}.get(r['nid_bch_errs'], str(r['nid_bch_errs']))
    print(f"  BCH={r['nid_bch_errs']:3d}  ({label:15s})  {r['n']} frames")

print("\n=== Recent TSBKs (last 10, CRC any) ===")
for r in db.execute(f"""
    SELECT id, ts_utc, freq_hz, opcode_name, src_id, dest_tg, crc_ok, snr_db,
           nid_bch_errs
    FROM {tbl} WHERE duid_name='TSBK' ORDER BY id DESC LIMIT 10"""):
    bch = r['nid_bch_errs']
    bch_s = f" BCH={bch}" if (bch is not None and bch != 0) else ""
    print(f"  [{r['id']}] {r['ts_utc']}  {r['freq_hz']/1e6:.4f}MHz  "
          f"op={r['opcode_name']}  src={r['src_id']}  tg={r['dest_tg']}  "
          f"crc={'OK' if r['crc_ok'] else 'BAD'}  snr={r['snr_db']:.1f}dB{bch_s}")

print("\n=== Recent PDUs (last 10) ===")
for r in db.execute(f"""
    SELECT id, ts_utc, freq_hz, pdu_sap, pdu_fmt, pdu_blks, crc_ok, snr_db,
           nid_bch_errs, trellis_errs
    FROM {tbl} WHERE duid_name='PDU' ORDER BY id DESC LIMIT 10"""):
    bch = r['nid_bch_errs']
    trl = r['trellis_errs']
    bch_s = f" BCH={bch}" if (bch is not None and bch != 0) else ""
    trl_s = f" TRL={trl}hard" if (trl is not None and trl > 0) else ""
    print(f"  [{r['id']}] {r['ts_utc']}  {r['freq_hz']/1e6:.4f}MHz  "
          f"sap={r['pdu_sap']}  fmt={r['pdu_fmt']}  blks={r['pdu_blks']}  "
          f"crc={'OK' if r['crc_ok'] else 'BAD'}  snr={r['snr_db']:.1f}dB{bch_s}{trl_s}")

print("\n=== Trellis hard error distribution (PDU only) ===")
for r in db.execute(f"""
    SELECT trellis_errs, COUNT(*) n FROM {tbl}
    WHERE duid_name='PDU' AND trellis_errs IS NOT NULL
    GROUP BY trellis_errs ORDER BY trellis_errs"""):
    print(f"  TRL={r['trellis_errs']:2d} hard  {r['n']} frames")

print("\n=== TSBK opcode breakdown ===")
for r in db.execute(f"""
    SELECT opcode_name, COUNT(*) n FROM {tbl}
    WHERE duid_name='TSBK' GROUP BY opcode_name ORDER BY n DESC"""):
    print(f"  {str(r['opcode_name']):30s}  {r['n']}")

print("\n=== Unique talk groups seen ===")
for r in db.execute(f"""
    SELECT dest_tg, COUNT(*) n FROM {tbl}
    WHERE dest_tg IS NOT NULL GROUP BY dest_tg ORDER BY n DESC LIMIT 20"""):
    print(f"  TG {r['dest_tg']:6d} ({r['dest_tg']:#08x})  {r['n']} frames")

print("\n=== Unique source IDs seen ===")
for r in db.execute(f"""
    SELECT src_id, COUNT(*) n FROM {tbl}
    WHERE src_id IS NOT NULL GROUP BY src_id ORDER BY n DESC LIMIT 20"""):
    print(f"  SRC {r['src_id']:8d} ({r['src_id']:#010x})  {r['n']} frames")

# ── DL-specific: voice channel grant breakdown ──────────────────────────────────
# Shows which voice channels were granted by the CC (only meaningful for DL runs).
if tbl == 'dl_frames':
    print("\n=== Voice channel grants (GRP_V_CH_GRNT* opcodes, DL only) ===")
    for r in db.execute(f"""
        SELECT opcode_name, channel, dest_tg, COUNT(*) n FROM {tbl}
        WHERE opcode_name LIKE 'GRP_V_CH_GRNT%' AND channel IS NOT NULL
        GROUP BY opcode_name, channel, dest_tg ORDER BY n DESC LIMIT 20"""):
        print(f"  {str(r['opcode_name']):25s}  ch={r['channel']:#05x}  "
              f"tg={r['dest_tg']}  {r['n']} grants")

    print("\n=== CRC pass rate by frame type (DL validation summary) ===")
    for r in db.execute(f"""
        SELECT duid_name,
               COUNT(*) total,
               SUM(CASE WHEN crc_ok=1 THEN 1 ELSE 0 END) crc_pass,
               SUM(CASE WHEN crc_ok=0 THEN 1 ELSE 0 END) crc_fail,
               SUM(CASE WHEN crc_ok IS NULL THEN 1 ELSE 0 END) crc_null
        FROM {tbl}
        WHERE duid_name IN ('TSBK','PDU','TDULC')
        GROUP BY duid_name ORDER BY total DESC"""):
        pct = 100 * r['crc_pass'] / r['total'] if r['total'] else 0
        print(f"  {r['duid_name']:8s}  total={r['total']:5d}  "
              f"pass={r['crc_pass']:5d}({pct:.1f}%)  "
              f"fail={r['crc_fail']:5d}  null={r['crc_null']}")
