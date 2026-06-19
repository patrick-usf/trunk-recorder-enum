#!/bin/bash
# Howard County, MD — Motorola Astro 25 P25 Phase 1 — DOWNLINK MONITOR
# Source: Raspberry Pi HackRF at 192.168.1.36, ZMQ port 5557
# HackRF: center 858.5 MHz, 10 Msps, range 853.5–863.5 MHz
#
# Control channels (downlink):
#   858.7375, 859.2375, 859.7375, 860.2375 MHz
# Data/voice channels within HackRF range:
#   853.0625, 856.2375, 856.7375, 857.2375, 857.7375, 858.2375, 860.7375 MHz

exec /home/sdr/trunk-build/p25-data-monitor \
  --zmq        tcp://192.168.1.36:5557 \
  --center     858497550 \
  --rate       10000000 \
  --qpsk \
  --freq       858737500 \
  --freq       859237500 \
  --freq       859737500 \
  --freq       860237500 \
  --freq       856237500 \
  --freq       856737500 \
  --freq       857237500 \
  --freq       857737500 \
  --freq       858237500 \
  --freq       860737500 \
  --log        /home/sdr/P25/trunk-b210/p25_downlink.tsv \
  --sys-name   HowardCounty \
  --freq-table /home/sdr/P25/trunk-b210/howard_county_freq_table.csv \
  --nac        0x842 \
  "$@"
