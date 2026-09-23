"""
温熱快適指標 PMV (Predicted Mean Vote, Fanger) の計算。

ISO 7730:2005 Annex D の参照アルゴリズム (BASIC コード) の移植。
-3 (寒い) 〜 +3 (暑い)、|PMV| <= 0.5 が快適域 (ISO 7730 カテゴリB)。

この家 (C値0.14 / UA値0.26 の高断熱) では壁・窓の表面温度が室温にほぼ一致する
ため、放射温度 tr = 室温 ta とみなす。気流は 0.1 m/s (空調の弱い気流) を既定とする。

コレクター (sb) が部屋ごと・想定シナリオごとに comfort_pmv{location, clo, met}
として保存する。シナリオは COMBOS を参照。
"""

from __future__ import annotations

import math
from typing import Optional

# 保存する想定シナリオ: (clo=着衣量, met=代謝量)
# clo 0.5=夏服 / 1.0=冬服。met 1.1=デスクワーク (座位・軽作業) / 2.0=家事 (ISO 7730 の
# 掃除・炊事等の家庭内作業相当)。
# 就寝シナリオは met 0.7 (ASHRAE/ISO の代謝表の睡眠値。ISO 7730 の正式適用範囲は
# 0.8met〜なので参考値扱い)、clo は寝具込みの総断熱で 1.4=夏寝具 (パジャマ+タオルケット
# 程度) / 2.6=冬寝具 (パジャマ+羽毛布団程度)。睡眠温熱研究の典型値 (1〜3clo) からの仮置き
COMBOS = (
    (0.5, 1.1), (0.5, 2.0),   # 夏服
    (1.0, 1.1), (1.0, 2.0),   # 冬服
    (1.4, 0.7), (2.6, 0.7),   # 就寝 (夏寝具 / 冬寝具)
)

# PMV 式の適用範囲のめやす (ISO 7730 6.4.2)。範囲外は None を返して保存しない
_TA_RANGE = (10.0, 30.0)
_PMV_CLAMP = 3.5


def pmv(
    ta: float,
    rh: float,
    tr: Optional[float] = None,
    vel: float = 0.1,
    met: float = 1.1,
    clo: float = 0.5,
    wme: float = 0.0,
) -> Optional[float]:
    """PMV を返す。収束しない/適用範囲外は None。

    ta: 空気温度℃, rh: 相対湿度%, tr: 平均放射温度℃ (省略時 ta),
    vel: 相対気流 m/s, met: 代謝量 met, clo: 着衣量 clo, wme: 外部仕事 met
    """
    if not (_TA_RANGE[0] <= ta <= _TA_RANGE[1]) or not (0 <= rh <= 100):
        return None
    if tr is None:
        tr = ta

    pa = rh * 10 * math.exp(16.6536 - 4030.183 / (ta + 235))  # 水蒸気分圧 Pa
    icl = 0.155 * clo          # 衣服の熱抵抗 m2K/W
    m = met * 58.15            # 代謝量 W/m2
    w = wme * 58.15
    mw = m - w
    if icl <= 0.078:
        fcl = 1 + 1.29 * icl   # 着衣面積率
    else:
        fcl = 1.05 + 0.645 * icl
    hcf = 12.1 * math.sqrt(vel)  # 強制対流の熱伝達率
    taa = ta + 273
    tra = tr + 273

    # 衣服表面温度 tcl を反復で解く
    tcla = taa + (35.5 - ta) / (3.5 * icl + 0.1)
    p1 = icl * fcl
    p2 = p1 * 3.96
    p3 = p1 * 100
    p4 = p1 * taa
    p5 = 308.7 - 0.028 * mw + p2 * (tra / 100) ** 4
    xn = tcla / 100
    xf = tcla / 50
    hc = hcf
    n = 0
    while abs(xn - xf) > 0.00015:
        xf = (xf + xn) / 2
        hcn = 2.38 * abs(100 * xf - taa) ** 0.25  # 自然対流
        hc = max(hcf, hcn)
        xn = (p5 + p4 * hc - p2 * xf**4) / (100 + p3 * hc)
        n += 1
        if n > 150:
            return None
    tcl = 100 * xn - 273

    # 熱損失の内訳
    hl1 = 3.05 * 0.001 * (5733 - 6.99 * mw - pa)          # 皮膚からの拡散
    hl2 = 0.42 * (mw - 58.15) if mw > 58.15 else 0.0      # 発汗
    hl3 = 1.7 * 1e-5 * m * (5867 - pa)                    # 呼吸 (潜熱)
    hl4 = 0.0014 * m * (34 - ta)                          # 呼吸 (顕熱)
    hl5 = 3.96 * fcl * (xn**4 - (tra / 100) ** 4)         # 放射
    hl6 = fcl * hc * (tcl - ta)                           # 対流

    ts = 0.303 * math.exp(-0.036 * m) + 0.028
    value = ts * (mw - hl1 - hl2 - hl3 - hl4 - hl5 - hl6)
    if abs(value) > _PMV_CLAMP:
        return None
    return value
