# homeserver

自宅の温湿度・CO2・電力を Raspberry Pi 4 で収集し、VictoriaMetrics に蓄積して Grafana で可視化する
ホームテレメトリシステム。コレクターは2系統あります:

- **`sb`** — SwitchBot 温湿度/CO2センサー(BLE アドバタイズ直接受信、Cloud API も対応)
- **`el`** — ECHONET Lite 経由の電力系(太陽光発電・分電盤の回路別消費・エアコン・エコキュート)

```
[SwitchBotセンサー×11] --BLE--> sb (switchbot.service) --\
                                                          +--> VictoriaMetrics --> Grafana
[太陽光/分電盤/家電]  --ECHONET Lite--> el (echonet.service) --/        |
                                                    watchdog が鮮度監視 / 毎晩 pCloud へバックアップ
```

## Features
- SwitchBot: BLE アドバタイズを直接デコード(Meter / Meter Plus / CO2 Meter / Hub 2)して
  API レート制限を回避。温度+湿度から絶対湿度も計算して保存
- ECHONET Lite: 太陽光の瞬時/積算発電、分電盤の主幹(買電/売電)と回路別28chの瞬時電力、
  エアコン(消費電力・室温・外気温)、エコキュート(消費電力・残湯量)。AiSEG2 履歴CSVの
  過去データ一括取込(`el import-history`)にも対応
- 耐障害設計: 連続エラー時のプロセス自己再起動 + データ鮮度 watchdog(詳細は runbook)

## Documentation
- [docs/runbook.md](docs/runbook.md) — 本番環境(ラズパイ4)の構成・障害対応手順・ハマりどころ集
- [docs/incidents/](docs/incidents/) — 障害記録(ポストモーテム)
- [deploy/README.md](deploy/README.md) — systemd ユニット・スクリプト・Grafana 設定の原本と再構築手順
- [AGENTS.md](AGENTS.md) — エージェント向け作業規約(デプロイ・ドキュメント更新の決まり)

## Repository layout

| パス | 内容 |
| --- | --- |
| `src/cli/` | コレクター実装(`sync_data.py` = sb、`echonet.py` = el、`switchbot_ble.py` = BLEデコード) |
| `deploy/` | Pi 上の成果物の原本(systemd / スクリプト / Grafana 設定) |
| `docs/` | runbook と障害記録 |
| `scripts/` | 運用スクリプト(`deploy-to-pi.sh`、`health-check.sh`) |
| `.claude/skills/` | Claude Code 用スキル(`/deploy-pi`、`/homeserver-health`) |
| `tests/` | pytest スイート(実機ペイロードのフィクスチャ入り) |

## Requirements
- Python 3.13 or later.
- [uv](https://docs.astral.sh/uv/) for environment management (recommended).
- macOS or Linux with BLE hardware and permissions for BLE mode.
- Valid SwitchBot Cloud API token and secret when using Cloud access.

## Setup
1. Install uv if necessary (`pip install uv`) or follow the uv documentation.
2. Install dependencies from the project root: `uv sync`.
3. Create a `.env` file (see below) or export the required environment variables.

## Configuration
These environment variables are read by the CLI (values shown below are examples):

```dotenv
INFLUX_URL=http://localhost:8428
LOCATION_PREFIX=home-
REQUEST_TIMEOUT_S=10
USE_V3_NATIVE=false
EF_MODEL=none
SWITCHBOT_MODE=ble
SWITCHBOT_BLE_DEVICES=B0:E9:FE:54:48:8F@co2=bedroom,F2:B2:02:06:4A:8B@meter=toilet
SWITCHBOT_BLE_SCAN_TIMEOUT=15
ECHONET_DEVICES=192.168.11.10@solar=太陽光,192.168.11.10@powerboard=分電盤,192.168.11.12@aircon=エアコンA
ECHONET_TIMEOUT_S=3
ECHONET_CIRCUIT_NAMES=1=リビング,2=玄関ホール,11=冷蔵庫
ECHONET_CIRCUIT_EXCLUDE=26,28
```

| Variable | Required | Description |
| --- | --- | --- |
| `INFLUX_URL` | yes | Base URL of the line-protocol endpoint (VictoriaMetrics: `http://host:8428`, InfluxDB: `http://host:8086`). |
| `INFLUX_BUCKET_OR_DB` | InfluxDB のみ | InfluxDB bucket (v2) or database (v3)。VictoriaMetrics では不要(既定 `home`)。 |
| `INFLUX_TOKEN` | InfluxDB のみ | InfluxDB API token。VictoriaMetrics では不要(既定 `none`)。 |
| `SWITCHBOT_TOKEN` | Cloud API のみ | SwitchBot API token (`App -> Profile -> Preferences`)。`sb devices` / `sb compare` / `--mode api` で必要。BLE 収集だけなら不要。 |
| `SWITCHBOT_SECRET` | Cloud API のみ | SwitchBot API secret(同上)。 |
| `LOCATION_PREFIX` | optional | Prepended to the `location` tag written to Influx. |
| `REQUEST_TIMEOUT_S` | optional | HTTP timeout in seconds (default `10`). |
| `USE_V3_NATIVE` | optional | `true` to use `/api/v3/write_lp` (default `false`). |
| `EF_MODEL` | optional | Enhancement factor model for absolute humidity (`none`, `buck`, `its90`). |
| `SWITCHBOT_MODE` | optional | Default acquisition mode (`api` or `ble`, default `api`). |
| `SWITCHBOT_BLE_DEVICES` | optional | Comma-separated `MAC[@type][=alias]` specs used by `push` and `run`. |
| `SWITCHBOT_BLE_SCAN_TIMEOUT` | optional | BLE scan timeout in seconds (default `5`). |
| `ECHONET_DEVICES` | el のみ | Comma-separated `IP@type[=alias]`。type: `solar` / `powerboard` / `aircon` / `ecocute`。 |
| `ECHONET_TIMEOUT_S` | optional | ECHONET Lite 応答タイムアウト秒(default `3`)。 |
| `ECHONET_CIRCUIT_NAMES` | optional | 分電盤の回路番号→名称(`1=リビング,...`)。`name` タグとして付与。 |
| `ECHONET_CIRCUIT_EXCLUDE` | optional | 収集から除外する回路番号(未使用回路)。 |

`@type` accepts values such as `meter`, `co2`, `hub2`, or the raw code label (`code_0x35`) if the device is unknown.

## CLI usage
All commands are exposed by the Typer application registered as the `sb` console script. Run them via uv:

```bash
uv run sb --help
```

### push
One-shot data collection and write to InfluxDB.

```bash
uv run sb push --mode ble --ble-device B0:E9:FE:54:48:8F@co2 --ble-scan-timeout 20
```

- `--mode` selects `api` or `ble`.
- `--ble-device` can be passed multiple times; if omitted, `SWITCHBOT_BLE_DEVICES` is used.
- When running in API mode, the command fetches the `/status` for every eligible device before writing.

### run
Continuous loop version of `push`.

```bash
uv run sb run --interval 300 --mode ble --ble-scan-timeout 20
```

The loop catches exceptions, logs them to stdout, and continues. After 5 consecutive failures it exits with a non-zero status so that systemd (`Restart=on-failure`) restarts the process with a fresh BLE/D-Bus session.

### devices
Lists all devices returned by `GET /devices` and prints every key and value from `GET /devices/{id}/status`.

```bash
uv run sb devices
```

Use this to confirm device IDs and check what the Cloud API currently reports (including cases such as stale battery percentages for WoIOSensor models).

### scan-ble
Scans the local BLE radio, identifies SwitchBot advertisements, and infers the device type or model from manufacturer data.

```bash
uv run sb scan-ble --timeout-s 30
```

Output includes `source=switchbot`, inferred `type`, raw `code`, RSSI, and decoded metrics (temperature, humidity, CO2, battery) when available. Non-SwitchBot advertisements are labeled `source=other`.

### compare
Cross-checks Cloud API readings against live BLE data for specific devices.

```bash
uv run sb compare --pair B0E9FE54488F=b0:e9:fe:54:48:8f@co2 --pair F2B202064A8B=f2:b2:02:06:4a:8b --ble-scan-timeout 30
```

- `--pair` follows `deviceId=BLE_MAC[@type]`. If `@type` is omitted, the CLI guesses based on the device type returned by the API.
- Output shows API values, BLE values, and deltas for temperature, humidity, CO2, and battery when both sources reported data.

## Power collection CLI (`el`)
ECHONET Lite (UDP 3610) 経由で太陽光・分電盤(回路別)・エアコン・エコキュートを収集する第2のコレクターです。

```bash
uv run el scan --subnet 192.168.11   # LAN上の ECHONET Lite 機器を発見
uv run el push                        # 1回収集して書き込み
uv run el run --interval 60           # 常駐ループ (echonet.service が使用)
uv run el import-history <dir> --max-day YYYYMMDD   # AiSEG2 履歴CSVの一括取込
```

対象機器は `ECHONET_DEVICES`(`IP@type[=alias]` のカンマ区切り、type: solar/powerboard/aircon/ecocute)、
回路名は `ECHONET_CIRCUIT_NAMES`、未使用回路の除外は `ECHONET_CIRCUIT_EXCLUDE` で設定します。
詳細は [deploy/README.md](deploy/README.md) と [docs/runbook.md](docs/runbook.md) を参照。

## Raspberry Pi サービス運用

本番構成(VictoriaMetrics + Grafana + コレクター + watchdog + pCloud バックアップ)の
systemd ユニット・スクリプト・Grafana 設定の原本はすべて [deploy/](deploy/) にあり、
配置先マッピングとゼロからの再構築手順は [deploy/README.md](deploy/README.md) にまとめてあります。

要点だけ:

1. リポジトリを `/opt/homeserver` に配置して `uv sync`、`.env` を作成
   (`INFLUX_URL=http://localhost:8428`、`SWITCHBOT_BLE_DEVICES` に全デバイスを列挙)。
   `.env` は systemd の `EnvironmentFile` ではなく CLI 自身(python-dotenv)が読む。
   ユニットに存在しない `EnvironmentFile` を書くと起動即失敗するので注意
   ([docs/incidents/2026-07-05-influxdb-scraper-bloat.md](docs/incidents/2026-07-05-influxdb-scraper-bloat.md) の教訓)。
2. `deploy/systemd/` のユニットと `deploy/bin/` のスクリプトを配置:
   ```bash
   cd /opt/homeserver
   sudo install -m 755 deploy/bin/*.sh /usr/local/bin/
   sudo install -m 644 deploy/systemd/* /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now victoria-metrics switchbot collector-watchdog.timer vm-backup.timer
   ```
3. 動作確認:
   ```bash
   journalctl -u switchbot.service -f        # 毎分 "wrote N points" が出る
   systemctl list-timers                     # watchdog / backup タイマー
   ```

watchdog は climate データが 10 分以上更新されない場合に bluetooth と switchbot.service を
自動再起動します(bluetoothd 側の故障はコレクター再起動だけでは直らないため。
経緯は [docs/incidents/2026-07-06-bluetoothd-dbus-outage.md](docs/incidents/2026-07-06-bluetoothd-dbus-outage.md))。
日々の運用・障害対応は [docs/runbook.md](docs/runbook.md) を参照してください。

## Testing
Run the test suite (pytest; the dev dependency group is synced automatically):

```bash
uv run pytest             # tests only
uv run pytest --cov       # with coverage (fails under the floor in pyproject.toml)
```

Existing unittest-style tests run under pytest as-is; write new tests in pytest
style. Coverage settings live in `pyproject.toml` (`[tool.coverage.*]`), with
hardware-bound BLE discovery excluded via `pragma: no cover`. CI
(`.github/workflows/test.yml`) runs the same suite plus shellcheck for
`deploy/bin/*.sh` and `scripts/*.sh` on every push.

## Operations
- デプロイ: `scripts/deploy-to-pi.sh` — main の HEAD を Pi へ同期し、成果物の配置と
  サービス再起動まで自動判定(Claude Code では `/deploy-pi`)
- ヘルスチェック: `scripts/health-check.sh` — サービス・データ鮮度・ディスク・バックアップの
  一括確認(Claude Code では `/homeserver-health`)

## Notes
- BLE decoding currently covers Meter, Meter Plus, CO2 meters (including outdoor versions), and Hub 2 (temperature/humidity only; no battery since it is mains powered). Unrecognized payloads fall back to `type=unknown` with a `code_0x..` label.
- For reliable BLE results, increase `--ble-scan-timeout` or `SWITCHBOT_BLE_SCAN_TIMEOUT`, especially for devices with long advertising intervals.
- SwitchBot のトークン/シークレットが必要なのは Cloud API を使うコマンド(`sb devices` / `sb compare` / `--mode api`)だけです。BLE 収集のみの運用では設定不要です。
