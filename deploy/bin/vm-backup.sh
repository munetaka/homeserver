#!/bin/bash
# VictoriaMetrics snapshot -> pCloud backup
set -euo pipefail
command -v rclone >/dev/null && rclone listremotes 2>/dev/null | grep -q "^pcloud:" || { echo "pcloud remote not configured; skipping backup"; exit 0; }
SNAP=$(curl -s -X POST "http://localhost:8428/snapshot/create" | python3 -c "import json,sys; print(json.load(sys.stdin)[\"snapshot\"])")
trap "curl -s -X POST \"http://localhost:8428/snapshot/delete?snapshot=$SNAP\" >/dev/null" EXIT
TARBALL="/tmp/vm-backup.tar.gz"
tar czf "$TARBALL" -C /var/lib/victoria-metrics/snapshots "$SNAP"
rclone copyto "$TARBALL" "pcloud:homeserver-backup/vm-backup-latest.tar.gz"
# 週次の世代コピー (日曜)
if [ "$(date +%u)" = "7" ]; then
  rclone copyto "$TARBALL" "pcloud:homeserver-backup/weekly/vm-backup-$(date +%Y%m%d).tar.gz"
  rclone delete --min-age 60d "pcloud:homeserver-backup/weekly/" || true
fi
rm -f "$TARBALL"
