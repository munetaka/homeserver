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


def _payload(times, **series):
    hourly = {"time": list(times)}
    for var in wx.FIELDS:
        hourly[var] = series.get(var, [1.0] * len(times))
    return {"latitude": 35.7, "longitude": 139.75, "hourly": hourly}


def _vm_last(rsps, ts):
    result = [] if ts is None else [{"metric": {}, "value": [0, str(ts)]}]
    rsps.get(f"{VM}/api/v1/query", json={"data": {"result": result}})


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
    def test_settled_until_is_six_hours_back_on_the_hour(self):
        now = datetime(2026, 10, 29, 20, 23, tzinfo=timezone(timedelta(hours=9)))  # 11:23 UTC
        assert wx.settled_until(now) == int(datetime(2026, 10, 29, 5, 0, tzinfo=timezone.utc).timestamp())

    def test_select_rows_excludes_written_and_unsettled(self):
        rows = {T0 + i * H: {"temp_c": float(i)} for i in range(5)}
        picked = wx.select_rows(rows, after=T0 + H, until=T0 + 3 * H)
        assert sorted(picked) == [T0 + 2 * H, T0 + 3 * H]  # after は含まず until は含む

    def test_update_window_resumes_after_last_written(self):
        now = datetime(2026, 10, 29, 11, 23, tzinfo=timezone.utc)
        last = int(datetime(2026, 10, 27, 23, 0, tzinfo=timezone.utc).timestamp())
        start, end, after = wx.update_window(now, last)
        assert (start, end, after) == (date(2026, 10, 28), date(2026, 10, 29), last)

    def test_update_window_first_run_looks_back_default_hours(self):
        now = datetime(2026, 10, 29, 11, 23, tzinfo=timezone.utc)
        start, _, after = wx.update_window(now, None)
        assert after == wx.settled_until(now) - wx.DEFAULT_LOOKBACK_HOURS * H
        assert start == date(2026, 10, 26)

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
    def test_last_written(self):
        _vm_last(responses, T0)
        assert wx.last_written(VM, 5) == T0
        responses.reset()
        _vm_last(responses, None)
        assert wx.last_written(VM, 5) is None

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
        lines = [l for call in write.call_args_list for l in call.args[0]]
        return result, lines

    @responses.activate
    def test_update_writes_only_new_settled_hours(self):
        now = datetime.now(timezone.utc)
        until = wx.settled_until(now)
        last = until - 3 * H
        _vm_last(responses, last)
        # 書き込み済み (last 以前)・確定済み・未確定 (until 以降) の時刻を混ぜて返す
        times = [last - H, last, last + H, last + 2 * H, until, until + H, until + 5 * H]
        responses.get(wx.API_URL, json=_payload(times))
        result, lines = self._invoke(["weather-update"])
        assert result.exit_code == 0, result.stdout
        written = sorted(int(l.rsplit(" ", 1)[1]) // 10**9 for l in lines)
        assert written == [last + H, last + 2 * H, until]
        assert "wrote 3 hours" in result.stdout

    @responses.activate
    def test_update_dry_run_writes_nothing(self):
        _vm_last(responses, None)
        responses.get(wx.API_URL, json=_payload([wx.settled_until(datetime.now(timezone.utc))]))
        result, lines = self._invoke(["weather-update", "--dry-run"])
        assert result.exit_code == 0, result.stdout
        assert lines == [] and "(dry-run) wrote 1 hours" in result.stdout

    def test_update_without_location_fails_clearly(self):
        env = {**WX_ENV, "WEATHER_LAT": "", "WEATHER_LON": ""}
        result, lines = self._invoke(["weather-update"], env=env)
        assert result.exit_code != 0
        assert lines == []

    @responses.activate
    def test_backfill_chunks_and_stops_at_settled(self):
        until = wx.settled_until(datetime.now(timezone.utc))
        responses.get(wx.API_URL, json=_payload([until - H, until, until + H]))
        start = (datetime.now(timezone.utc) - timedelta(days=40)).date().isoformat()
        result, lines = self._invoke(["weather-backfill", "--start", start])
        assert result.exit_code == 0, result.stdout
        assert len(responses.calls) == 2  # 31日ごとに分割
        assert all(int(l.rsplit(" ", 1)[1]) // 10**9 <= until for l in lines)
