// /healthz の外部監視の判定ロジック (Workers のランタイムに依存しない純粋関数。node --test で検証する)。
//
// state = { failingSince: number|null, notified: boolean }
//   failingSince: NG が続いている最初の時刻 (ms)。OK なら null
//   notified:     その NG 期間について「停止」通知を送り済みか

export const GRACE_MS = 20 * 60 * 1000; // watchdog の自動復旧 (10分) で直る障害では鳴らさない
// Cron の起動時刻は毎回数秒〜十数秒ずれる。ちょうど 20 分で比べると 4 回目の判定が
// 「19分59秒」になって 1 周 (5分) 遅れるので、1 分の余裕を持たせる
export const JITTER_MS = 60 * 1000;

export const INITIAL_STATE = Object.freeze({ failingSince: null, notified: false });

// /healthz の応答を OK/NG に。200 かつ本文が "OK" のときだけ OK
// (Access に弾かれた 401/403、Tunnel 断の 530、Pi の 503 NG、タイムアウトはすべて NG)
export function isHealthy(status, body) {
  return status === 200 && typeof body === "string" && body.trim() === "OK";
}

// 次の状態と、送るべき通知 ("down" | "up" | null) を返す
export function decide(state, healthy, now, graceMs = GRACE_MS) {
  const s = state ?? INITIAL_STATE;
  if (healthy) {
    if (s.notified) {
      return { state: INITIAL_STATE, notify: "up", downForMs: now - s.failingSince };
    }
    return { state: INITIAL_STATE, notify: null };
  }
  const failingSince = s.failingSince ?? now;
  if (!s.notified && now - failingSince >= graceMs - JITTER_MS) {
    return { state: { failingSince, notified: true }, notify: "down", downForMs: now - failingSince };
  }
  return { state: { failingSince, notified: s.notified }, notify: null };
}

export function sameState(a, b) {
  const x = a ?? INITIAL_STATE;
  const y = b ?? INITIAL_STATE;
  return x.failingSince === y.failingSince && x.notified === y.notified;
}

export function formatMinutes(ms) {
  return `${Math.round(ms / 60000)}分`;
}

// Slack に送る本文 (ホスト名や家の情報は入れない)
export function message(kind, downForMs, detail) {
  if (kind === "down") {
    return `:red_circle: homeserver の /healthz が ${formatMinutes(downForMs)} 以上 NG です (${detail})。` +
      "Mac から scripts/health-check.sh で NG の項目を確認してください。";
  }
  return `:large_green_circle: homeserver の /healthz が復旧しました (NG だった時間: 約 ${formatMinutes(downForMs)})。`;
}
