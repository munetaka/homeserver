#!/bin/bash
# システム全体の健全性を一括確認する (読み取りのみ)。
# 判定は Pi 上の `health check` (src/cli/health.py。/healthz と同じ判定) が行い、
# 1 つでも NG なら終了コード 1、Pi に繋がらなければ 255 を返す。
# 使い方: scripts/health-check.sh [ssh-host]   (既定: homeserver)
set -uo pipefail
HOST="${1:-homeserver}"

echo "== git =="
unpushed=$(git rev-list --count origin/main..main 2>/dev/null || echo "?")
echo "未プッシュ: ${unpushed} commits / 作業ツリー: $(if [ -n "$(git status --porcelain)" ]; then echo dirty; else echo clean; fi)"

echo "== raspi (${HOST}) =="
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" bash -s <<'REMOTE'
cd /opt/homeserver || exit 2
.venv/bin/health check
rc=$?
echo "-- 参考 (判定に含めない) --"
echo "CPU温度  : $(awk '{printf "%.1f", $1/1000}' /sys/class/thermal/thermal_zone0/temp)C"
printf "watchdog発火累計: "
cat /var/lib/collector-watchdog/restart_count 2>/dev/null || echo 0
printf "最新バックアップ: "
sudo rclone lsl pcloud:homeserver-backup/vm-backup-latest.tar.gz 2>/dev/null \
  | awk '{print $2" "substr($3,1,8)" ("int($1/1024)" KB)"}' || echo "取得失敗"
exit "$rc"
REMOTE
rc=$?
case "$rc" in
  0) echo "== 判定: OK ==" ;;
  255) echo "== 判定: NG (Pi に ssh できない) ==" ;;
  *) echo "== 判定: NG (exit ${rc}) ==" ;;
esac
exit "$rc"
