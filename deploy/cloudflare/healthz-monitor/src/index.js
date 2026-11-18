// 5分ごとの Cron で Pi の /healthz を Cloudflare Tunnel + Access のサービストークン経由で確認し、
// 20分以上 NG が続いたら Slack に通知、復旧したらもう一度通知する。
//
// 公開ドメインやトークンはコードにも wrangler.toml にも書かず、すべて secret で渡す:
//   HEALTHZ_URL              https://<healthz のホスト名>/healthz
//   CF_ACCESS_CLIENT_ID      Access のサービストークン (healthz-monitor)
//   CF_ACCESS_CLIENT_SECRET  同上
//   SLACK_WEBHOOK_URL        Slack の Incoming Webhook の URL
// 状態は KV (binding: STATE) の 1 キーに置き、変化したときだけ書く (無料枠の書き込み上限に触れない)。

import { decide, isHealthy, message, sameState } from "./monitor.js";

const STATE_KEY = "state";
const TIMEOUT_MS = 15000;

async function probe(env) {
  try {
    const res = await fetch(env.HEALTHZ_URL, {
      headers: {
        "CF-Access-Client-Id": env.CF_ACCESS_CLIENT_ID,
        "CF-Access-Client-Secret": env.CF_ACCESS_CLIENT_SECRET,
      },
      redirect: "manual", // Access のログイン画面への転送を OK と取り違えない
      signal: AbortSignal.timeout(TIMEOUT_MS),
    });
    const body = await res.text();
    return { healthy: isHealthy(res.status, body), detail: `HTTP ${res.status}` };
  } catch (e) {
    return { healthy: false, detail: e?.name === "TimeoutError" ? "タイムアウト" : "接続失敗" };
  }
}

async function notifySlack(env, text) {
  const res = await fetch(env.SLACK_WEBHOOK_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ text }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`Slack webhook HTTP ${res.status}`);
}

export async function runCheck(env, now = Date.now()) {
  const prev = await env.STATE.get(STATE_KEY, "json");
  const { healthy, detail } = await probe(env);
  const next = decide(prev, healthy, now);
  if (next.notify) {
    try {
      await notifySlack(env, message(next.notify, next.downForMs, detail));
    } catch (e) {
      // 送れなかったら状態を進めない (次の 5 分後にもう一度送る)
      console.error(`notify ${next.notify} failed: ${e.message}`);
      return { healthy, detail, notified: false };
    }
  }
  if (!sameState(prev, next.state)) {
    await env.STATE.put(STATE_KEY, JSON.stringify(next.state));
  }
  console.log(`healthz ${healthy ? "OK" : "NG"} (${detail})${next.notify ? ` notify=${next.notify}` : ""}`);
  return { healthy, detail, notified: next.notify };
}

export default {
  async scheduled(_event, env, ctx) {
    ctx.waitUntil(runCheck(env));
  },
};
