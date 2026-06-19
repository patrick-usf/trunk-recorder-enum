#!/bin/bash
# Howard County, MD — Motorola Astro 25 P25 Phase 1
# WACN=0xBEE00  SYS_ID=0x84B  RFSS=1  SITE=2  NAC=0x842
#
# Data channel frequencies derived from SNDCP_CH_GRANT distribution
# in control channel logs. Channels within SDR capture range of
# center=856.5 MHz, rate=10.8 Msps (usable ±~5 MHz).
#
# Channel grant share (from CC log, 5012 total SNDCP grants):
#   851.5750: 5%    851.6875: 27%   851.9625: 20%   851.9875: 15%
#   852.6375: 6%    853.0375: 7%    857.7375: 2%    858.2375: 11%
#   (total: 93% coverage)

exec /home/sdr/trunk-build/p25-data-monitor \
  --zmq        tcp://127.0.0.1:5556 \
  --center     856500000 \
  --rate       10800000 \
  --freq       851575000 \
  --freq       851687500 \
  --freq       851962500 \
  --freq       851987500 \
  --freq       852637500 \
  --freq       853037500 \
  --freq       857737500 \
  --freq       858237500 \
  --log        /home/sdr/P25/trunk-b210/p25_data.tsv \
  --sys-name   HowardCounty \
  --freq-table /home/sdr/P25/trunk-b210/howard_county_freq_table.csv \
  "$@"
