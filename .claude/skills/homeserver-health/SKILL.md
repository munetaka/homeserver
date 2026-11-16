---
name: homeserver-health
description: ラズパイのサービス・タイマー・データ鮮度・ディスク・バックアップ・未プッシュ状態を一括チェックして表で報告する。「ヘルスチェック」「調子どう?」「システム状態を確認」と言われたとき、および障害調査の初手として使う。
---

# ヘルスチェック

1. 実行: `scripts/health-check.sh`(読み取りのみ・安全)。判定は Pi 上の `health check`
   (`/healthz` と同じ判定)。終了コード 0 = 全項目 OK、1 = NG あり、255 = Pi に ssh できない
2. 結果を表に整形して報告する。`NG` の行が異常。項目ごとの対応:
   - services に `active` 以外がある → 該当ユニットの journalctl を確認
   - climate / power の鮮度が 600s 超 → watchdog が次の5分周期で自動復旧するはず。1200s 超なら
     watchdog 自体を疑う(`sudo journalctl -u collector-watchdog.service`)
   - disk 90% 以上で NG(80% 超から注意) → `sudo du -xh --max-depth=1 / | sort -rh | head` で犯人特定
     (前例: docs/incidents/2026-07-05-influxdb-scraper-bloat.md)
   - CPU温度 70℃ 超 → ケース通気を確認
   - backup が NG(前回失敗、または36時間以上走っていない)→ `sudo journalctl -u vm-backup.service` を確認
   - cost が NG → `journalctl -u cost-update.service`(単価未登録で日次をスキップしている疑い)
   - 未プッシュが多い → ユーザーに `git push origin main` を依頼
3. 深掘りが必要な場合は docs/runbook.md の「障害対応手順」に従う。
