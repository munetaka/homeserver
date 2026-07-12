# deploy/ — ラズパイ側の構成物

ラズパイ (raspi4-homeserver) 上で稼働している systemd ユニット・スクリプト・Grafana 設定の原本。
**Pi 側を変更したら必ずここに反映してコミットする**(SDカード故障時にここが唯一の復元元になる)。

## 配置先マッピング

| リポジトリ内 | Pi 上の配置先 |
| --- | --- |
| `systemd/victoria-metrics.service` | `/etc/systemd/system/victoria-metrics.service` |
| `systemd/switchbot.service` | `/etc/systemd/system/switchbot.service` |
| `systemd/echonet.service` | `/etc/systemd/system/echonet.service` |
| `systemd/collector-watchdog.service` / `.timer` | `/etc/systemd/system/` |
| `systemd/vm-backup.service` / `.timer` | `/etc/systemd/system/` |
| `bin/collector-watchdog.sh` | `/usr/local/bin/collector-watchdog.sh` (要 `chmod +x`) |
| `bin/vm-backup.sh` | `/usr/local/bin/vm-backup.sh` (要 `chmod +x`) |
| `systemd/rpi-metrics.service` / `.timer` | `/etc/systemd/system/` |
| `bin/rpi-metrics.sh` | `/usr/local/bin/rpi-metrics.sh` (要 `chmod +x`) |
| `node-exporter/prometheus-node-exporter.default` | `/etc/default/prometheus-node-exporter` |
| `victoria-metrics/scrape.yml` | `/etc/victoria-metrics/scrape.yml` |
| `grafana/provisioning/datasources/victoriametrics.yaml` | `/etc/grafana/provisioning/datasources/` |
| `grafana/provisioning/dashboards/home.yaml` | `/etc/grafana/provisioning/dashboards/` |
| `grafana/dashboards/*.json` | `/var/lib/grafana/dashboards/` (owner: grafana) |

## ゼロからの再構築手順(概要)

1. VictoriaMetrics バイナリ(linux-arm64, OSS版)を GitHub Releases から取得し `/usr/local/bin/victoria-metrics-prod` に配置。
   専用ユーザーとデータディレクトリを作成:
   ```bash
   sudo useradd --system --no-create-home --shell /usr/sbin/nologin victoriametrics
   sudo mkdir -p /var/lib/victoria-metrics && sudo chown victoriametrics: /var/lib/victoria-metrics
   ```
2. 本リポジトリを `/opt/homeserver` に配置し `uv sync`。`.env` を作成(`.env.example` 参照。
   `INFLUX_URL=http://localhost:8428`、`SWITCHBOT_BLE_DEVICES` にセンサー一覧)。
3. Grafana を apt.grafana.com から、`prometheus-node-exporter` を Debian 標準リポジトリから
   インストール(サーバー監視メトリクス用。VictoriaMetrics が `scrape.yml` に従い
   :9100 と :8428 自身を60秒間隔でスクレイプする)。
4. 上記マッピング通りにファイルを配置して:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now victoria-metrics switchbot grafana-server collector-watchdog.timer vm-backup.timer
   ```
5. バックアップを有効化する場合は `sudo rclone config` で `pcloud` リモートを作成
   (未設定の間 vm-backup.sh はスキップ動作)。
6. データ復元: pCloud 上の `homeserver-backup/vm-backup-latest.tar.gz` を取得し、
   `victoria-metrics` **停止中**に以下を実施:
   ```bash
   tar xzf vm-backup-latest.tar.gz          # snapshots/<名前>/... の構造で展開される
   SNAP=<展開されたスナップショット名>
   # 各スナップショットの中身を対応するディレクトリへ戻す
   rsync -a snapshots/$SNAP/            /var/lib/victoria-metrics/
   rsync -a data/big/snapshots/$SNAP/    /var/lib/victoria-metrics/data/big/
   rsync -a data/small/snapshots/$SNAP/  /var/lib/victoria-metrics/data/small/
   rsync -a data/indexdb/snapshots/$SNAP/ /var/lib/victoria-metrics/data/indexdb/
   chown -R victoriametrics: /var/lib/victoria-metrics
   ```
   ※ スナップショットは snapshots/(メタデータ)と data/{big,small,indexdb}/snapshots/
   (実データ)の4箇所に分散する点に注意(vm-backup.sh はこの4箇所を tar している)。
   tar のルートには `.env`(コレクターの設定・SwitchBotトークン・BLEデバイスリスト)も
   含まれるので、`/opt/homeserver/.env` に配置して chmod 640 する。

詳細な運用手順・障害対応は [docs/runbook.md](../docs/runbook.md) を参照。
