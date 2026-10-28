"""燃調自動取得のテスト。parser は実ページのスナップショットを fixture として使う。"""

import json
from datetime import date
from pathlib import Path

import pytest

from cli import tariff, tariff_fetch

FIXTURE = Path(__file__).parent / "fixtures" / "tepco_fuelcost_list_2026-10.html"


@pytest.fixture
def published():
    return tariff_fetch.parse_fuel_table(FIXTURE.read_text(encoding="utf-8"))


class TestParseFuelTable:
    def test_known_values(self, published):
        # 一覧ページの掲載値は支援「反映後」の適用単価 (低圧=自由料金の列)
        assert published["2026-11"] == pytest.approx(-5.33)
        assert published["2026-10"] == pytest.approx(-9.30)   # 反映前 -5.80 + 支援 -3.50
        assert published["2026-08"] == pytest.approx(-10.27)  # 反映前 -6.77 + 支援 -3.50
        assert published["2025-06"] == pytest.approx(-6.39)   # 支援なし期間

    def test_covers_full_history(self, published):
        # 2024-12 まで遡って載っている (月数はページ更新で増える)
        assert len(published) >= 20
        assert "2024-12" in published

    def test_rejects_page_without_table(self):
        with pytest.raises(ValueError):
            tariff_fetch.parse_fuel_table("<html><body>メンテナンス中</body></html>")


class TestPlanAgainstLivePage:
    def test_current_code_table_matches_published(self, published):
        # 登録済み定数 (fuel+subsidy) が TEPCO 公表の適用値と全月一致すること。
        # タイポ・二重計上の週次セルフチェックを兼ねる
        plan = tariff_fetch.plan_overlay_update(
            published, tariff.FUEL_ADJUSTMENT_YEN, tariff.SUBSIDY_YEN, {})
        assert plan.mismatches == []
        assert plan.rejected == []
        assert plan.added == {}  # 2026-11 までは定数に登録済み


class TestPlanOverlayUpdate:
    CODE = {"2026-10": -5.80, "2026-11": -5.33}
    SUBSIDY = {"2026-10": -3.5}

    def test_adds_consecutive_new_months(self):
        published = {"2026-10": -9.30, "2026-11": -5.33, "2026-12": -5.10, "2027-01": -4.90}
        plan = tariff_fetch.plan_overlay_update(published, self.CODE, self.SUBSIDY, {})
        assert plan.added == {"2026-12": -5.10, "2027-01": -4.90}
        assert plan.rejected == [] and plan.mismatches == []

    def test_rejects_gap(self):
        # 2026-12 を飛ばして 2027-01 だけ現れたら追記しない
        plan = tariff_fetch.plan_overlay_update(
            {"2026-11": -5.33, "2027-01": -4.90}, self.CODE, self.SUBSIDY, {})
        assert plan.added == {}
        assert any("連続していない" in r for r in plan.rejected)

    def test_rejects_insane_value(self):
        plan = tariff_fetch.plan_overlay_update(
            {"2026-11": -5.33, "2026-12": -93.0}, self.CODE, self.SUBSIDY, {})
        assert plan.added == {}
        assert any("正気範囲" in r for r in plan.rejected)

    def test_detects_code_mismatch(self):
        # 反映後の値を定数に入れる事故 (2026-08 でやらかした) を公表値との照合で検出
        plan = tariff_fetch.plan_overlay_update(
            {"2026-10": -9.30}, {"2026-10": -9.30}, self.SUBSIDY, {})
        assert any("2026-10" in m for m in plan.mismatches)

    def test_overlay_value_change_is_reported_not_overwritten(self):
        plan = tariff_fetch.plan_overlay_update(
            {"2026-12": -5.10}, self.CODE, self.SUBSIDY, {"2026-12": -5.00})
        assert plan.added == {}
        assert any("追記のみ" in m for m in plan.mismatches)

    def test_months_before_first_registration_are_ignored(self):
        plan = tariff_fetch.plan_overlay_update(
            {"2024-12": -6.33, "2026-11": -5.33}, self.CODE, self.SUBSIDY, {})
        assert plan.added == {} and plan.rejected == []


class TestOverlayIO:
    def test_append_only(self, tmp_path):
        path = tmp_path / "var" / "fuel_adjustment.json"
        tariff_fetch.append_overlay(path, {"2026-12": -5.10}, "2026-10-28T06:30:00+09:00")
        tariff_fetch.append_overlay(path, {"2026-12": -9.99, "2027-01": -4.90}, "t2")
        data = json.loads(path.read_text(encoding="utf-8"))
        assert data["2026-12"]["applied_yen"] == -5.10  # 既存月は上書きされない
        assert data["2027-01"]["applied_yen"] == -4.90

    def test_load_overlay_tolerates_garbage(self, tmp_path, monkeypatch):
        path = tmp_path / "fuel_adjustment.json"
        path.write_text('{"2026-12": {"applied_yen": -5.1}, "junk": 1, "2027-1": {"applied_yen": 0}}',
                        encoding="utf-8")
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(path))
        assert tariff.load_overlay() == {"2026-12": -5.1}
        path.write_text("not json", encoding="utf-8")
        assert tariff.load_overlay() == {}


class TestRatesWithOverlay:
    def test_overlay_fills_unregistered_month(self, tmp_path, monkeypatch):
        path = tmp_path / "fuel_adjustment.json"
        tariff_fetch.append_overlay(path, {"2026-12": -5.10}, "t")
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(path))
        r = tariff.rates_for("2026-12")
        assert r.fuel == pytest.approx(-5.10)  # 支援なし月: 適用値がそのまま fuel
        assert r.subsidy == 0.0
        assert tariff.marginal_buy_rate_yen("2026-12") == pytest.approx(30.72 - 5.10 + 4.18)

    def test_overlay_decomposes_registered_subsidy(self, tmp_path, monkeypatch):
        # 支援が SUBSIDY_YEN に登録されている月は、適用値 = fuel + subsidy として逆算する
        path = tmp_path / "fuel_adjustment.json"
        tariff_fetch.append_overlay(path, {"2026-10": -9.30}, "t")
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(path))
        monkeypatch.delitem(tariff.FUEL_ADJUSTMENT_YEN, "2026-10")
        r = tariff.rates_for("2026-10")
        assert r.fuel == pytest.approx(-5.80)
        assert r.subsidy == pytest.approx(-3.5)
        assert r.per_kwh_extra == pytest.approx(-9.30 + 4.18)

    def test_code_table_wins_over_overlay(self, tmp_path, monkeypatch):
        path = tmp_path / "fuel_adjustment.json"
        tariff_fetch.append_overlay(path, {"2026-11": -99.0}, "t")
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(path))
        assert tariff.rates_for("2026-11").fuel == pytest.approx(-5.33)

    def test_unknown_month_still_raises(self, tmp_path, monkeypatch):
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(tmp_path / "missing.json"))
        with pytest.raises(KeyError):
            tariff.rates_for("2028-01")


class TestMonthsAheadAndMetrics:
    def test_months_ahead(self, tmp_path, monkeypatch):
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(tmp_path / "missing.json"))
        # 登録最大 2026-11 / 10月28日の請求月は 2026-11 → 先行 0ヶ月
        assert tariff_fetch.months_ahead(date(2026, 10, 28)) == 0
        assert tariff_fetch.months_ahead(date(2026, 9, 5)) == 2
        assert tariff_fetch.months_ahead(date(2026, 12, 10)) == -2  # 既に未登録期間

    def test_months_ahead_counts_overlay(self, tmp_path, monkeypatch):
        path = tmp_path / "fuel_adjustment.json"
        tariff_fetch.append_overlay(path, {"2026-12": -5.10}, "t")
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(path))
        assert tariff_fetch.months_ahead(date(2026, 10, 28)) == 1

    def test_recent_applied_rates_merges_both_layers(self, tmp_path, monkeypatch):
        path = tmp_path / "fuel_adjustment.json"
        tariff_fetch.append_overlay(path, {"2026-12": -5.10}, "t")
        monkeypatch.setenv(tariff.OVERLAY_PATH_ENV, str(path))
        rates = tariff_fetch.recent_applied_rates(date(2026, 10, 28), back=2)
        # 請求月 2026-11 の2ヶ月前 = 2026-09 から登録最大 2026-12 まで
        assert sorted(rates) == ["2026-09", "2026-10", "2026-11", "2026-12"]
        assert rates["2026-09"] == (-10.96, "code")  # 定数は反映後に換算 (-6.46 + -4.5)
        assert rates["2026-10"] == (-9.30, "code")
        assert rates["2026-11"] == (-5.33, "code")
        assert rates["2026-12"] == (-5.10, "overlay")

    def test_metric_lines(self):
        lines = tariff_fetch.build_metric_lines(
            True, 1, 0, {"2026-12": (-5.1, "overlay"), "2026-11": (-5.33, "code")},
            ts_ms=1_700_000_000_000)
        assert lines[0] == ("tariff check_ok=1i,months_ahead=1i,code_mismatch=0i "
                            "1700000000000000000")
        assert lines[1] == "tariff_fuel,month=2026-11,source=code applied_yen=-5.33 1700000000000000000"
        assert lines[2] == "tariff_fuel,month=2026-12,source=overlay applied_yen=-5.1 1700000000000000000"
