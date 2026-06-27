#!/bin/bash
# Howard County, MD — Motorola Astro 25 P25 Phase 2 — UPLINK MONITOR
# WACN=0xBEE00  SYS_ID=0x84B  RFSS=1  SITE=2  NAC=0x842
#
# B210 recentered to 810.9 MHz (uplink band).
# Effective coverage: 805.5–816.3 MHz  (center ± 5.4 MHz at 10.8 Msps)
#
# Phase 2 TDMA uplink: H-DQPSK at 6000 sps (--phase2 flag).
# Both traffic channel uplinks and CC uplinks use H-DQPSK on Phase 2.
#
# 800 MHz uplink frequencies (downlink − 45 MHz split):
#   Traffic channels (observed from SNDCP_CH_GRANT / CH_GRANT logs):
#     851.5750 → 806.5750 MHz    851.6875 → 806.6875 MHz
#     851.9625 → 806.9625 MHz    851.9875 → 806.9875 MHz
#     852.6375 → 807.6375 MHz    853.0375 → 808.0375 MHz
#     857.7375 → 812.7375 MHz    858.2375 → 813.2375 MHz
#   Control channel uplinks (ISP TSBKs from subscriber radios):
#     858.7375 → 813.7375 MHz    859.2375 → 814.2375 MHz
#     859.7375 → 814.7375 MHz    860.2375 → 815.2375 MHz

exec /home/sdr/trunk-build/p25-data-monitor \
  --zmq        tcp://127.0.0.1:5556 \
  --center     810900000 \
  --rate       10800000 \
  --phase2 \
  --freq       806575000 \
  --freq       806687500 \
  --freq       806962500 \
  --freq       806987500 \
  --freq       807637500 \
  --freq       808037500 \
  --freq       812737500 \
  --freq       813237500 \
  --freq       813737500 \
  --freq       814237500 \
  --freq       814737500 \
  --freq       815237500 \
  --log        /home/sdr/P25/trunk-b210/p25_uplink.tsv \
  --sys-name   HowardCounty \
  --freq-table /home/sdr/P25/trunk-b210/howard_county_freq_table.csv \
  --nac        0x842 \
  "$@"
