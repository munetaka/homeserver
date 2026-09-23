"""快適マップのゾーンポリゴン生成。

領域の定義: PMV(ISO 7730) が -0.5〜+0.5 (温熱快適) かつ RH 35〜60% (衛生:
乾燥・カビダニ) かつ 絶対湿度 <= 12.5 g/m3 (蒸し暑さ/結露リスクの上限)。
左右の辺が PMV 等値線 (負の傾き)、上下の辺が RH 線になる。

使い方: uv run python scripts/gen_comfort_zone.py <clo> <met> <x列名> <y列名>
出力: Grafana TestData の csv_content にそのまま貼れる CSV (閉じたポリライン)。
仮定は comfort.pmv の既定 (放射温度=室温, 気流0.1m/s)。
"""

import math
import sys

from cli.comfort import pmv

RH_LOW, RH_HIGH, AH_CAP = 35.0, 60.0, 12.5


def ah_sat(t: float) -> float:
    es = 6.1094 * math.exp(17.625 * t / (t + 243.04))  # hPa (Magnus)
    return 216.7 * es / (t + 273.15)


def pmv_at(t: float, ah: float, clo: float, met: float) -> float:
    rh = min(100.0, 100.0 * ah / ah_sat(t))
    v = pmv(t, rh, met=met, clo=clo)
    if v is None:  # クランプ外は温度で符号を決める (境界探索用)
        return -4.0 if t < 24 else 4.0
    return v


def ah_bounds(t: float, clo: float, met: float):
    """温度 t での AH の下限・上限 (領域が無ければ None)。"""
    lo_h, hi_h = RH_LOW / 100 * ah_sat(t), min(RH_HIGH / 100 * ah_sat(t), AH_CAP)
    if lo_h >= hi_h:
        return None

    def solve(target: float) -> float:
        # pmv は AH について単調増加。境界 AH を二分法で求める
        a, b = 0.05, 30.0
        fa = pmv_at(t, a, clo, met) - target
        fb = pmv_at(t, b, clo, met) - target
        if fa >= 0:
            return a       # 乾いていても target より暖かい → 下限なし
        if fb <= 0:
            return b       # 湿らせても target に届かない → 全域が下/上
        for _ in range(60):
            mid = (a + b) / 2
            if pmv_at(t, mid, clo, met) - target < 0:
                a = mid
            else:
                b = mid
        return (a + b) / 2

    lo = max(lo_h, solve(-0.5))   # 寒い側: PMV >= -0.5 に必要な最小 AH
    hi = min(hi_h, solve(+0.5))   # 暑い側: PMV <= +0.5 でいられる最大 AH
    if lo >= hi:
        return None
    return lo, hi


def polygon(clo: float, met: float, step: float = 0.25):
    ts = [10 + i * step for i in range(int(20 / step) + 1)]
    spans = [(t, ah_bounds(t, clo, met)) for t in ts]
    spans = [(t, b) for t, b in spans if b]
    if not spans:
        raise SystemExit("領域が空です")
    lower = [(t, b[0]) for t, b in spans]
    upper = [(t, b[1]) for t, b in reversed(spans)]
    pts = lower + upper + [lower[0]]
    # ほぼ直線上の中間点を間引く (CSV を短く保つ)
    out = [pts[0]]
    for p in pts[1:-1]:
        q, r = out[-1], pts[pts.index(p) + 1]
        # 前後の点を結ぶ線からの外れが小さければ捨てる
        if abs((r[1] - q[1]) * (p[0] - q[0]) - (p[1] - q[1]) * (r[0] - q[0])) > 0.02:
            out.append(p)
    out.append(pts[-1])
    return out


def main():
    clo, met = float(sys.argv[1]), float(sys.argv[2])
    xname, yname = sys.argv[3], sys.argv[4]
    pts = polygon(clo, met)
    print(f"{xname},{yname}")
    for t, ah in pts:
        print(f"{round(t, 2)},{round(ah, 2)}")


if __name__ == "__main__":
    main()
