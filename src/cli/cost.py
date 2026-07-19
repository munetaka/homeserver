"""
電気料金メトリクスの計算。

日次の kWh (買電/売電/自家消費) を VictoriaMetrics から取得し、tariff.py の
料金モデルで円に換算して書き戻す:

- cost_day_yen{kind="buy"|"savings"|"sell_income"}    ... 日次 (限界単価ベース)
- cost_period_yen{kind="bill"|"savings"|"sell_income"} ... 検針期間 (10日〆) 合計。
  bill は基本料金・定額込みの請求額見込み。タイムスタンプは期間開始日
- cost_year_yen{kind, year} ... 暦年合計。savings/sell_income は日次の暦年合計、
  bill は「開始日がその暦年に属する検針期間」の請求合計。タイムスタンプは1月1日。
  保存済みメトリクスから毎回全年を再計算するため、cost-update の再計算窓に依存しない
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Tuple

from . import tariff

JST = timezone(timedelta(hours=9))

# energy_day_kwh は必ず明示窓 last_over_time(...[12h]) で読む。窓なしセレクタだと VM が
# サンプル間隔 (1日) から推定した猶予で最終サンプルを翌日の評価点へ持ち越すため、
# 系列終端の翌日に前日のコピーが現れる (docs/incidents/2026-07-20-*)。12h なら
# JST 0時打点のサンプルを JST 0時グリッドでも UTC 0時 (JST 9時) グリッドでも拾え、
# 前日分 (24h 以上前) は決して入らない。
# ダッシュボードと同じハイブリッド式 (実サンプル or ライブ積算差分)
DAILY_QUERIES = {
    "buy": 'sum(last_over_time(energy_day_kwh{kind="buy"}[12h])) or sum(increase(power_buy_total_kwh[1d] offset -1d))',
    "sell": 'sum(last_over_time(energy_day_kwh{kind="sell"}[12h])) or sum(increase(power_sell_total_kwh[1d] offset -1d))',
    "generation": 'sum(last_over_time(energy_day_kwh{kind="generation"}[12h])) or sum(increase(power_generation_total_kwh[1d] offset -1d))',
}

# 実体化: 確定日の日次kWhを積算メーターの増分から求める式 (JST カレンダー日ちょうど)
COUNTER_DAY_QUERIES = {
    "buy": "sum(increase(power_buy_total_kwh[1d]))",
    "sell": "sum(increase(power_sell_total_kwh[1d]))",
    "generation": "sum(increase(power_generation_total_kwh[1d]))",
}
DAY_KINDS = ("buy", "consumption", "generation", "sell")


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


def fetch_stored_day_kinds(influx_url: str, start: date, end: date, timeout_s: float) -> Dict[date, set]:
    """energy_day_kwh の実サンプルがある日を kind 別に返す (持ち越しを除外した実在判定)。"""
    import requests

    r = requests.get(
        f"{influx_url}/api/v1/query_range",
        params={
            "query": "count by (kind) (last_over_time(energy_day_kwh[12h]))",
            "start": day_ts_ms(start) // 1000,
            "end": day_ts_ms(end) // 1000,
            "step": 86400,
        },
        timeout=timeout_s,
    )
    r.raise_for_status()
    stored: Dict[date, set] = defaultdict(set)
    for series in r.json()["data"]["result"]:
        kind = series["metric"].get("kind", "")
        for ts, _ in series["values"]:
            stored[datetime.fromtimestamp(int(ts), JST).date()].add(kind)
    return dict(stored)


def build_day_kwh_lines(influx_url: str, start: date, end: date, timeout_s: float) -> Tuple[List[str], List[str]]:
    """energy_day_kwh が無い確定日を積算メーターから実体化する行 (line protocol) を作る。

    - end には「昨日」以前を渡すこと (進行中の日は確定しないので書かない)
    - 実サンプルがある日はスキップ (AiSEG2 インポートの公式値を正として保持)
    - 積算メーターに増分が取れない日 (ライブ収集開始前など) もスキップ
    """
    import requests

    stored = fetch_stored_day_kinds(influx_url, start, end, timeout_s)
    lines: List[str] = []
    warnings: List[str] = []
    day = start
    while day <= end:
        have = stored.get(day, set())
        if not have:
            values: Dict[str, float] = {}
            for kind, query in COUNTER_DAY_QUERIES.items():
                r = requests.get(
                    f"{influx_url}/api/v1/query",
                    params={"query": query, "time": day_ts_ms(day) // 1000 + 86400},
                    timeout=timeout_s,
                )
                r.raise_for_status()
                result = r.json()["data"]["result"]
                if result:
                    values[kind] = float(result[0]["value"][1])
            if len(values) == len(COUNTER_DAY_QUERIES):
                values["consumption"] = values["generation"] + values["buy"] - values["sell"]
                ts_ns = day_ts_ms(day) * 1_000_000
                for kind in DAY_KINDS:
                    lines.append(f"energy_day,kind={kind} kwh={round(values[kind], 3)} {ts_ns}")
            elif values:
                warnings.append(f"{day}: 積算メーターの増分が一部しか取れないため実体化をスキップ "
                                f"(取れた kind: {sorted(values)})")
        elif have < set(DAY_KINDS):
            warnings.append(f"{day}: energy_day_kwh が部分的にしか無い (要調査: {sorted(have)})")
        day += timedelta(days=1)
    return lines, warnings


FIRST_DATA_YEAR = 2025  # 家の稼働開始年。これより前の年は照会しない


def year_window_seconds(year: int) -> int:
    """暦年ぶんの sum_over_time 窓 (秒)。左端が排他なので1時間の余白を足し、
    1/1 00:00 打点のサンプルを確実に含める (翌年のサンプルは常に窓の右外)。"""
    start = datetime(year, 1, 1, tzinfo=JST)
    end = datetime(year + 1, 1, 1, tzinfo=JST)
    return int((end - start).total_seconds()) + 3600


def build_year_lines(influx_url: str, today: date, timeout_s: float) -> List[str]:
    """保存済みの cost_day/cost_period から暦年合計を計算して line protocol を返す。

    評価点は翌年1/1 0時 (進行中の年は未来時刻になるが、窓指定なので問題ない)。
    """
    import requests

    lines: List[str] = []
    for year in range(FIRST_DATA_YEAR, today.year + 1):
        eval_ts = int(datetime(year + 1, 1, 1, tzinfo=JST).timestamp())
        window = year_window_seconds(year)
        ts_ns = int(datetime(year, 1, 1, tzinfo=JST).timestamp() * 1000) * 1_000_000
        queries = {
            "savings": f'sum(sum_over_time(cost_day_yen{{kind="savings"}}[{window}s]))',
            "sell_income": f'sum(sum_over_time(cost_day_yen{{kind="sell_income"}}[{window}s]))',
            "bill": f'sum(sum_over_time(cost_period_yen{{kind="bill"}}[{window}s]))',
        }
        for kind, query in queries.items():
            r = requests.get(
                f"{influx_url}/api/v1/query",
                params={"query": query, "time": eval_ts},
                timeout=timeout_s,
            )
            r.raise_for_status()
            result = r.json()["data"]["result"]
            if result:
                yen = float(result[0]["value"][1])
                lines.append(f"cost_year,kind={kind},year={year} yen={round(yen, 2)} {ts_ns}")
    return lines


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
