---
name: deploy-pi
description: コミット済みの main をラズパイ (ssh homeserver) の /opt/homeserver へ同期し、deploy/ 配下の成果物の配置・必要なサービス再起動・動作検証まで行う。「Piにデプロイ」「ラズパイに反映」「デプロイして」と言われたときに使う。
---

# Pi へのデプロイ

## 手順

1. デプロイ対象が **main にコミット済み**であることを確認する(`git status`)。
   未コミット変更はデプロイされない(スクリプトが警告を出す)。
2. `chmod +x scripts/deploy-to-pi.sh` 済みの前提で実行:
   ```bash
   scripts/deploy-to-pi.sh
   ```
3. 出力を読んで報告する: 同期コミット(`synced:`)、配置されたファイル(`installed:`)、
   再起動されたサービス(`restarted:`)、最終行の `active?:` が全て active であること。

## 実行後の検証(変更内容に応じて)

- **コレクター(src/)を変更した場合**: 再起動後60〜90秒待ち、
  `ssh homeserver 'sudo journalctl -u switchbot.service -u echonet.service -n 4 --no-pager'`
  で `wrote N points`(switchbot: 10前後 / echonet: 31)を確認する。
- **ダッシュボード JSON を変更した場合**: Grafana が数十秒以内に自動リロードする。
  `curl -u admin:<pw> http://localhost:3000/api/dashboards/uid/<uid>`(Pi上)で
  変更が反映されたことを確認する。
- **watchdog / backup スクリプトを変更した場合**: `sudo bash -n` 済みだが、
  `sudo /usr/local/bin/collector-watchdog.sh` のドライラン(発火しないこと)を確認する。

## 注意

- `deploy/bin/vm-backup.sh` の変更を含むデプロイは、エージェント実行だと権限ブロック
  されることがある(.env を外部アップロードする処理を含むため)。ブロックされたら
  ユーザーに `scripts/deploy-to-pi.sh` の手動実行を依頼する。
- Pi 側を直接変更した場合は逆方向の同期を忘れない: 変更を deploy/ に取り込んでコミットする。
- GitHub への push はこのスキルの範囲外(ユーザーが行う)。
