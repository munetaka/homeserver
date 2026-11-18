# healthz-monitor — /healthz の外部監視 Worker

5分ごとの Cron で Pi の `/healthz`(`healthz.service`)を Cloudflare Tunnel + Access の
サービストークン経由で確認し、**20分以上 NG が続いたら Slack に通知**、復旧したらもう一度通知する。
20分の猶予は watchdog の自動復旧(10分)で直る障害では鳴らさないため。
NG には Pi の 503 だけでなく、Access の拒否(401/403)、Tunnel 断(530)、タイムアウトも含む。

- 判定ロジック: `src/monitor.js`(純粋関数)。テストは `node --test`(依存パッケージなし。CI でも実行)
- 状態: KV(binding `STATE`)の `state` キー 1 つ。変化したときだけ書く
- HTTP では公開しない(`workers_dev = false`)。Cron だけで動く

## secret(リポジトリに書かない)

| 名前 | 中身 |
| --- | --- |
| `HEALTHZ_URL` | `https://<healthz のホスト名>/healthz` |
| `CF_ACCESS_CLIENT_ID` / `CF_ACCESS_CLIENT_SECRET` | Access のサービストークン `healthz-monitor`(パスワードマネージャーに保管) |
| `SLACK_WEBHOOK_URL` | 個人用 Slack ワークスペースの Incoming Webhook の URL |

## デプロイ(このディレクトリで)

wrangler はバージョンを固定して npx で使う(グローバルには入れない)。
匿名テレメトリを送らないよう、先に `export WRANGLER_SEND_METRICS=false` しておく。

```bash
npx wrangler@4.157.0 login                                   # 初回のみ。ブラウザで許可
npx wrangler@4.157.0 kv namespace create healthz-monitor-state   # 初回のみ。出力の id を wrangler.toml へ
npx wrangler@4.157.0 deploy
npx wrangler@4.157.0 secret put HEALTHZ_URL                  # 4 つとも対話入力 (画面に出さない)
npx wrangler@4.157.0 secret put CF_ACCESS_CLIENT_ID
npx wrangler@4.157.0 secret put CF_ACCESS_CLIENT_SECRET
npx wrangler@4.157.0 secret put SLACK_WEBHOOK_URL
```

ログは `npx wrangler@4.157.0 tail` か、ダッシュボードの Workers & Pages → healthz-monitor → Logs。
サービストークンを作り直したら(期限 1 年)、2 つの `CF_ACCESS_*` を `secret put` し直す。
