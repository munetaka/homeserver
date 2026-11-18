import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";

import { runCheck } from "../src/index.js";
import { GRACE_MS, INITIAL_STATE, decide, isHealthy, message, sameState } from "../src/monitor.js";

const MIN = 60 * 1000;

describe("isHealthy", () => {
  it("200 で本文 OK だけが OK", () => {
    assert.equal(isHealthy(200, "OK\n"), true);
    assert.equal(isHealthy(503, "NG\n"), false);
    assert.equal(isHealthy(200, "<html>login</html>"), false); // Access のログイン画面など
    assert.equal(isHealthy(403, "Forbidden"), false);
    assert.equal(isHealthy(530, ""), false); // Tunnel 断
  });
});

describe("decide", () => {
  it("OK が続く間は何もしない", () => {
    const r = decide(null, true, 0);
    assert.deepEqual(r, { state: INITIAL_STATE, notify: null });
  });

  it("猶予 (20分) 未満の NG では鳴らさない", () => {
    let r = decide(null, false, 0);
    assert.deepEqual(r.state, { failingSince: 0, notified: false });
    r = decide(r.state, false, GRACE_MS - 1);
    assert.equal(r.notify, null);
    assert.equal(r.state.failingSince, 0);
  });

  it("猶予を越えたら 1 回だけ down を送る", () => {
    let r = decide({ failingSince: 0, notified: false }, false, GRACE_MS);
    assert.equal(r.notify, "down");
    assert.equal(r.downForMs, GRACE_MS);
    r = decide(r.state, false, GRACE_MS + 5 * MIN);
    assert.equal(r.notify, null); // 2 回目は送らない
    assert.equal(r.state.notified, true);
  });

  it("通知後に復旧したら up と NG だった時間を返す", () => {
    const r = decide({ failingSince: 0, notified: true }, true, 35 * MIN);
    assert.equal(r.notify, "up");
    assert.equal(r.downForMs, 35 * MIN);
    assert.deepEqual(r.state, INITIAL_STATE);
  });

  it("通知前に自然に直ったら黙ってリセットする (watchdog の自動復旧)", () => {
    const r = decide({ failingSince: 0, notified: false }, true, 10 * MIN);
    assert.equal(r.notify, null);
    assert.deepEqual(r.state, INITIAL_STATE);
  });
});

describe("sameState / message", () => {
  it("null は初期状態と同じ扱い", () => {
    assert.equal(sameState(null, INITIAL_STATE), true);
    assert.equal(sameState({ failingSince: 1, notified: false }, INITIAL_STATE), false);
  });

  it("本文に分数と詳細が入る", () => {
    assert.match(message("down", 20 * MIN, "HTTP 503"), /20分 以上 NG.*HTTP 503/);
    assert.match(message("up", 35 * MIN), /約 35分/);
  });
});

describe("runCheck", () => {
  const realFetch = globalThis.fetch;
  afterEach(() => {
    globalThis.fetch = realFetch;
  });

  function fakeEnv(initial) {
    const store = new Map(initial ? [["state", JSON.stringify(initial)]] : []);
    const puts = [];
    return {
      HEALTHZ_URL: "https://healthz.example.test/healthz",
      CF_ACCESS_CLIENT_ID: "id",
      CF_ACCESS_CLIENT_SECRET: "secret",
      SLACK_WEBHOOK_URL: "https://hooks.slack.test/x",
      STATE: {
        async get(key, type) {
          const v = store.get(key);
          return v === undefined ? null : type === "json" ? JSON.parse(v) : v;
        },
        async put(key, value) {
          puts.push(value);
          store.set(key, value);
        },
      },
      puts,
      store,
    };
  }

  function fakeFetch({ healthz, slackStatus = 200 }) {
    const calls = [];
    globalThis.fetch = async (url, init = {}) => {
      calls.push({ url, init });
      if (url.includes("hooks.slack")) return new Response("ok", { status: slackStatus });
      if (healthz instanceof Error) throw healthz;
      return new Response(healthz.body, { status: healthz.status });
    };
    return calls;
  }

  it("OK のときはサービストークンを付けて叩き、KV に書かない", async () => {
    const env = fakeEnv();
    const calls = fakeFetch({ healthz: { status: 200, body: "OK\n" } });
    const r = await runCheck(env, 0);
    assert.equal(r.healthy, true);
    assert.equal(calls.length, 1);
    assert.equal(calls[0].init.headers["CF-Access-Client-Id"], "id");
    assert.equal(calls[0].init.headers["CF-Access-Client-Secret"], "secret");
    assert.equal(calls[0].init.redirect, "manual");
    assert.equal(env.puts.length, 0);
  });

  it("NG の始まりだけ KV に書き、猶予を越えたら Slack に送る", async () => {
    const env = fakeEnv();
    const calls = fakeFetch({ healthz: { status: 503, body: "NG\n" } });
    await runCheck(env, 0);
    assert.equal(env.puts.length, 1);
    await runCheck(env, 5 * MIN);
    assert.equal(env.puts.length, 1); // 状態が変わらなければ書かない
    const r = await runCheck(env, GRACE_MS);
    assert.equal(r.notified, "down");
    const slack = calls.filter((c) => c.url.includes("hooks.slack"));
    assert.equal(slack.length, 1);
    assert.match(JSON.parse(slack[0].init.body).text, /HTTP 503/);
    assert.deepEqual(JSON.parse(env.store.get("state")), { failingSince: 0, notified: true });
  });

  it("接続できないのも NG として扱う", async () => {
    const env = fakeEnv({ failingSince: 0, notified: false });
    fakeFetch({ healthz: new TypeError("network") });
    const r = await runCheck(env, GRACE_MS);
    assert.equal(r.healthy, false);
    assert.equal(r.detail, "接続失敗");
    assert.equal(r.notified, "down");
  });

  it("Slack に送れなかったら状態を進めず、次回また送る", async () => {
    const env = fakeEnv({ failingSince: 0, notified: false });
    fakeFetch({ healthz: { status: 503, body: "NG\n" }, slackStatus: 500 });
    const r = await runCheck(env, GRACE_MS);
    assert.equal(r.notified, false);
    assert.deepEqual(JSON.parse(env.store.get("state")), { failingSince: 0, notified: false });
  });

  it("通知後に復旧したら up を送って状態を戻す", async () => {
    const env = fakeEnv({ failingSince: 0, notified: true });
    const calls = fakeFetch({ healthz: { status: 200, body: "OK\n" } });
    const r = await runCheck(env, 40 * MIN);
    assert.equal(r.notified, "up");
    assert.match(JSON.parse(calls[1].init.body).text, /復旧/);
    assert.deepEqual(JSON.parse(env.store.get("state")), INITIAL_STATE);
  });
});
