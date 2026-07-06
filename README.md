# homeserver

Typer CLI for collecting SwitchBot environmental sensor metrics via Cloud API or BLE and writing them into a time-series database over InfluxDB line protocol. The production deployment writes to **VictoriaMetrics** (InfluxDB v2 API compatible); plain InfluxDB v2/v3 also works.

## Features
- Works with SwitchBot Cloud API v1.1 and direct BLE advertisements to avoid API rate limits.
- Decodes Meter, Meter Plus, CO2 meter, and Hub 2 payloads (temperature, humidity, CO2, battery).
- Supports single push or continuous loop writes in InfluxDB line protocol (VictoriaMetrics / InfluxDB v2 / v3).
- Provides discovery utilities: list devices from the API, scan BLE radios, and compare API versus BLE readings.

## Documentation
- [docs/runbook.md](docs/runbook.md) — 本番環境(ラズパイ4)の構成・障害対応手順・ハマりどころ集
- [docs/incidents/](docs/incidents/) — 障害記録(ポストモーテム)
- [deploy/README.md](deploy/README.md) — systemd ユニット・スクリプト・Grafana 設定の原本と再構築手順

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
SWITCHBOT_TOKEN=xxxxxxxxxxxxxxxxxxxx
SWITCHBOT_SECRET=yyyyyyyyyyyyyyyyyyyy
INFLUX_URL=http://localhost:8086
INFLUX_BUCKET_OR_DB=home-sensors
INFLUX_TOKEN=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz
LOCATION_PREFIX=home-
REQUEST_TIMEOUT_S=10
USE_V3_NATIVE=false
EF_MODEL=none
SWITCHBOT_MODE=ble
SWITCHBOT_BLE_DEVICES=B0:E9:FE:54:48:8F@co2=bedroom,F2:B2:02:06:4A:8B@meter=toilet
SWITCHBOT_BLE_SCAN_TIMEOUT=15
```

| Variable | Required | Description |
| --- | --- | --- |
| `SWITCHBOT_TOKEN` | Cloud mode | SwitchBot API token (`App -> Profile -> Preferences`). |
| `SWITCHBOT_SECRET` | Cloud mode | SwitchBot API secret. |
| `INFLUX_URL` | yes | Base URL of the line-protocol endpoint (VictoriaMetrics: `http://host:8428`, InfluxDB: `http://host:8086`). |
| `INFLUX_BUCKET_OR_DB` | yes | InfluxDB bucket (v2) or database (v3). Ignored by VictoriaMetrics (still must be set). |
| `INFLUX_TOKEN` | yes | InfluxDB API token. Ignored by VictoriaMetrics (still must be set). |
| `LOCATION_PREFIX` | optional | Prepended to the `location` tag written to Influx. |
| `REQUEST_TIMEOUT_S` | optional | HTTP timeout in seconds (default `10`). |
| `USE_V3_NATIVE` | optional | `true` to use `/api/v3/write_lp` (default `false`). |
| `EF_MODEL` | optional | Enhancement factor model for absolute humidity (`none`, `buck`, `its90`). |
| `SWITCHBOT_MODE` | optional | Default acquisition mode (`api` or `ble`, default `api`). |
| `SWITCHBOT_BLE_DEVICES` | optional | Comma-separated `MAC[@type][=alias]` specs used by `push` and `run`. |
| `SWITCHBOT_BLE_SCAN_TIMEOUT` | optional | BLE scan timeout in seconds (default `5`). |

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
Run the unit test suite with:

```bash
uv run python -m unittest discover -s tests -t .
```

## Notes
- BLE decoding currently covers Meter, Meter Plus, CO2 meters (including outdoor versions), and Hub 2 (temperature/humidity only; no battery since it is mains powered). Unrecognized payloads fall back to `type=unknown` with a `code_0x..` label.
- For reliable BLE results, increase `--ble-scan-timeout` or `SWITCHBOT_BLE_SCAN_TIMEOUT`, especially for devices with long advertising intervals.
- Ensure the SwitchBot REST token and secret are present even when primarily using BLE; the CLI uses them for commands that interact with the Cloud API.
