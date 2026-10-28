"""
TEPCO 燃料費調整単価の自動取得 (`el tariff-fetch` の実体)。

一覧ページ (低圧・従量制・税込) を取得し、tariff.py 未登録の請求月を overlay
(var/fuel_adjustment.json) へ追記する。設計の要点:

- 一覧ページの掲載値は国の支援等の値引き「反映後」の適用単価。overlay には
  これをそのまま格納する (分解は tariff.rates_for 側。二重計上事故
  docs/incidents/2026-08-17-* の再発防止はこの「適用値を1つだけ持つ」構造で担保)
- 検証に通らない値は書かない。誤値で請求メトリクスを汚すより止まって
  tariff_check_ok=0 を立てて Grafana で赤くする
- overlay は追記のみ。既存月は上書きせず、公表値とズレたら警告に出す
- 一覧ページは UA によって 403 を返すためブラウザ UA を名乗る
"""

from __future__ import annotations

import html as html_mod
import json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List

from . import tariff

TEPCO_LIST_URL = "https://www.tepco.co.jp/ep/private/fuelcost2/newlist/index-j.html"
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 正気チェックの許容範囲 (円/kWh)。過去実績は -12.22〜-5.33、支援反映後でも -11 程度。
# 燃料価格高騰期 (2023) は +5 超があったため上側にも余裕を持たせる
VALUE_MIN_YEN, VALUE_MAX_YEN = -25.0, 15.0


def fetch_html(url: str, timeout_s: float) -> str:
    import requests

    r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=timeout_s)
    r.raise_for_status()
    return r.content.decode("utf-8", errors="replace")


def _cell_texts(row_html: str) -> List[str]:
    cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row_html, re.S)
    out = []
    for c in cells:
        text = html_mod.unescape(re.sub(r"<[^>]+>", " ", c))
        out.append(unicodedata.normalize("NFKC", re.sub(r"\s+", "", text)))
    return out


def parse_fuel_table(html: str) -> Dict[str, float]:
    """低圧・従量制の燃調一覧表から {請求月: 適用単価(税込)} を返す。

    表の見分け方: ヘッダに「適用年月」と「燃料費調整単価」を両方含む表はこの1つだけ。
    行は「YYYY年」(rowspan) +「N月分」+ 単価列で、単価列が複数ある場合は最後の列
    (電気需給約款[低圧] = 自由料金プラン = くらし上手に適用される側) を採る。
    """
    for table in re.findall(r"<table.*?</table>", html, re.S):
        plain = unicodedata.normalize("NFKC", re.sub(r"<[^>]+>", " ", table))
        if "適用年月" not in plain or "燃料費調整単価" not in plain:
            continue
        months: Dict[str, float] = {}
        year = None
        for row in re.findall(r"<tr.*?</tr>", table, re.S):
            month_num = None
            values: List[float] = []
            for cell in _cell_texts(row):
                if m := re.fullmatch(r"(\d{4})年", cell):
                    year = int(m.group(1))
                elif m := re.fullmatch(r"(\d{1,2})月分", cell):
                    month_num = int(m.group(1))
                elif m := re.fullmatch(r"([▲−-]?)(\d+(?:\.\d+)?)", cell):
                    sign = -1.0 if m.group(1) else 1.0  # ▲/−/- はいずれも負号
                    values.append(sign * float(m.group(2)))
                # 「-」(未公表) やヘッダ・注記セルは無視
            if year and month_num and values:
                months[f"{year:04d}-{month_num:02d}"] = values[-1]
        if months:
            return months
        raise ValueError("燃調一覧の表は見つかったが単価を1つも読めませんでした (ページ構成変更?)")
    raise ValueError("燃調一覧の表が見つかりません (ページ構成が変わった可能性)")


# ---------------------------------------------------------------------
# 検証と overlay 更新計画
# ---------------------------------------------------------------------

def _month_index(month: str) -> int:
    return int(month[:4]) * 12 + int(month[5:7])


def _next_month(month: str) -> str:
    idx = _month_index(month) + 1
    return f"{(idx - 1) // 12:04d}-{(idx - 1) % 12 + 1:02d}"


@dataclass
class UpdatePlan:
    added: Dict[str, float] = field(default_factory=dict)   # overlay へ追記する月
    rejected: List[str] = field(default_factory=list)       # 検証不合格 (書かない理由)
    mismatches: List[str] = field(default_factory=list)     # 登録済みの値と公表値の不一致


def plan_overlay_update(
    published: Dict[str, float],
    code_fuel: Dict[str, float],
    code_subsidy: Dict[str, float],
    overlay: Dict[str, float],
) -> UpdatePlan:
    """公表値を検証し、overlay へ追記すべき月を決める (純粋関数)。

    - 登録済みの月は照合のみ: 定数は fuel+subsidy を反映後に換算して比較する
      (手入力のタイポ検出を兼ねる)。overlay 月は追記のみ方針なので上書きしない
    - 新しい月は「登録済み最大月から連続」かつ正気範囲内のときだけ追加する
    """
    plan = UpdatePlan()
    for month in sorted(set(published) & set(code_fuel)):
        expected = code_fuel[month] + code_subsidy.get(month, 0.0)
        if abs(published[month] - expected) > 0.005:
            plan.mismatches.append(
                f"{month}: 公表値 {published[month]} != 登録値 {expected:.2f} (fuel+subsidy)")
    for month in sorted(set(published) & set(overlay) - set(code_fuel)):
        if abs(published[month] - overlay[month]) > 0.005:
            plan.mismatches.append(
                f"{month}: 公表値 {published[month]} != overlay {overlay[month]} (追記のみ方針のため保持)")

    floor = min(code_fuel)  # これより前はデータ開始前の月なので対象外
    known_max = max(set(code_fuel) | set(overlay))
    cursor = known_max
    for month in sorted(set(published) - set(code_fuel) - set(overlay)):
        if month < floor:
            continue
        if month <= known_max:
            plan.rejected.append(f"{month}: 登録済み最大月 {known_max} 以前の未登録月 (テーブルの穴? 要手動確認)")
            continue
        if month != _next_month(cursor):
            plan.rejected.append(f"{month}: {cursor} と連続していないため追記しない")
            continue
        if not (VALUE_MIN_YEN <= published[month] <= VALUE_MAX_YEN):
            plan.rejected.append(
                f"{month}: {published[month]} が正気範囲 [{VALUE_MIN_YEN}, {VALUE_MAX_YEN}] 外")
            continue
        plan.added[month] = published[month]
        cursor = month
    return plan


# ---------------------------------------------------------------------
# overlay I/O とメトリクス
# ---------------------------------------------------------------------

def append_overlay(path: Path, added: Dict[str, float], fetched_at: str) -> None:
    """overlay へ追記する (既存キーは一切変更しない)。tmp 書き → rename で原子的に。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, json.JSONDecodeError):
        data = {}
    for month, applied in added.items():
        data.setdefault(month, {"applied_yen": applied, "fetched_at": fetched_at})
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                   encoding="utf-8")
    os.replace(tmp, path)


def months_ahead(today: date) -> int:
    """登録済み最大月が今日の請求月より何ヶ月先か (負 = 既に料金グラフが止まっている)。"""
    return _month_index(max(tariff.registered_months())) - _month_index(tariff.billing_month(today))


def recent_applied_rates(today: date, back: int = 2) -> Dict[str, tuple[float, str]]:
    """ダッシュボードの単価テーブル用に {請求月: (適用単価, 出どころ)} を返す。

    範囲は「現在の請求月 - back」〜登録済み最大月。定数側は fuel+subsidy を
    反映後 (適用値) に換算して overlay と同じ土俵に揃える。
    """
    overlay = tariff.load_overlay()
    start = _month_index(tariff.billing_month(today)) - back
    rates: Dict[str, tuple[float, str]] = {}
    for month in sorted(tariff.registered_months()):
        if _month_index(month) < start:
            continue
        if month in tariff.FUEL_ADJUSTMENT_YEN:
            applied = tariff.FUEL_ADJUSTMENT_YEN[month] + tariff.SUBSIDY_YEN.get(month, 0.0)
            rates[month] = (round(applied, 2), "code")
        else:
            rates[month] = (overlay[month], "overlay")
    return rates


def build_metric_lines(check_ok: bool, ahead: int, mismatch_count: int,
                       applied_rates: Dict[str, tuple[float, str]], ts_ms: int) -> List[str]:
    ts_ns = ts_ms * 1_000_000
    lines = [f"tariff check_ok={int(check_ok)}i,months_ahead={ahead}i,"
             f"code_mismatch={mismatch_count}i {ts_ns}"]
    for month, (applied, source) in sorted(applied_rates.items()):
        lines.append(f"tariff_fuel,month={month},source={source} applied_yen={applied} {ts_ns}")
    return lines
