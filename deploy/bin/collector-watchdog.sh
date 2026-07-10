#!/bin/bash
# climate データが10分以上更新されていなければ bluetooth + collector を再起動する
# 発火回数とデータ鮮度は node_exporter textfile メトリクスとして書き出す
set -u
STATE_DIR=/var/lib/collector-watchdog
PROM_DIR=/var/lib/prometheus/node-exporter
COUNT_FILE="$STATE_DIR/restart_count"
mkdir -p "$STATE_DIR"

write_metrics() {
  local age="$1"   # 空文字なら age 行を出力しない(算出不能時)
  local count last_ts
  count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
  last_ts=$(cat "$STATE_DIR/last_restart_ts" 2>/dev/null || echo 0)
  [ -d "$PROM_DIR" ] || return 0
  {
    echo "# TYPE collector_watchdog_restarts_total counter"
    echo "collector_watchdog_restarts_total ${count}"
    echo "# TYPE collector_watchdog_last_restart_timestamp_seconds gauge"
    echo "collector_watchdog_last_restart_timestamp_seconds ${last_ts}"
    if [ -n "$age" ]; then
      echo "# TYPE collector_watchdog_data_age_seconds gauge"
      echo "collector_watchdog_data_age_seconds ${age}"
    fi
  } > "$PROM_DIR/collector-watchdog.prom.tmp" \
    && mv "$PROM_DIR/collector-watchdog.prom.tmp" "$PROM_DIR/collector-watchdog.prom"
}

systemctl is-active --quiet switchbot.service || exit 0   # 意図的停止中は何もしない
curl -s --max-time 10 "http://localhost:8428/health" | grep -q OK || exit 0  # VM停止中は判断不能

# 1時間の検索窓で最終サンプルの時刻を取る (MetricsQL の timestamp() rollup)。
# クエリ失敗は -1、1時間データなしは 0、正常時は epoch 秒。
LAST=$(curl -s --max-time 10 -G "http://localhost:8428/api/v1/query" \
  --data-urlencode "query=max(timestamp(climate_temperature[1h]))" | python3 -c "
import json,sys
try:
    d = json.load(sys.stdin)
    if d.get(\"status\") != \"success\":
        print(-1)
    else:
        r = d[\"data\"][\"result\"]
        print(int(float(r[0][\"value\"][1])) if r else 0)
except Exception:
    print(-1)")

if [ "$LAST" -lt 0 ] 2>/dev/null || [ -z "$LAST" ]; then
  # クエリ失敗: 鮮度は判断不能。誤発火もゴミ値の記録もしない
  exit 0
fi

if [ "$LAST" -eq 0 ]; then
  # 1時間まったくデータなし = 確実に停止している。経過時間は不明なので記録しない
  echo "no climate data in the last hour; restarting bluetooth and switchbot.service"
  echo "$(( $(cat "$COUNT_FILE" 2>/dev/null || echo 0) + 1 ))" > "$COUNT_FILE"
  date +%s > "$STATE_DIR/last_restart_ts"
  write_metrics ""
  systemctl restart bluetooth
  sleep 5
  systemctl restart switchbot.service
  exit 0
fi

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
