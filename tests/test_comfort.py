"""PMV 計算 (ISO 7730) のテスト。

参照値は CBE の参照実装 pythermalcomfort 3.3.0 の pmv_ppd_iso
(tr=tdb, vr=0.1, model="7730-2005") で 2026-09-23 に生成した。
生成時、実装は 6温度×4湿度×2clo×2met の全120点で参照実装と誤差0で一致している
(クランプ対象の低温×夏服を除く)。ここではその代表点を回帰テストとして固定する。
"""

import pytest

from cli import comfort
from cli.comfort import pmv
from cli.sync_data import _comfort_lines
from cli.switchbot_ble import SwitchBotReading

# (ta, rh, clo, met) -> PMV (pythermalcomfort 3.3.0)
REFERENCE = {
    (20, 45, 0.5, 1.1): -1.8426,
    (20, 45, 1.0, 1.1): -0.6058,
    (22, 30, 0.5, 1.1): -1.2527,
    (22, 60, 1.0, 1.1): -0.0368,
    (24, 45, 0.5, 1.1): -0.4967,
    (24, 45, 0.5, 2.0): 0.8470,
    (24, 60, 1.0, 2.0): 1.3222,
    (26, 30, 0.5, 1.1): 0.0371,
    (26, 60, 0.5, 1.1): 0.2803,
    (26, 60, 0.5, 2.0): 1.3065,
    (28, 45, 0.5, 1.1): 0.8238,
    (28, 75, 1.0, 2.0): 2.0077,
    # 就寝シナリオ (met 0.7, clo=寝具込み)
    (24, 45, 1.4, 0.7): -0.8194,
    (26, 60, 1.4, 0.7): 0.0452,
    (22, 45, 2.6, 0.7): -0.1314,
    (28, 60, 2.6, 0.7): 1.4670,
}


class TestPmv:
    @pytest.mark.parametrize("key,expected", REFERENCE.items())
    def test_matches_reference_implementation(self, key, expected):
        ta, rh, clo, met = key
        assert pmv(ta, rh, clo=clo, met=met) == pytest.approx(expected, abs=1e-3)

    def test_monotonic_in_temperature_and_humidity(self):
        assert pmv(26, 50) > pmv(24, 50) > pmv(22, 50)
        assert pmv(26, 70) > pmv(26, 40)  # 湿度が高いほど暑い側へ

    def test_humidity_compensates_cold_slightly(self):
        # 左下カットの根拠: 低温では湿度を上げるとわずかに寒さが緩む
        low, high = pmv(22, 35), pmv(22, 65)
        assert 0 < high - low < 0.3

    def test_out_of_range_returns_none(self):
        assert pmv(9, 50) is None       # 温度が適用範囲外
        assert pmv(35, 50) is None
        assert pmv(22, 120) is None     # 湿度が不正
        assert pmv(10, 30, clo=0.5, met=1.1) is None  # |PMV|>3.5 のクランプ


class TestComfortLines:
    def _reading(self, name, t=25.0, h=50.0):
        return SwitchBotReading(device_id="AA", name=name, device_type="meter",
                                temperature=t, humidity=h)

    def test_emits_all_scenarios_for_indoor_room(self):
        lines = _comfort_lines(self._reading("2F-リビング"), "home-", 1000)
        assert len(lines) == len(comfort.COMBOS)
        assert any(l.startswith("comfort,location=home-2F-リビング,clo=0.5,met=1.1 pmv=")
                   for l in lines)
        assert all(l.endswith(" 1000000000") for l in lines)

    def test_excludes_outdoor_and_crawlspace(self):
        assert _comfort_lines(self._reading("屋外-玄関"), "home-", 1000) == []
        assert _comfort_lines(self._reading("基礎-寝室"), "home-", 1000) == []

    def test_requires_both_temperature_and_humidity(self):
        r = SwitchBotReading(device_id="AA", name="1F-寝室", device_type="meter",
                             temperature=25.0, humidity=None)
        assert _comfort_lines(r, "home-", 1000) == []


class TestPmvHistoryLines:
    """バックフィル用: /api/v1/export 形式からの過去分計算。"""

    def _row(self, loc, ts_values):
        return {
            "metric": {"__name__": "x", "location": loc},
            "timestamps": [ts for ts, _ in ts_values],
            "values": [v for _, v in ts_values],
        }

    def test_pairs_by_location_and_timestamp(self):
        from cli.sync_data import _pmv_history_lines
        temp = [self._row("home-1F-書斎", [(1000, 25.0), (2000, 25.5)])]
        hum = [self._row("home-1F-書斎", [(1000, 50.0), (3000, 55.0)])]  # 2000 は欠け
        lines = _pmv_history_lines(temp, hum)
        # 揃った ts=1000 の1点 × 4シナリオのみ
        assert len(lines) == len(comfort.COMBOS)
        assert all(" 1000000000" in l for l in lines)
        assert all(l.startswith("comfort,location=home-1F-書斎,clo=") for l in lines)
        # ライブ計算 (_comfort_lines) と同じ値になる
        live = _comfort_lines(
            SwitchBotReading(device_id="AA", name="1F-書斎", device_type="meter",
                             temperature=25.0, humidity=50.0), "home-", 1000)
        assert sorted(lines) == sorted(live)

    def test_excludes_outdoor_and_unmatched_rooms(self):
        from cli.sync_data import _pmv_history_lines
        temp = [self._row("home-屋外-玄関", [(1000, 30.0)]),
                self._row("home-1F-トイレ", [(1000, 25.0)])]
        hum = [self._row("home-屋外-玄関", [(1000, 60.0)])]  # トイレの湿度なし
        assert _pmv_history_lines(temp, hum) == []
