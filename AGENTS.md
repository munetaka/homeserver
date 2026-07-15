# AGENTS.md — エージェント向け作業規約

自宅ラズパイ4で動くセンサー/電力収集システム。構成の詳細・障害対応・ハマりどころは
[docs/runbook.md](docs/runbook.md)、過去障害は [docs/incidents/](docs/incidents/) を必ず参照すること。

## 環境

- **開発**: この Mac。**本番**: Raspberry Pi 4 = `ssh homeserver`(自宅LAN内は mDNS、外出先は
  Tailscale に自動フォールバック。sudo はパスワード不要)
- 本番のリポジトリは Pi の `/opt/homeserver`(git checkout)。systemd サービス
  `switchbot.service`(BLE温湿度)/ `echonet.service`(電力)がここから動いている
- 実行時設定は `/opt/homeserver/.env`(**git 管理外**。SwitchBot トークン、BLE/ECHONET デバイス一覧、
  回路名。夜間の pCloud バックアップに含まれる)

## テスト

- `uv run pytest --cov` — カバレッジ下限(pyproject の fail_under)を割ると失敗する
- `shellcheck deploy/bin/*.sh scripts/*.sh`
- **バグ修正には再現テストを必ず同伴させる**(華氏デコード・絶対湿度10倍・D-Busリークは全部この流儀で直した)
- Grafana など UI と連携する変更は、**実機で動作確認してから完了報告する**
  (凡例トグルが固定オーバーライドと衝突して全消えした前科がある)

## デプロイ規約(最重要)

- main にコミット → `scripts/deploy-to-pi.sh` で Pi へ同期(`/deploy-pi` スキルあり)。
  **GitHub は経由しない**(bundle + scp 方式)
- **Pi 上の設定・ユニット・ダッシュボードを変えたら、必ず deploy/ に反映してコミットする**。
  deploy/ が SD カード故障時の唯一の復元元
- `deploy/bin/vm-backup.sh` は .env(認証情報)を外部へアップロードする処理を含むため、
  エージェントからの変更・配布は権限ブロックされることがある → その場合はユーザーに
  `scripts/deploy-to-pi.sh` の手動実行を依頼する

## git / GitHub

- **`git push` はユーザーが行う**(エージェントからの main 直 push はポリシーでブロックされる)。
  コミットまで済ませて push を依頼すること
- **`gh` CLI は仕事用アカウントで認証されている。この個人リポジトリへの書き込みに使わない**
  (読み取りは可)

## ドキュメント更新規約(作業完了の定義に含む)

コードや設定を変えたら、**コミット前に以下を自問し、古くなったドキュメントを同じコミットで更新する**。
「動いたら完了」ではなく「docs が現状を語っていたら完了」。

| 変更の種類 | 更新先 |
| --- | --- |
| CLI コマンド・環境変数・セットアップ手順・構成概観の変化 | README.md(冒頭の説明/Repository layout/Configuration 表/CLI 節) |
| 構成・サービス・機器・メトリクスの追加/変更 | docs/runbook.md(該当節と「機器一覧」の最終更新日) |
| 運用手順やハマりどころの発見 | docs/runbook.md の Tips |
| データ欠損・誤データを伴う障害、重要バグの根本原因判明 | docs/incidents/YYYY-MM-DD-*.md を新規作成(既存2件の形式を踏襲)。既知障害の続報は該当ファイルへ追記 |
| Pi 上の成果物(unit/スクリプト/ダッシュボード) | deploy/(前述のデプロイ規約) |
| エージェントの作業手順・規約・環境の変化 | この AGENTS.md 自身 |

軽微な表示調整などドキュメントに影響しない変更は対象外。迷ったら更新する側に倒す。

## データ操作の注意

- VictoriaMetrics の `delete_series` は**時間範囲指定不可・シリーズ全体が消える**。
  部分削除したい場合はエクスポート→削除→再インポートの手順を踏む
- 過去データの一括投入は `el import-history`(AiSEG2 履歴CSV)。ライブ収集との
  二重計上を防ぐため `--max-day` を必ず指定する
