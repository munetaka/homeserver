# 2026-07-06: bluetoothd の D-Bus 劣化による13時間のデータ欠損

## 概要

bluetoothd(BlueZ)が D-Bus オブジェクト登録に失敗する状態に陥り、コレクターが
BLE スキャンできないまま約13時間データが欠損した(JST 07:28〜20:44)。
コレクターの再起動では回復せず、**bluetoothd の再起動が必要**だった。

## タイムライン(Pi ローカル時刻 = BST。JST は +8h)

| BST | 事象 |
| --- | --- |
| 07-05 23:28:32 | bluetoothd が `Unable to register device interface` / `Unable to create object for found device` を出し始める |
| 07-05 23:28:56 | コレクター最後の正常書き込み(wrote 8 points) |
| 07-05 23:34〜 | コレクターが毎分 `[org.freedesktop.DBus.Error.AccessDenied] Client tried to send a message other than Hello without being registered` |
| 07-06 12:39 | switchbot.service 再起動(Hub 2 対応デプロイの一環)→ **回復せず**。新プロセスはスキャンがハングし CPU を消費 |
| 07-06 12:44 | `systemctl restart bluetooth` → `restart switchbot.service` で**即復旧**(wrote 10 points) |
| 07-06 12:46 | 再発防止の watchdog を作成・有効化 |

## 根本原因

- 長時間の連続 BLE ディスカバリで bluetoothd の D-Bus オブジェクト管理が劣化し、
  新規デバイスオブジェクトの登録に失敗 → クライアント(bleak)の D-Bus セッションも
  AccessDenied で無効化される、BlueZ の既知の劣化パターン
- コレクターの `run` ループは例外を握りつぶして続行する設計だったため、
  **壊れた D-Bus 接続のまま無限にリトライし続け、自力回復の機会がなかった**
- 当時データ鮮度の監視が存在せず、人間がダッシュボードを見るまで気づけなかった

## 影響

- JST 07-06 07:28〜20:44 の約13時間、全センサーのデータ欠損(BLE データは他に記録が
  ないため復元不能)

## 対処

```bash
sudo systemctl restart bluetooth && sleep 5 && sudo systemctl restart switchbot.service
```

## 再発防止(2層)

1. **collector-watchdog**(`deploy/systemd/collector-watchdog.timer`、5分間隔):
   VictoriaMetrics の `max(timestamp(climate_temperature))` で鮮度を判定し、
   600秒超の停滞(または未来タイムスタンプ)で **bluetooth → switchbot.service の順に再起動**。
   bluetoothd 側の故障もカバーする。意図的なサービス停止中・VM 停止中は何もしない
2. **コレクターの self-exit**(commit `e3d5fd2`): run ループが連続5回失敗したら exit(1) し、
   systemd の `Restart=on-failure` に再起動させる(クライアント側の故障はこれで回復)

→ 同種の障害が再発しても欠損は最大10分程度に抑えられる。

## 教訓

- 「例外を握りつぶして continue」する常駐ループは、**接続系リソース(D-Bus・ソケット)が
  腐った場合に最悪の挙動**をする。失敗が続くならプロセスごと死んで作り直すほうが強い
- サービス再起動で直らない障害がある。依存デーモン(bluetoothd)まで含めて再起動するのが
  自宅運用では現実的な落とし所
- 監視は「プロセスが生きているか」ではなく**「データが流れているか」**を見る

## 追記 (2026-07-11): 真の根本原因が判明、コードで恒久修正

同種の障害が 2026-07-11 11:12 に再発し(watchdog により14分で自動復旧)、調査の結果
「bluetoothd の劣化」は二次症状で、**真因はコレクター自身の D-Bus 接続リーク**だったことが
確定した。

- コレクターの `run` ループはサイクル(約80〜95秒)ごとに `asyncio.run()` を呼んでおり、
  bleak はイベントループごとに D-Bus 接続を張るため、**毎サイクル接続が1本リーク**していた
- 約256サイクル(≒6時間)で dbus-daemon の上限に到達:
  `dbus-daemon[678]: The maximum number of active connections for UID 1000 has been
  reached (max_connections_per_user=256)`(11:12:42、エラー開始と同時刻)
- 以降のスキャンは全て `D-Bus AccessDenied` で失敗。bluetoothd 側に残った宙吊りの
  ディスカバリセッションが「コレクター再起動だけでは直らない」症状を作っていた
- bleak メンテナーが公式に指摘している誤用パターン
  (https://github.com/hbldh/bleak/discussions/1273):
  「asyncio.run() はアプリのトップレベルで1回だけ。BlueZManager は閉じない前提の
  アプリ単位シングルトン」

**恒久修正**(commit `feaf656`): `run` ループ全体を単一の `asyncio.run()` 配下に移し、
D-Bus 接続を1本だけ張って使い回す構造に変更。256上限には到達し得なくなった。
watchdog と self-exit は別要因(ハード障害等)への保険として存置。
