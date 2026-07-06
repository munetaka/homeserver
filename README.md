# homeserver

Typer CLI for collecting SwitchBot environmental sensor metrics via Cloud API or BLE and writing them into InfluxDB.

## Features
- Works with SwitchBot Cloud API v1.1 and direct BLE advertisements to avoid API rate limits.
- Decodes Meter, Meter Plus, CO2 meter, and Hub 2 payloads (temperature, humidity, CO2, battery).
- Supports single push or continuous loop writes in InfluxDB line protocol (v2 or v3).
- Provides discovery utilities: list devices from the API, scan BLE radios, and compare API versus BLE readings.

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
| `INFLUX_URL` | yes | Base URL for InfluxDB (`http://host:port`). |
| `INFLUX_BUCKET_OR_DB` | yes | InfluxDB bucket (v2) or database (v3). |
| `INFLUX_TOKEN` | yes | InfluxDB API token. |
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

The loop catches exceptions, logs them to stdout, and continues.

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
Raspberry Pi OS 上で 60 秒ごとに `sb run` を常駐実行し、計測結果を InfluxDB に書き込む例です。

1. Raspberry Pi に必要パッケージを用意します。
   ```bash
   sudo apt update
   sudo apt install python3 python3-pip bluetooth bluez
   pipx install uv  # pipx が無ければ `python3 -m pip install --user uv`
   ```
2. 本リポジトリをサービスで使うディレクトリに配置し、依存を解決します。
   ```bash
   sudo mkdir -p /opt/homeserver
   sudo chown -R pi:pi /opt/homeserver    # 実行ユーザーに合わせて変更
   cd /opt/homeserver
   git clone <このリポジトリ> .
   uv sync
   ```
3. 環境変数ファイルを作成します。
   ```bash
   sudo tee /etc/switchbot.env >/dev/null <<'EOF'
   SWITCHBOT_TOKEN=xxxxxxxxxxxxxxxxxxxx
   SWITCHBOT_SECRET=yyyyyyyyyyyyyyyyyyyy
   INFLUX_URL=http://influxdb.local:8086
   INFLUX_BUCKET_OR_DB=home-sensors
   INFLUX_TOKEN=zzzzzzzzzzzzzzzzzzzzzzzzzzzzzzzz
   LOCATION_PREFIX=home-
   REQUEST_TIMEOUT_S=10
   EF_MODEL=none
   USE_V3_NATIVE=false
   SWITCHBOT_MODE=ble
   SWITCHBOT_BLE_DEVICES=B0:E9:FE:54:48:8F@co2=bedroom,F2:B2:02:06:4A:8B@meter=toilet
   SWITCHBOT_BLE_SCAN_TIMEOUT=20
   EOF
   ```
   `SWITCHBOT_BLE_DEVICES` や `SWITCHBOT_BLE_SCAN_TIMEOUT` は環境に合わせて調整します。
4. systemd ユニットを `/etc/systemd/system/switchbot.service` に作成します。
   ```ini
   [Unit]
   Description=SwitchBot sensor collector
   After=network-online.target bluetooth.service
   Wants=network-online.target bluetooth.service

   [Service]
   Type=simple
   WorkingDirectory=/opt/homeserver
   EnvironmentFile=/etc/switchbot.env
   ExecStart=/usr/bin/env uv run sb run --interval 60 --mode ble --ble-scan-timeout 20
   Restart=on-failure
   User=pi
   Group=pi

   [Install]
   WantedBy=multi-user.target
   ```
   - `ExecStart` は `which uv` で確認できるパスに変更しても構いません。
   - Bluetooth の許可が必要な場合、`User` を `pi` のままなら `sudo usermod -aG bluetooth pi` を追加します。
5. systemd に読み込ませ、起動します。
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now switchbot.service
   ```
6. 動作確認は以下の通りです。
   ```bash
   sudo systemctl status switchbot.service
   journalctl -u switchbot.service -f
   ```
7. データ鮮度 watchdog を設定します。BLE/D-Bus が壊れたままプロセスだけ生き残る障害に備え、climate データが 10 分以上更新されていなければ bluetooth と `switchbot.service` を再起動します。ユニット一式は [deploy/](deploy/) にあります。
   ```bash
   cd /opt/homeserver
   sudo install -m 755 deploy/collector-watchdog.sh /usr/local/bin/collector-watchdog.sh
   sudo install -m 644 deploy/collector-watchdog.service deploy/collector-watchdog.timer /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable --now collector-watchdog.timer
   ```
   動作確認は以下の通りです。
   ```bash
   systemctl list-timers collector-watchdog.timer
   journalctl -u collector-watchdog.service -f
   ```
   スクリプトは VictoriaMetrics が `localhost:8428` で応答すること、および `switchbot.service` が稼働中であることを前提にしています。ポートが異なる場合は `deploy/collector-watchdog.sh` の URL を調整してください。

ユニットは 60 秒間隔 (`--interval 60`) で BLE スキャンを実行し、指定した InfluxDB に書き込みます。必要に応じてコマンドライン引数や環境変数を調整してください。

## Testing
Run the unit test suite with:

```bash
uv run python -m unittest discover -s tests -t .
```

## Notes
- BLE decoding currently covers Meter, Meter Plus, CO2 meters (including outdoor versions), and Hub 2 (temperature/humidity only; no battery since it is mains powered). Unrecognized payloads fall back to `type=unknown` with a `code_0x..` label.
- For reliable BLE results, increase `--ble-scan-timeout` or `SWITCHBOT_BLE_SCAN_TIMEOUT`, especially for devices with long advertising intervals.
- Ensure the SwitchBot REST token and secret are present even when primarily using BLE; the CLI uses them for commands that interact with the Cloud API.
