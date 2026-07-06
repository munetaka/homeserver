# deploy/ — ラズパイ側の構成物

ラズパイ (raspi4-homeserver) 上で稼働している systemd ユニット・スクリプト・Grafana 設定の原本。
**Pi 側を変更したら必ずここに反映してコミットする**(SDカード故障時にここが唯一の復元元になる)。

## 配置先マッピング

| リポジトリ内 | Pi 上の配置先 |
| --- | --- |
| `systemd/victoria-metrics.service` | `/etc/systemd/system/victoria-metrics.service` |
| `systemd/switchbot.service` | `/etc/systemd/system/switchbot.service` |
| `systemd/collector-watchdog.service` / `.timer` | `/etc/systemd/system/` |
| `systemd/vm-backup.service` / `.timer` | `/etc/systemd/system/` |
| `bin/collector-watchdog.sh` | `/usr/local/bin/collector-watchdog.sh` (要 `chmod +x`) |
| `bin/vm-backup.sh` | `/usr/local/bin/vm-backup.sh` (要 `chmod +x`) |
| `grafana/provisioning/datasources/victoriametrics.yaml` | `/etc/grafana/provisioning/datasources/` |
| `grafana/provisioning/dashboards/home.yaml` | `/etc/grafana/provisioning/dashboards/` |
| `grafana/dashboards/home-climate-dashboard.json` | `/var/lib/grafana/dashboards/` (owner: grafana) |

## ゼロからの再構築手順(概要)

1. VictoriaMetrics バイナリ(linux-arm64, OSS版)を GitHub Releases から取得し `/usr/local/bin/victoria-metrics-prod` に配置。
   専用ユーザーとデータディレクトリを作成:
   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin victoriametrics
   sudo mkdir -p /var/lib/victoria-metrics && sudo chown victoriametrics: /var/lib/victoria-metrics
   ```
2. 本リポジトリを `/opt/homeserver` に配置し `uv sync`。`.env` を作成(`.env.example` 参照。
   `INFLUX_URL=http://localhost:8428`、`SWITCHBOT_BLE_DEVICES` にセンサー一覧)。
3. Grafana を apt.grafana.com からインストール。
4. 上記マッピング通りにファイルを配置して:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now victoria-metrics switchbot grafana-server collector-watchdog.timer vm-backup.timer
   ```
5. バックアップを有効化する場合は `sudo rclone config` で `pcloud` リモートを作成
   (未設定の間 vm-backup.sh はスキップ動作)。
6. データ復元: pCloud 上の `homeserver-backup/vm-backup-latest.tar.gz` を展開し、
   `victoria-metrics` 停止中に `/var/lib/victoria-metrics` へ配置して起動
   (スナップショット形式なので `vmrestore` 相当の手動展開で可)。

詳細な運用手順・障害対応は [docs/runbook.md](../docs/runbook.md) を参照。
