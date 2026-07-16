"""
電気料金メトリクスの計算。

日次の kWh (買電/売電/自家消費) を VictoriaMetrics から取得し、tariff.py の
料金モデルで円に換算して書き戻す:

- cost_day_yen{kind="buy"|"savings"|"sell_income"}    ... 日次 (限界単価ベース)
- cost_period_yen{kind="bill"|"savings"|"sell_income"} ... 検針期間 (10日〆) 合計。
  bill は基本料金・定額込みの請求額見込み。タイムスタンプは期間開始日
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Tuple

from . import tariff

JST = timezone(timedelta(hours=9))

# ダッシュボードと同じハイブリッド式 (履歴 or ライブ積算差分)
DAILY_QUERIES = {
    "buy": 'sum(energy_day_kwh{kind="buy"}) or sum(increase(power_buy_total_kwh[1d] offset -1d))',
    "sell": 'sum(energy_day_kwh{kind="sell"}) or sum(increase(power_sell_total_kwh[1d] offset -1d))',
    "generation": 'sum(energy_day_kwh{kind="generation"}) or sum(increase(power_generation_total_kwh[1d] offset -1d))',
}


def day_ts_ms(day: date) -> int:
    return int(datetime(day.year, day.month, day.day, tzinfo=JST).timestamp() * 1000)


def fetch_daily_kwh(influx_url: str, start: date, end: date, timeout_s: float) -> Dict[date, Dict[str, float]]:
    """VM の query_range (JST 0時整列, step=1日) で日次 kWh を取得する。"""
    import requests

    daily: Dict[date, Dict[str, float]] = defaultdict(dict)
    for kind, query in DAILY_QUERIES.items():
        r = requests.get(
            f"{influx_url}/api/v1/query_range",
            params={
                "query": query,
                "start": day_ts_ms(start) // 1000,
                "end": day_ts_ms(end) // 1000,
                "step": 86400,
            },
            timeout=timeout_s,
        )
        r.raise_for_status()
        result = r.json()["data"]["result"]
        if not result:
            continue
        for ts, value in result[0]["values"]:
            day = datetime.fromtimestamp(int(ts), JST).date()
            daily[day][kind] = float(value)
    return dict(daily)


def build_cost_lines(daily: Dict[date, Dict[str, float]], today: date) -> Tuple[List[str], List[str]]:
    """日次 kWh からコスト行 (line protocol) と警告を作る。

    - 単価テーブルにない月はスキップ (警告)
    - 期間集計は「完結した期間で全日データあり」または「進行中の期間」のみ書く
    """
    lines: List[str] = []
    warnings: List[str] = []
    periods: Dict[str, Dict[str, float]] = defaultdict(lambda: defaultdict(float))
    period_days: Dict[str, int] = defaultdict(int)

    for day in sorted(daily):
        kwh = daily[day]
        month = tariff.billing_month(day)
        try:
            rate = tariff.marginal_buy_rate_yen(month)
        except KeyError as exc:
            warnings.append(str(exc))
            continue
        buy = kwh.get("buy")
        sell = kwh.get("sell")
        gen = kwh.get("generation")
        ts_ns = day_ts_ms(day) * 1_000_000
        fields = {}
        if buy is not None:
            fields["buy"] = buy * rate
            periods[month]["buy_kwh"] += buy
        if sell is not None:
            fields["sell_income"] = tariff.sell_income_yen(sell)
            periods[month]["sell_income"] += fields["sell_income"]
        if gen is not None and sell is not None:
            fields["savings"] = tariff.savings_yen(month, gen - sell)
            periods[month]["savings"] += fields["savings"]
        for kind, yen in fields.items():
            lines.append(f"cost_day,kind={kind} yen={round(yen, 2)} {ts_ns}")
        if buy is not None:
            period_days[month] += 1

    period_lines: List[str] = []
    for month, acc in sorted(periods.items()):
        start, end = tariff.billing_period(month)
        expected_end = min(end, today - timedelta(days=1))
        expected_days = (expected_end - start).days + 1
        ongoing = start <= today <= end
        if period_days[month] < expected_days and not ongoing:
            warnings.append(f"{month}: 期間内のデータが欠けているため期間集計をスキップ "
                            f"({period_days[month]}/{expected_days}日)")
            continue
        if period_days[month] == 0:
            continue
        ts_ns = day_ts_ms(start) * 1_000_000
        bill = tariff.period_bill_yen(month, acc["buy_kwh"])
        period_lines.append(f"cost_period,kind=bill,month={month} yen={round(bill, 2)} {ts_ns}")
        period_lines.append(f"cost_period,kind=savings,month={month} yen={round(acc['savings'], 2)} {ts_ns}")
        period_lines.append(f"cost_period,kind=sell_income,month={month} yen={round(acc['sell_income'], 2)} {ts_ns}")
    return lines + period_lines, warnings
