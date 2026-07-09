#!/bin/bash
# Raspberry Pi 固有メトリクス (vcgencmd get_throttled) を node_exporter の
# textfile コレクター向けに書き出す。rpi-metrics.timer から毎分実行される。
set -u
OUT=/var/lib/prometheus/node-exporter/rpi.prom
RAW=$(vcgencmd get_throttled | cut -d= -f2)
V=$((RAW))
{
  echo "# HELP rpi_throttled_raw Raw bitmask from vcgencmd get_throttled"
  echo "# TYPE rpi_throttled_raw gauge"
  echo "rpi_throttled_raw ${V}"
  echo "rpi_undervoltage_now $(( (V >> 0) & 1 ))"
  echo "rpi_freq_capped_now $(( (V >> 1) & 1 ))"
  echo "rpi_throttled_now $(( (V >> 2) & 1 ))"
  echo "rpi_undervoltage_occurred $(( (V >> 16) & 1 ))"
  echo "rpi_freq_capped_occurred $(( (V >> 17) & 1 ))"
  echo "rpi_throttled_occurred $(( (V >> 18) & 1 ))"
} > "${OUT}.tmp" && mv "${OUT}.tmp" "${OUT}"
