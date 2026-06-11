#!/bin/bash
# Example: start p25-data-monitor for Howard County, MD
#
# System: Motorola Astro 25 P25 Phase 1 FDMA
#   WACN=0xBEE00 (781824)  SYS_ID=0x84B (2123)  RFSS=1  SITE=2  NAC=0x842
#   Control channel: 860.2375 MHz
#   SDR: USRP B210, center=856.5 MHz, rate=10.8 Msps
#
# Data channel plan derived from SNDCP_CH_GRANT distribution.
# All 8 channels below fall within the SDR's capture bandwidth (±5.4 MHz).
# Grant share (5012 grants sampled from CC log):
#   851.5750: 5%    851.6875: 27%   851.9625: 20%   851.9875: 15%
#   852.6375: 6%    853.0375:  7%   857.7375:  2%   858.2375: 11%
#   ─────────────────────────────────────────────────────────  93% total

exec /path/to/trunk-build/p25-data-monitor \
  --zmq    tcp://127.0.0.1:5556 \
  --center 856500000 \
  --rate   10800000 \
  --freq   851575000 \
  --freq   851687500 \
  --freq   851962500 \
  --freq   851987500 \
  --freq   852637500 \
  --freq   853037500 \
  --freq   857737500 \
  --freq   858237500 \
  --log    /path/to/p25_data.tsv \
  --sys-name HowardCounty \
  "$@"
