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
| コレクター(温湿度) | `switchbot.service`: `/opt/homeserver` で `uv run sb run --interval 60 --mode ble --ble-scan-timeout 20` |
| コレクター(電力) | `echonet.service`: `uv run el run --interval 60`。ECHONET Lite (UDP 3610) で太陽光/分電盤(回路別28ch)/エアコン2台/エコキュートを読む。対象は `.env` の `ECHONET_DEVICES` |
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
- 電力系メトリクス: `power_generation_w`(太陽光)、`power_grid_w`(主幹、正=買電/負=売電)、
  `power_{buy,sell,generation}_total_kwh`(積算)、`power_circuit_watts{circuit="01".."28"}`(回路別)、
  `appliance_{power_w,room_temp,outdoor_temp,setpoint,on,tank_l}`(エアコン/エコキュート)。
  総消費は保存せず `sum(power_generation_w) + sum(power_grid_w)` で導出する。
  比率系も保存せずダッシュボードで導出: **自家消費率** = (発電−売電)/発電(発電をどれだけ
  使い切れたか)、**自給率** = (発電−売電)/総消費(消費をどれだけ発電で賄えたか)。
  日次版は kWh の日合計同士で割る(瞬時比率の平均では正しい日次値にならない)
- 電力の過去データ: `energy_{30min,day}_kwh{kind=generation|buy|sell|consumption}` と
  `energy_{30min,day}_circuit_kwh{circuit,name}`。AiSEG2 の履歴CSV(rireki_*.zip)を
  `el import-history` で投入したもの。初回 2026-07-12(日次 2025-06-16〜2026-07-11)、
  2回目 2026-07-19(`--max-day 20260712` で日次 07-12 の欠け=ライブ移行の継ぎ目を補完。
  30分値は max-day の対象外で 07-19 まで投入)。30分値は AiSEG2 本体に直近94日分しか残らない。
  タイムスタンプは**区間の終端**。再実行する場合は `--max-day` でライブ収集との二重計上を防ぐこと
  (日次のライブ相当は積算メーターから導出しているため、丸1日ライブが揃っていない最後の日まで
  を指定する)。生CSVの zip は `pcloud:homeserver-backup/rireki/` に保管(ローカルには残さない方針)。
  インポート範囲より後の確定日は `el cost-update`(毎晩)が積算メーターから**実体化**して
  `energy_day_kwh` に書き足す。クエリ時の都度計算が残るのは進行中の「今日」のみ
  (経緯は [docs/incidents/2026-07-20-daily-panel-stale-carry.md](incidents/2026-07-20-daily-panel-stale-carry.md))
  AiSEG2 の時間単位履歴は約94日で上書き消失するため、追加救出は不可能(以降はライブ収集が上位互換)
- 電気料金メトリクス: `cost_day_yen{kind=buy|savings|sell_income}`(日次、限界単価ベース)と
  `cost_period_yen{kind=bill|savings|sell_income, month}`(検針期間=10日〆の合計。bill は
  基本料金・定額込みの請求見込み)。料金モデルは [tariff.py](../src/cli/tariff.py)
  (くらし上手L + 燃調月別テーブル + 賦課金 + 軽減措置 + FIT16円)で、**実請求書2ヶ月分を
  1円単位で再現**することをテストで担保(請求書の「電力量料金」「賦課金」は各々円未満切捨て)。
  計算は `cost-update.timer`(毎日00:20)が `el cost-update` を実行して書き込む。
  **単価改定・燃調の新月分・軽減措置は tariff.py を手で更新**(未登録月は警告してスキップ)。
  請求書との差異は月0.3〜0.5kWh・10〜15円程度(内訳は下記「請求書と計測値の差異」参照)

## 請求書と計測値の差異について (2026-07-15 調査済み)

TEPCO請求書の使用量と当システムの買電合計は月0.3〜0.5kWh ずれるが、**原因は特定済みで
これ以上詰められない構造的なもの**。3つの計測点の突き合わせ結果(2ヶ月分で同傾向を確認):

| 計測点 | 2026-07請求期間の合計 | 2026-06請求期間 |
| --- | --- | --- |
| 請求書 | 326 kWh | 281 kWh |
| 検針メーター自身の30分値 (AiSEG2エクスポートの `sm` 系CSV) | 325.7 | 280.8 |
| スマートコスモCT (`rc` 系 = 当システムのデータ源) | 325.5 | 280.7 |

内訳:

1. **請求書 vs 検針メーターCSV (+0.2〜0.3 kWh)**: 請求は累積指示数の差分を整数kWhへ
   切り捨て(端数は翌月へ繰り越し)。加えて AiSEG2 が取得する検針メーターの30分値は
   0.1kWh 単位に量子化されている(CSV生値が100Wh刻み)ため、30日分の丸め誤差が乗る
2. **検針メーター vs CT (+0.13〜0.18 kWh ≈ 0.05%)**: 検定済み電力量計と計測用CTという
   物理的に別の計器の測定差。計器としては良好な一致

**境界仮説(検針期間の切れ目のズレ)は棄却済み**: 期間を±1日ずらした合計は 326.4 / 328.6 kWh
となり、請求値 326 に一致する期間ズレは存在しない。期間定義は「開始日0時〜終了日24時(JST)」で正しい。
再調査する場合は AiSEG2 の履歴エクスポートに含まれる `30minhistory_sm_1_*` / `hourhistory_sm_1_*`
(検針メーター側の値)と `_rc_`(CT側)を期間合計して比較する。
- サーバー監視系のメトリクス: `node_*`(node_exporter)、`rpi_*`(スロットリング、textfile)、
  `collector_watchdog_*`(発火回数・データ鮮度、textfile)、`vm_*`(VictoriaMetrics 自身)。
  システムメトリクスは約800系列 × 60秒間隔で**年間 1GB 弱**消費する。ディスクが厳しくなったら
  古い `node_*` だけ delete API で間引く選択肢がある(センサーデータは消さない)

## 湿度系の計算式の選定理由

絶対湿度と露点はどちらも「飽和水蒸気圧 es(T) の近似式」から導く派生値だが、
用途に応じて式を使い分けている(どちらも同じ物理量の近似で、差はセンサー精度より1桁小さい)。

| 派生値 | 使用式 | 実装場所 | 選定理由 |
| --- | --- | --- | --- |
| 絶対湿度 `climate_abs_humidity` | 岡田の式(液水 −30〜50℃, log10(es) の4次多項式)+ ITS-90 増強係数 f(T,P)。−30℃未満は Goff–Gratch(氷) | コレクター ([sync_data.py](../src/cli/sync_data.py)) で計算し保存 | 順方向計算のみで良いので**精度優先**。範囲内の当てはめ誤差 <0.1%。増強係数(+0.4〜0.5%)は「純水蒸気→湿り空気」の補正で `EF_MODEL=its90` で有効化 |
| 露点温度(ダッシュボードのみ) | Magnus 式(Sonntag 1990: a=17.62, b=243.12) | Grafana パネルの MetricsQL(保存しない) | 露点は es の**逆算**が必要で、Magnus は閉形式で解ける唯一の実用形。誤差 ±0.3% 程度。SwitchBot アプリの表示値と一致することを確認済み(23.5℃/69% → 17.5℃) |

SwitchBot アプリの絶対湿度と当システムの値が 1% 弱ずれるのは、①BLE ブロードキャストの
湿度が整数(±0.5%RH)、②アプリは増強係数なしの Magnus 系と推定、の合算で説明でき、
センサー自体の精度(±1.8%RH ≈ 絶対湿度 0.4 g/m³)より十分小さい。

## 機器一覧(最終更新 2026-07-15)

**BLE 温湿度センサー(SwitchBot)— 全11台稼働中**:
WoIOSensor(防水温湿度計)×9、MeterPro(CO2)×1(1F-寝室)、Hub 2 ×1(1F-ユーティリティ)。
`屋外-玄関`(F2:B2:02:46:55:20)は 2025-10〜2026-07 の長期電池切れの後、
2026-07-14 に電池交換で完全復旧(受信率も他と同水準)。
デバイスの追加・変更は `/opt/homeserver/.env` の `SWITCHBOT_BLE_DEVICES`(書式 `MAC[@type][=alias]`、
type は meter/co2/hub2)を編集して `sudo systemctl restart switchbot.service`。

**ECHONET Lite 機器(電力系)**:

| IP | 機器 | 収集クラス |
| --- | --- | --- |
| 192.168.11.10 | エネルギー計測ユニット MKN7350S1 | 太陽光発電 + 分電盤メータリング(回路別28ch) |
| 192.168.11.12 / .181 | ダイキン エアコンA / B | 消費電力・室温・外気温・設定温度(自己申告、100W/1℃刻み) |
| 192.168.11.169 | ダイキン エコキュート | 消費電力・残湯量(50L刻み) |
| 192.168.11.216 | AiSEG2(コントローラ) | 収集対象外(ECHONET照会に応答しない) |

IP は DHCP 予約で固定を推奨。追加・変更は `.env` の `ECHONET_DEVICES` を編集して
`sudo systemctl restart echonet.service`。

## 障害対応手順

### グラフが途切れた / データが来ない

1. 鮮度確認(Pi 上):
   ```bash
   curl -sG "http://localhost:8428/api/v1/query" --data-urlencode "query=count(climate_temperature)"
   ```
   結果が空 = 直近5分のデータなし。
2. サービス状態: `systemctl status switchbot victoria-metrics grafana-server`
3. コレクターのログ: `sudo journalctl -u switchbot.service -n 30`
   - **`D-Bus AccessDenied` が毎分出続ける / 1サイクルも完了しない** → まず
     `sudo journalctl -u dbus.service | grep maximum` を確認。
     `max_connections_per_user=256` が出ていれば D-Bus 接続リーク
     (2026-07-11 に根本修正済みの既知障害 → incident 記録の追記参照。再発したら退行を疑う)。
     復旧はどちらのケースも:
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
- **VM の窓なしセレクタは最終サンプルを約1サンプル間隔ぶん持ち越す**。日次などまばらな系列を
  `or` フォールバックの左辺に置くと、系列終端の翌日に前日のコピーが現れてフォールバックを隠す。
  まばらな系列は必ず `last_over_time(...[12h])` のような明示窓で読むこと。持ち越し猶予は
  クエリ範囲内のサンプル数から推定されるため、**レンジ長によって再現したりしなかったりする**
  (→ [2026-07-20 の障害記録](incidents/2026-07-20-daily-panel-stale-carry.md))。
- **VM への書き込みは InfluxDB v2 互換**(`/api/v2/write`)なのでコレクターのコードは
  InfluxDB / VictoriaMetrics のどちらにも書ける。bucket / token パラメータは VM では無視される。
- **SwitchBot BLE デコードの罠**: 湿度バイトの bit7 は「本体の表示単位が°F」を示すだけで、
  値は常に摂氏(過去に華氏変換して −4℃ を記録するバグがあった → commit `6a563e3`)。
  Hub 2 は manufacturer data の bytes 13–15 に温湿度、バッテリー報告なし(→ commit `5f8e452`)。
- **ECHONET Lite の罠**: 多くの機器は応答を送信元ポートではなく **UDP 3610 宛て**に返すため、
  クライアントは必ず 3610 に bind する(一時ポートで待つと全機器が「無応答」に見える。
  2026-07-12 の調査で実際に誤診した)。AiSEG2 はコントローラ専業で ECHONET の照会には応答しない。
  エアコンの設定温度 (EPC 0xB3) は自動運転時 0xFD を返すので**符号なし**で解釈すること
  (符号付きだと -3℃ に化ける)。回路別の名称は ECHONET では取れないため、
  Grafana 側の凡例マッピングか AiSEG2 の Web 画面(回路名設定)を参照する
- **bleak の鉄則: `asyncio.run()` はプロセスで1回だけ**。ループ内で毎回呼ぶと D-Bus 接続が
  サイクルごとにリークし、約6時間で dbus-daemon の UID あたり256接続上限に達して
  BLE が全滅する(→ commit `feaf656` で修正。長時間の定期スキャンは
  `collect_ble_readings_async` を単一イベントループから await すること)。
- **デプロイ**: GitHub 鍵が ssh-agent に無い環境では `git bundle create` + scp + Pi 側で
  `git fetch <bundle> main` を使う。Mac と Pi の working tree を揃えること。
- 障害の詳細な経緯は `docs/incidents/` を参照。
