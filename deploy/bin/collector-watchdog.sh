#!/bin/bash
# climate データが10分以上更新されていなければ bluetooth + collector を再起動する
set -u
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
  systemctl restart bluetooth
  sleep 5
  systemctl restart switchbot.service
fi
