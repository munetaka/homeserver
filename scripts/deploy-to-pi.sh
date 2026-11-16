#!/bin/bash
# main の HEAD をラズパイの /opt/homeserver に同期し、deploy/ 配下の成果物を配置する。
# GitHub を経由せず git bundle + scp で送る。変更内容に応じてサービスを再起動する。
#
# 使い方: scripts/deploy-to-pi.sh [ssh-host]   (既定: homeserver)
set -euo pipefail
HOST="${1:-homeserver}"
cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain)" ]; then
  echo "warning: 作業ツリーに未コミットの変更があります (デプロイされるのは main の HEAD です)" >&2
fi

BUNDLE=$(mktemp /tmp/homeserver-deploy.XXXXXX)
trap 'rm -f "$BUNDLE"' EXIT
git bundle create "$BUNDLE" main 2>/dev/null
scp -q "$BUNDLE" "$HOST:/tmp/deploy.bundle"

ssh "$HOST" bash -s <<'REMOTE'
set -euo pipefail
cd /opt/homeserver
OLD=$(git rev-parse HEAD)
git fetch -q /tmp/deploy.bundle main
git checkout -q -B main FETCH_HEAD
rm -f /tmp/deploy.bundle
NEW=$(git rev-parse HEAD)
echo "synced: ${OLD:0:7} -> ${NEW:0:7} ($(git log -1 --format=%s))"
PATH="$HOME/.local/bin:$PATH" uv sync -q

installed=""
install_if_changed() {
  local src=$1 dst=$2 mode=$3
  shift 3
  if ! sudo cmp -s "$src" "$dst"; then
    sudo install -m "$mode" "$@" "$src" "$dst"
    installed="$installed $(basename "$src")"
  fi
}
for f in deploy/systemd/*; do
  [ -f "$f" ] || continue
  install_if_changed "$f" "/etc/systemd/system/$(basename "$f")" 644
done
# パッケージ提供のユニット (grafana-server など) への drop-in
for f in deploy/systemd/*.d/*.conf; do
  d="/etc/systemd/system/$(basename "$(dirname "$f")")"
  sudo mkdir -p "$d"
  install_if_changed "$f" "$d/$(basename "$f")" 644
done
for f in deploy/bin/*.sh; do
  install_if_changed "$f" "/usr/local/bin/$(basename "$f")" 755
done
for f in deploy/grafana/dashboards/*.json; do
  install_if_changed "$f" "/var/lib/grafana/dashboards/$(basename "$f")" 644 -o grafana -g grafana
done
for f in deploy/grafana/provisioning/datasources/*.yaml; do
  install_if_changed "$f" "/etc/grafana/provisioning/datasources/$(basename "$f")" 644
done
for f in deploy/grafana/provisioning/dashboards/*.yaml; do
  install_if_changed "$f" "/etc/grafana/provisioning/dashboards/$(basename "$f")" 644
done
if [ -n "$installed" ]; then
  echo "installed:$installed"
else
  echo "installed: (none)"
fi

changed_paths=$(git diff --name-only "$OLD" "$NEW")
restart=""
if echo "$changed_paths" | grep -qE '^(src/|pyproject\.toml|uv\.lock)'; then
  restart="switchbot.service echonet.service"
  # healthz は enable するまで起動しない (初回は手動で enable --now)
  if systemctl is-enabled -q healthz.service 2>/dev/null; then restart="$restart healthz.service"; fi
fi
if echo "$changed_paths" | grep -q '^deploy/systemd/'; then
  sudo systemctl daemon-reload
fi
# ユニットファイルが変わった常駐サービスはそのサービス自身を再起動する
for unit in $(echo "$changed_paths" | grep -oE '^deploy/systemd/[a-z-]+\.service' | xargs -n1 basename 2>/dev/null); do
  case "$unit" in
    switchbot.service|echonet.service|victoria-metrics.service)
      restart="$restart $unit" ;;
    cloudflare-tunnel.service|healthz.service)
      # enable するまでは起動しない (tunnel はトークン .env.tunnel を置いてから)
      if systemctl is-enabled -q "$unit" 2>/dev/null; then restart="$restart $unit"; fi ;;
  esac
done
if echo "$changed_paths" | grep -q '^deploy/systemd/grafana-server\.service\.d/'; then
  restart="$restart grafana-server.service"
fi
restart=$(echo "$restart" | xargs -n1 2>/dev/null | sort -u | xargs)
if [ -n "$restart" ]; then
  for s in $restart; do sudo systemctl restart "$s"; done
  echo "restarted: $restart"
else
  echo "restarted: (none)"
fi

printf "active?: "
systemctl is-active switchbot.service echonet.service victoria-metrics.service grafana-server.service collector-watchdog.timer healthz.service | tr '\n' ' '
echo "(switchbot / echonet / victoria-metrics / grafana / watchdog / healthz)"
REMOTE
