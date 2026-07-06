# 2026-07-05: InfluxDB 自己メトリクスによるディスク圧迫 & 収集8ヶ月停止

## 概要

システム再構築時の調査で2つの潜在障害を同時に発見した。

1. ディスク使用率 94%(16GB中、残り1.1GB)。原因は InfluxDB の内部統計データ 6.7GB
2. センサーデータの収集が **2025-10-21 から約8ヶ月半停止**していた(誰も気づかなかった)

## タイムライン

- 2025-10-19: 収集開始(Cloud API モード、11センサー)
- 2025-10-21 12:55: 最後のセンサーデータ書き込み。**実際に取れていたのは2日分だけ**
- 2025-10月〜2026-05月: InfluxDB のスクレイパーだけが動き続け、6.7GB のゴミを蓄積
- 2026-05-29: 再起動を機に switchbot.service が起動失敗状態で固定化
- 2026-07-05: 発見・復旧(VictoriaMetrics への移行と同時に実施)

## 根本原因

### ディスク圧迫

InfluxDB 2.x の「スクレイパー」機能に、自分自身の Prometheus メトリクス
(`http://192.168.11.61:8086/metrics`)を **10秒ごとに `home-sensors-raw` バケット
(retention 無制限)へ書き込む設定**が残っていた(セットアップ時のUI操作で作られたと推定)。
`go_memstats_*` / `boltdb_*` など数百系列 × 10秒間隔 ≈ 週700MB ペース。
センサーデータ(`climate`)自体は2日分・数MBしかなかった。

### 収集停止

複合要因。いずれか1つでも直れば気づけた:

1. systemd ユニットが参照する `EnvironmentFile=/etc/switchbot.env` が存在せず、起動即失敗
2. ユニットの `User=pi` — 実際のユーザーは `homepi`(README のテンプレをそのまま使用)
3. `.env` の `SWITCHBOT_BLE_DEVICES=` が空 — BLE モードはデバイス指定必須
4. デプロイされていたコードが古く、BLE モジュール(`switchbot_ble.py`)自体が未配置
   (最新コードは GitHub に未プッシュで Mac にしかなかった)

## 対処

- スクレイパー設定を削除(API 経由、HTTP 204)
- `climate` のみ `influxd inspect export-lp --measurement climate` でエクスポート
  (67,382行、gzip 385KB)し、`CO2` フィールド名を `co2` に正規化して VictoriaMetrics へ移行
- InfluxDB をアンインストールし `/var/lib/influxdb` を削除 → ディスク 94% → 55%
- ユニット修正(EnvironmentFile 削除、User=homepi、uv フルパス)、
  `.env` に11台の BLE デバイスリストを復元(deviceId = BLE MAC の対応を利用)
- 副産物として BLE デコードの華氏変換バグを発見・修正(commit `6a563e3`)

## 再発防止 / 教訓

- **「データが増え続けるだけで通知が無い」構成は必ず腐る**。データ鮮度の watchdog
  (`collector-watchdog.timer`)を導入した(翌日の障害で早速効いた)
- retention 無制限のバケット/DB に自己メトリクスを混ぜない
- systemd ユニット・設定はリポジトリ(`deploy/`)で管理し、Pi 上の手書き設定を残さない
- デプロイしたら**その場で1サイクル分の動作確認**をする(2025-10 の停止は デプロイ後の
  確認不足が直接原因)
