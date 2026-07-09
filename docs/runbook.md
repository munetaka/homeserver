# Runbook — 自宅センサー収集システム

最終更新: 2026-07-06

## 構成概要

```
[SwitchBot センサー 11台] --BLE advertisement--> [Raspberry Pi 4 (raspi4-homeserver)]
                                                    switchbot.service (60秒ごとにスキャン)
                                                        ↓ line protocol (/api/v2/write)
                                                    VictoriaMetrics :8428 (retention 100y)
                                                        ↓ PromQL             ↓ 毎日03:30 snapshot
                                                    Grafana :3000        vm-backup.timer → pCloud
```

| 項目 | 値 |
| --- | --- |
| ホスト | `raspi4-homeserver.local`(Mac からは ssh エイリアス `homeserver`、ユーザー `homepi`) |
| タイムゾーン | **Asia/Tokyo (JST)**(2026-07-09 に Europe/London から変更) |
| コレクター | `switchbot.service`: `/opt/homeserver` で `uv run sb run --interval 60 --mode ble --ble-scan-timeout 20` |
| 設定 | `/opt/homeserver/.env`(SWITCHBOT_BLE_DEVICES、INFLUX_URL=http://localhost:8428 等) |
| DB | VictoriaMetrics v1.146.0 単一ノード、データ `/var/lib/victoria-metrics` |
| 可視化 | Grafana、ダッシュボード uid `home-climate`(プロビジョニング管理 → `deploy/grafana/`) |
| 監視 | `collector-watchdog.timer`(5分ごと): データ鮮度 >10分 or 未来時刻で bluetooth+collector を自動再起動 |
| サーバー監視 | node_exporter :9100(systemd コレクターは主要ユニットのみ)+ `rpi-metrics.timer`(スロットリング)。VictoriaMetrics 自身が `/etc/victoria-metrics/scrape.yml` に従い 60 秒間隔でスクレイプ。ダッシュボード uid `homeserver-health` |
| バックアップ | `vm-backup.timer`(毎日03:30): VM スナップショット → pCloud `homeserver-backup/` |

## メトリクスのデータモデル

- Influx line protocol の `climate` measurement を VictoriaMetrics が `climate_<field>` に変換:
  `climate_temperature` / `climate_humidity` / `climate_abs_humidity` / `climate_co2` / `climate_battery`
- ラベル: `location`(例 `home-1F-寝室`。プレフィックスは `.env` の `LOCATION_PREFIX`)、
  `device_id`(BLE MAC または SwitchBot deviceId)、`type`(meter / co2 / hub2)
- SwitchBot の Cloud API deviceId は **BLE MAC のコロン抜き**(例 `B0E9FE54488F` = `B0:E9:FE:54:48:8F`)
- サーバー監視系のメトリクス: `node_*`(node_exporter)、`rpi_*`(スロットリング、textfile)、
  `collector_watchdog_*`(発火回数・データ鮮度、textfile)、`vm_*`(VictoriaMetrics 自身)。
  システムメトリクスは約800系列 × 60秒間隔で**年間 1GB 弱**消費する。ディスクが厳しくなったら
  古い `node_*` だけ delete API で間引く選択肢がある(センサーデータは消さない)

## センサー一覧(2026-07-06 時点)

WoIOSensor(防水温湿度計)×9、MeterPro(CO2)×1(1F-寝室)、Hub 2 ×1(1F-ユーティリティ)。
`屋外-玄関`(F2:B2:02:46:55:20)は電池切れ or BLE 圏外で不達(要現地確認)。
デバイスの追加・変更は `/opt/homeserver/.env` の `SWITCHBOT_BLE_DEVICES`(書式 `MAC[@type][=alias]`、type は meter/co2/hub2)を編集して `sudo systemctl restart switchbot.service`。

## 障害対応手順

### グラフが途切れた / データが来ない

1. 鮮度確認(Pi 上):
   ```bash
   curl -sG "http://localhost:8428/api/v1/query" --data-urlencode "query=count(climate_temperature)"
   ```
   結果が空 = 直近5分のデータなし。
2. サービス状態: `systemctl status switchbot victoria-metrics grafana-server`
3. コレクターのログ: `sudo journalctl -u switchbot.service -n 30`
   - **`D-Bus AccessDenied` が毎分出続ける / 1サイクルも完了しない** → bluetoothd 側の障害。
     コレクター再起動では直らない。以下の順で再起動:
     ```bash
     sudo systemctl restart bluetooth && sleep 5 && sudo systemctl restart switchbot.service
     ```
     (watchdog が10分以内に自動で同じ操作をするはず。しなかった場合は
     `sudo journalctl -u collector-watchdog.service` を確認)
   - `wrote N points` の N が少ない → 特定センサーの電池切れ/圏外を疑う。
     `count_over_time(climate_temperature[10m])` を location 別に見ると欠けている個体が分かる。
4. 個体診断(BLE スキャン、**sudo 必須** — homepi のままだと D-Bus AccessDenied になる):
   ```bash
   cd /opt/homeserver && sudo -E env "PATH=$HOME/.local/bin:$PATH" uv run sb scan-ble --timeout-s 30
   ```

### ディスクが逼迫した

`sudo du -xh --max-depth=1 / | sort -rh | head` で犯人を特定。
このワークロードの正常な増加は年間数十MB程度(11センサー×60秒間隔)。それを大きく超えるなら
何かが暴走している(過去例: InfluxDB の自己メトリクススクレイパー → `docs/incidents/2026-07-05-influxdb-scraper-bloat.md`)。

### Pi が SSH に応答しない

ping・各ポート(22/3000/8428)の TCP 応答を個別に確認。ポートは開くが SSH ハンドシェイクが
タイムアウトする場合は高負荷の可能性があるので、時間を置いて再試行。完全に死んでいる場合は
電源再投入 → それでもダメなら SD カード故障を疑い、`deploy/README.md` の再構築手順へ。
データは pCloud の前日バックアップから復元。

## Tips(ハマりどころ)

- **Pi の TZ は 2026-07-09 まで Europe/London (BST) だった**。docs/incidents/ 内の時刻表記は
  当時の BST(JST−8時間)。journalctl は表示時点の TZ に変換するため、古いログを見るときは
  障害記録の BST 表記と 8 時間ずれて見えることに注意。
- **非対話 SSH では uv が PATH にない**: `PATH="$HOME/.local/bin:$PATH"` を明示するか
  `/home/homepi/.local/bin/uv` をフルパスで叩く(systemd ユニットはフルパス指定済み)。
- **BLE の手動スキャンは sudo 必須**(サービスとして動く分は問題ない)。
- **20秒スキャンで1台程度取りこぼすのは正常**(BLE advertise 間隔の揺らぎ)。常駐ループで平均化される。
- **VictoriaMetrics の label/series API は既定で直近しか見ない**。過去データの確認には
  `start=` / `end=` を明示する。
- **VM への書き込みは InfluxDB v2 互換**(`/api/v2/write`)なのでコレクターのコードは
  InfluxDB / VictoriaMetrics のどちらにも書ける。bucket / token パラメータは VM では無視される。
- **SwitchBot BLE デコードの罠**: 湿度バイトの bit7 は「本体の表示単位が°F」を示すだけで、
  値は常に摂氏(過去に華氏変換して −4℃ を記録するバグがあった → commit `6a563e3`)。
  Hub 2 は manufacturer data の bytes 13–15 に温湿度、バッテリー報告なし(→ commit `5f8e452`)。
- **デプロイ**: GitHub 鍵が ssh-agent に無い環境では `git bundle create` + scp + Pi 側で
  `git fetch <bundle> main` を使う。Mac と Pi の working tree を揃えること。
- 障害の詳細な経緯は `docs/incidents/` を参照。
