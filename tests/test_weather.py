"""気象取り込み (Open-Meteo / 気象庁MSM) のテスト。"""

from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from cli import echonet
from cli import weather as wx

VM = "http://vm.test:8428"
H = 3600
T0 = 1793095200  # 2026-10-27 01:00 UTC (正時)
DELETE_URL = f"{VM}/api/v1/admin/tsdb/delete_series"


def _payload(times, **series):
    hourly = {"time": list(times)}
    for var in wx.FIELDS:
        hourly[var] = series.get(var, [1.0] * len(times))
    return {"latitude": 35.7, "longitude": 139.75, "hourly": hourly}


def _vm_last(rsps, ts):
    result = [] if ts is None else [{"metric": {}, "value": [0, str(ts)]}]
    rsps.get(f"{VM}/api/v1/query", json={"data": {"result": result}})


def _meta(rsps, init, available):
    rsps.get(wx.META_URL, json={"last_run_initialisation_time": init, "last_run_availability_time": available,
                                "update_interval_seconds": 10800})


def _ts(line: str) -> int:
    return int(line.rsplit(" ", 1)[1]) // 10**9


class TestParse:
    def test_maps_fields_and_drops_nulls(self):
        rows = wx.parse_hourly(_payload(
            [T0, T0 + H],
            temperature_2m=[18.1, None],
            precipitation=[0.5, 0.0],
            shortwave_radiation=[None, None],
        ))
        assert rows[T0]["temp_c"] == 18.1
        assert rows[T0]["precip_mm"] == 0.5
        assert "temp_c" not in rows[T0 + H]
        assert "ghi_wm2" not in rows[T0]  # null は書かない (0 と区別する)

    def test_row_with_all_nulls_is_dropped(self):
        nulls = {var: [None] for var in wx.FIELDS}
        assert wx.parse_hourly(_payload([T0], **nulls)) == {}

    def test_rejects_payload_without_hourly(self):
        with pytest.raises(ValueError):
            wx.parse_hourly({"error": True, "reason": "Cannot initialize JMA MSM"})


class TestWindow:
    NOW = datetime(2026, 10, 29, 20, 23, tzinfo=timezone(timedelta(hours=9)))  # 11:23 UTC
    FIXED = int(datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc).timestamp())  # 11:23 - 6h → 05:00

    def test_settled_until_is_six_hours_back_on_the_hour(self):
        assert wx.settled_until(self.NOW) == self.FIXED

    def test_settled_until_not_capped_when_latest_run_is_on_time(self):
        # 06:00 UTC ラン (公開 09:31) が取り込み済み → 06:00〜08:00 まで確定なので固定の 05:00 が勝つ
        run = wx.ModelRun(init=self.FIXED + H, available=self.FIXED + 4 * H + 1860)
        assert wx.settled_until(self.NOW, run) == self.FIXED

    def test_settled_until_waits_for_a_late_run(self):
        # 最新が 00:00 UTC ラン (03:00 ランが遅延中) → 02:00 までしか確定していない
        run = wx.ModelRun(init=self.FIXED - 5 * H, available=self.FIXED - 2 * H)
        assert wx.settled_until(self.NOW, run) == self.FIXED - 3 * H

    def test_settled_until_treats_a_just_published_run_as_the_previous_one(self):
        # 公開から 10 分未満は取り込み途中かもしれないので、ひとつ前のラン (00:00) で判定 → 02:00
        now_ts = int(self.NOW.timestamp())
        run = wx.ModelRun(init=self.FIXED - 2 * H, available=now_ts - 300)
        assert wx.settled_until(self.NOW, run) == self.FIXED - 3 * H
        run = wx.ModelRun(init=self.FIXED - 2 * H, available=now_ts - wx.META_GRACE_S)
        assert wx.settled_until(self.NOW, run) == self.FIXED

    def test_select_rows_excludes_written_and_unsettled(self):
        rows = {T0 + i * H: {"temp_c": float(i)} for i in range(5)}
        picked = wx.select_rows(rows, after=T0 + H, until=T0 + 3 * H)
        assert sorted(picked) == [T0 + 2 * H, T0 + 3 * H]  # after は含まず until は含む

    def test_update_window_resumes_after_last_written(self):
        until = int(datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc).timestamp())
        last = int(datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc).timestamp())
        start, end, after = wx.update_window(last, until)
        assert (start, end, after) == (date(2026, 10, 28), date(2026, 10, 29), last)

    def test_update_window_first_run_looks_back_default_hours(self):
        until = int(datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc).timestamp())
        start, _, after = wx.update_window(None, until)
        assert after == until - wx.DEFAULT_LOOKBACK_HOURS * H
        assert start == date(2026, 10, 26)

    def test_forecast_window_starts_at_settled_boundary_and_reaches_ahead(self):
        since, end = wx.forecast_window(self.NOW, self.FIXED)
        assert since == self.FIXED  # 確定値の最終時刻と同じ点から始めて破線がつながる
        assert end == self.FIXED + (wx.SETTLE_HOURS + wx.FORECAST_HOURS) * H
        assert wx.forecast_past_hours(self.NOW, since) == wx.SETTLE_HOURS
        assert wx.forecast_past_hours(self.NOW, self.FIXED - 3 * H) == wx.SETTLE_HOURS + 3

    def test_select_forecast_rows_is_inclusive_on_both_ends(self):
        rows = {T0 + i * H: {"temp_c": float(i)} for i in range(5)}
        assert sorted(wx.select_forecast_rows(rows, T0 + H, T0 + 3 * H)) == [T0 + H, T0 + 2 * H, T0 + 3 * H]

    def test_date_chunks_cover_range_without_overlap(self):
        chunks = wx.date_chunks(date(2025, 6, 1), date(2025, 8, 15), days=31)
        assert chunks[0] == (date(2025, 6, 1), date(2025, 7, 1))
        assert chunks[-1][1] == date(2025, 8, 15)
        for (_, e), (s, _) in zip(chunks, chunks[1:]):
            assert s == e + timedelta(days=1)


class TestLinesAndIO:
    def test_build_lines_format(self):
        lines = wx.build_lines({T0 + H: {"temp_c": 17.9, "humidity_pct": 93.0}, T0: {"temp_c": 18.1}})
        assert lines == [
            f"weather,source=jma_msm temp_c=18.1 {T0 * 10**9}",
            f"weather,source=jma_msm humidity_pct=93.0,temp_c=17.9 {(T0 + H) * 10**9}",
        ]

    def test_forecast_lines_use_their_own_measurement(self):
        # weather_* の selector に予報が混ざらないよう、計測名で分ける (ラベルではなく)
        lines = wx.build_lines({T0: {"temp_c": 18.1}}, wx.FORECAST_MEASUREMENT)
        assert lines == [f"weather_forecast,source=jma_msm temp_c=18.1 {T0 * 10**9}"]

    @responses.activate
    def test_fetch_requests_msm_in_unixtime_and_ms(self):
        responses.get(wx.API_URL, json=_payload([T0]))
        wx.fetch_hourly(wx.Location(35.68, 139.77), date(2026, 10, 27), date(2026, 10, 28), 5)
        q = parse_qs(urlparse(responses.calls[0].request.url).query)
        assert q["models"] == ["jma_msm"]
        assert q["timeformat"] == ["unixtime"]
        assert q["wind_speed_unit"] == ["ms"]
        assert q["start_date"] == ["2026-10-27"] and q["end_date"] == ["2026-10-28"]
        assert set(q["hourly"][0].split(",")) == set(wx.FIELDS)

    @responses.activate
    def test_fetch_forecast_uses_forecast_api_with_hour_window(self):
        responses.get(wx.FORECAST_API_URL, json=_payload([T0]))
        wx.fetch_forecast(wx.Location(35.68, 139.77), 6, 49, 5)
        q = parse_qs(urlparse(responses.calls[0].request.url).query)
        assert q["models"] == ["jma_msm"] and q["wind_speed_unit"] == ["ms"]
        assert q["past_hours"] == ["6"] and q["forecast_hours"] == ["49"]
        assert set(q["hourly"][0].split(",")) == set(wx.FIELDS)

    @responses.activate
    def test_fetch_model_run(self):
        _meta(responses, 1795381200, 1795393885)
        assert wx.fetch_model_run(5) == wx.ModelRun(init=1795381200, available=1795393885)

    @responses.activate
    def test_last_written_only_looks_at_settled_series(self):
        _vm_last(responses, T0)
        assert wx.last_written(VM, 5) == T0
        q = parse_qs(urlparse(responses.calls[0].request.url).query)["query"][0]
        assert q.startswith('max(tlast_over_time(weather_temp_c{source="jma_msm"}')
        responses.reset()
        _vm_last(responses, None)
        assert wx.last_written(VM, 5) is None

    @responses.activate
    def test_delete_forecast_matches_only_forecast_series(self):
        responses.post(DELETE_URL, status=204)
        wx.delete_forecast(VM, 5)
        q = parse_qs(urlparse(responses.calls[0].request.url).query)["match[]"]
        assert q == ['{__name__=~"weather_forecast_.*",source="jma_msm"}']

    def test_parse_location(self):
        assert wx.parse_location("35.68", "139.77") == wx.Location(35.68, 139.77)
        with pytest.raises(ValueError):
            wx.parse_location("", "139.77")
        with pytest.raises(ValueError):
            wx.parse_location("135.0", "139.77")


WX_ENV = {
    "INFLUX_URL": VM, "LOCATION_PREFIX": "home-", "REQUEST_TIMEOUT_S": 5.0,
    "ECHONET_DEVICES": "", "ECHONET_TIMEOUT_S": 3.0,
    "ECHONET_CIRCUIT_NAMES": "", "ECHONET_CIRCUIT_EXCLUDE": "",
    "WEATHER_LAT": "35.68", "WEATHER_LON": "139.77",
}


class TestCli:
    def _invoke(self, args, env=WX_ENV):
        from typer.testing import CliRunner
        with patch("cli.echonet._load_env", return_value=env), \
             patch("cli.echonet._write_influx") as write:
            result = CliRunner().invoke(echonet.app, args)
        batches = [list(call.args[0]) for call in write.call_args_list]
        return result, batches

    @staticmethod
    def _on_time_run(now):
        # 直近の 3 時間境界ランが 3.5 時間前に公開済み (平常運転)
        floor3 = int(now.timestamp()) // (3 * H) * (3 * H)
        init = floor3 - 3 * H if now.timestamp() - floor3 < 3.5 * H else floor3
        return init, init + int(3.5 * H)

    @responses.activate
    def test_update_writes_new_settled_hours_then_replaces_forecast(self):
        now = datetime.now(timezone.utc)
        until = wx.settled_until(now)
        last = until - 3 * H
        _meta(responses, *self._on_time_run(now - timedelta(hours=1)))
        _vm_last(responses, last)
        # 書き込み済み (last 以前)・確定済み・未確定 (until 以降) の時刻を混ぜて返す
        responses.get(wx.API_URL, json=_payload([last - H, last, last + H, last + 2 * H, until, until + H, until + 5 * H]))
        fc_end = wx.floor_hour(now) + wx.FORECAST_HOURS * H
        responses.get(wx.FORECAST_API_URL, json=_payload([until - H, until, until + H, fc_end, fc_end + H]))
        responses.post(DELETE_URL, status=204)
        result, batches = self._invoke(["weather-update"])
        assert result.exit_code == 0, result.stdout
        assert len(batches) == 2
        settled, forecast = batches
        assert sorted(map(_ts, settled)) == [last + H, last + 2 * H, until]
        assert all(l.startswith("weather,") for l in settled)
        # 予報は確定境界 until から始まり (線がつながる)、FORECAST_HOURS 先まで。古い予報は消してから書く
        assert sorted(map(_ts, forecast)) == [until, until + H, fc_end]
        assert all(l.startswith("weather_forecast,") for l in forecast)
        urls = [c.request.url for c in responses.calls]
        assert any(u.startswith(DELETE_URL) for u in urls)
        assert "wrote 3 hours" in result.stdout and "forecast 3 hours" in result.stdout

    @responses.activate
    def test_update_waits_when_the_latest_run_is_late(self):
        now = datetime.now(timezone.utc)
        fixed = wx.settled_until(now)
        late_init = fixed - 8 * H  # 本来なら 2 つ先のランまで来ているはず
        late_init -= late_init % (3 * H)
        _meta(responses, late_init, late_init + int(3.5 * H))
        _vm_last(responses, fixed - 12 * H)
        responses.get(wx.API_URL, json=_payload([fixed - 11 * H, late_init + 2 * H, late_init + 3 * H, fixed]))
        responses.get(wx.FORECAST_API_URL, json=_payload([late_init + 2 * H, late_init + 3 * H, fixed, fixed + H]))
        responses.post(DELETE_URL, status=204)
        result, batches = self._invoke(["weather-update"])
        assert result.exit_code == 0, result.stdout
        settled, forecast = batches
        assert max(map(_ts, settled)) == late_init + 2 * H  # 遅れているランが上書きする時刻は書かない
        assert min(map(_ts, forecast)) == late_init + 2 * H  # その先は予報として出す
        assert sorted(map(_ts, forecast)) == [late_init + 2 * H, late_init + 3 * H, fixed, fixed + H]

    @responses.activate
    def test_update_falls_back_to_fixed_settle_without_model_meta(self):
        now = datetime.now(timezone.utc)
        until = wx.settled_until(now)
        responses.get(wx.META_URL, status=503)
        _vm_last(responses, until - H)
        responses.get(wx.API_URL, json=_payload([until, until + H]))
        responses.get(wx.FORECAST_API_URL, json=_payload([until, until + H]))
        responses.post(DELETE_URL, status=204)
        result, batches = self._invoke(["weather-update"])
        assert result.exit_code == 0, result.stdout
        assert "warn: model meta unavailable" in result.stdout
        assert list(map(_ts, batches[0])) == [until]

    @responses.activate
    def test_update_keeps_old_forecast_when_forecast_fetch_fails(self):
        now = datetime.now(timezone.utc)
        until = wx.settled_until(now)
        _meta(responses, *self._on_time_run(now - timedelta(hours=1)))
        _vm_last(responses, until - H)
        responses.get(wx.API_URL, json=_payload([until]))
        responses.get(wx.FORECAST_API_URL, status=500)
        result, batches = self._invoke(["weather-update"])
        assert result.exit_code != 0
        assert len(batches) == 1 and list(map(_ts, batches[0])) == [until]  # 確定値は書けている
        assert not any(c.request.url.startswith(DELETE_URL) for c in responses.calls)  # 前回の予報は残す

    @responses.activate
    def test_update_dry_run_writes_and_deletes_nothing(self):
        now = datetime.now(timezone.utc)
        until = wx.settled_until(now)
        _meta(responses, *self._on_time_run(now - timedelta(hours=1)))
        _vm_last(responses, None)
        responses.get(wx.API_URL, json=_payload([until]))
        responses.get(wx.FORECAST_API_URL, json=_payload([until, until + H]))
        result, batches = self._invoke(["weather-update", "--dry-run"])
        assert result.exit_code == 0, result.stdout
        assert batches == []
        assert not any(c.request.url.startswith(DELETE_URL) for c in responses.calls)
        assert "(dry-run) wrote 1 hours" in result.stdout and "forecast 2 hours" in result.stdout

    def test_update_without_location_fails_clearly(self):
        env = {**WX_ENV, "WEATHER_LAT": "", "WEATHER_LON": ""}
        result, batches = self._invoke(["weather-update"], env=env)
        assert result.exit_code != 0
        assert batches == []

    @responses.activate
    def test_backfill_chunks_and_stops_at_settled(self):
        now = datetime.now(timezone.utc)
        until = wx.settled_until(now)
        _meta(responses, *self._on_time_run(now - timedelta(hours=1)))
        responses.get(wx.API_URL, json=_payload([until - H, until, until + H]))
        start = (now - timedelta(days=40)).date().isoformat()
        result, batches = self._invoke(["weather-backfill", "--start", start])
        assert result.exit_code == 0, result.stdout
        assert sum(1 for c in responses.calls if c.request.url.startswith(wx.API_URL)) == 2  # 31日ごとに分割
        assert all(_ts(l) <= until for b in batches for l in b)
        assert not any(l.startswith("weather_forecast") for b in batches for l in b)
