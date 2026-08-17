#!/bin/bash
# システム全体の健全性を一括確認する (読み取りのみ)。
# 使い方: scripts/health-check.sh [ssh-host]   (既定: homeserver)
set -uo pipefail
HOST="${1:-homeserver}"

echo "== git =="
unpushed=$(git rev-list --count origin/main..main 2>/dev/null || echo "?")
echo "未プッシュ: ${unpushed} commits / 作業ツリー: $(if [ -n "$(git status --porcelain)" ]; then echo dirty; else echo clean; fi)"

echo "== raspi (${HOST}) =="
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" bash -s <<'REMOTE'
printf "services : "
systemctl is-active switchbot.service echonet.service victoria-metrics.service grafana-server.service | tr '\n' ' '
echo "(switchbot / echonet / victoria-metrics / grafana)"
printf "timers   : "
systemctl is-active collector-watchdog.timer vm-backup.timer rpi-metrics.timer | tr '\n' ' '
echo "(watchdog / backup / rpi-metrics)"
age() {
  curl -s --max-time 10 -G "http://localhost:8428/api/v1/query" \
    --data-urlencode "query=time() - max(timestamp($1[1h]))" | python3 -c "
import json,sys
try:
    r = json.load(sys.stdin)['data']['result']
    print(int(float(r[0]['value'][1])) if r else '>3600')
except Exception:
    print('?')"
}
echo "鮮度     : climate $(age climate_temperature)s / power $(age power_generation_w)s (600s超でwatchdog発火)"
# コスト系列は毎晩更新される (最新サンプルは昨日0時打点 = 正常でも最大約48h)。
# 60時間 (216000s) より古い場合は cost-update が単価未登録などで日次をスキップ
# している疑い → journalctl -u cost-update.service を確認
cost_age() {
  curl -s --max-time 10 -G "http://localhost:8428/api/v1/query" \
    --data-urlencode "query=time() - max(timestamp(last_over_time(cost_day_yen[3d])))" | python3 -c "
import json,sys
try:
    r = json.load(sys.stdin)['data']['result']
    print(int(float(r[0]['value'][1])) if r else '>259200')
except Exception:
    print('?')"
}
CA=$(cost_age)
printf "コスト鮮度: %ss" "$CA"
case "$CA" in
  ''|'?'|'>'*) echo "  ← 要確認: journalctl -u cost-update.service (単価未登録?)" ;;
  *) if [ "$CA" -gt 216000 ]; then
       echo "  ← 要確認: journalctl -u cost-update.service (単価未登録?)"
     else
       echo " (OK)"
     fi ;;
esac
echo "disk     : $(df -h / | awk 'NR==2{print $5" used, "$4" free"}')"
echo "CPU温度  : $(awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)C"
printf "watchdog発火累計: "
cat /var/lib/collector-watchdog/restart_count 2>/dev/null || echo 0
printf "最新バックアップ: "
sudo rclone lsl pcloud:homeserver-backup/vm-backup-latest.tar.gz 2>/dev/null \
  | awk '{print $2" "substr($3,1,8)" ("int($1/1024)" KB)"}' || echo "取得失敗"
REMOTE
