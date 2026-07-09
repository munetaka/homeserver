#!/bin/bash
# climate データが10分以上更新されていなければ bluetooth + collector を再起動する
# 発火回数とデータ鮮度は node_exporter textfile メトリクスとして書き出す
set -u
STATE_DIR=/var/lib/collector-watchdog
PROM_DIR=/var/lib/prometheus/node-exporter
COUNT_FILE="$STATE_DIR/restart_count"
mkdir -p "$STATE_DIR"

write_metrics() {
  local age="$1"
  local count last_ts
  count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  last_ts=$(cat "$STATE_DIR/last_restart_ts" 2>/dev/null || echo 0)
  [ -d "$PROM_DIR" ] || return 0
  {
    echo "# TYPE collector_watchdog_restarts_total counter"
    echo "collector_watchdog_restarts_total ${count}"
    echo "# TYPE collector_watchdog_last_restart_timestamp_seconds gauge"
    echo "collector_watchdog_last_restart_timestamp_seconds ${last_ts}"
    echo "# TYPE collector_watchdog_data_age_seconds gauge"
    echo "collector_watchdog_data_age_seconds ${age}"
  } > "$PROM_DIR/collector-watchdog.prom.tmp" \
    && mv "$PROM_DIR/collector-watchdog.prom.tmp" "$PROM_DIR/collector-watchdog.prom"
}

systemctl is-active --quiet switchbot.service || exit 0   # 意図的停止中は何もしない
curl -s --max-time 10 "http://localhost:8428/health" | grep -q OK || exit 0  # VM停止中は判断不能
LAST=$(curl -s --max-time 10 -G "http://localhost:8428/api/v1/query" \
  --data-urlencode "query=max(timestamp(climate_temperature))" | python3 -c "
import json,sys
try:
    r = json.load(sys.stdin)[\"data\"][\"result\"]
    print(int(float(r[0][\"value\"][1])) if r else 0)
except Exception:
    print(0)")
AGE=$(( $(date +%s) - LAST ))
if [ "$AGE" -gt 600 ] || [ "$AGE" -lt -120 ]; then
  echo "climate data stale (${AGE}s); restarting bluetooth and switchbot.service"
  echo "$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))" > "$COUNT_FILE"
  date +%s > "$STATE_DIR/last_restart_ts"
  write_metrics "$AGE"
  systemctl restart bluetooth
  sleep 5
  systemctl restart switchbot.service
else
  write_metrics "$AGE"
fi
