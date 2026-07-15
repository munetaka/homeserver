# tests/test_sync_data_more.py
"""カバレッジ拡充テスト (pytest スタイル)。

対象: push コマンド / _write_influx / _lp / _reading_to_line / _load_env ほか。
HTTP レイヤは responses、時刻凍結は time-machine、エスケープの
プロパティテストは hypothesis を使う。
"""

import re
from unittest.mock import patch

import pytest
import requests
import responses
from hypothesis import given
from hypothesis import strategies as st
from responses import matchers
from typer.testing import CliRunner

from cli import sync_data
from cli.switchbot_ble import BleTarget, SwitchBotReading

runner = CliRunner()

BASE_ENV = {
    "INFLUX_URL": "http://influx:8428",
    "LOCATION_PREFIX": "home.",
    "REQUEST_TIMEOUT_S": 10.0,
    "EF_MODEL": "none",
    "SWITCHBOT_BLE_DEVICES": "",
    "SWITCHBOT_BLE_SCAN_TIMEOUT": 5.0,
}


def env_with(**overrides):
    env = dict(BASE_ENV)
    env.update(overrides)
    return env


def make_reading(**overrides):
    params = dict(
        device_id="AA:BB:CC:DD:EE:FF",
        name="Living",
        device_type="meter",
        temperature=25.0,
        humidity=50.0,
    )
    params.update(overrides)
    return SwitchBotReading(**params)


# ---------------------------------------------------------------------
# push コマンド
# ---------------------------------------------------------------------

class TestPushCommand:
    def test_ble_mode_writes_points(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data.collect_ble_readings", return_value=[make_reading()]) as collect, \
             patch("cli.sync_data._write_influx") as write:
            result = runner.invoke(
                sync_data.app,
                ["push", "--ble-device", "AA:BB:CC:DD:EE:FF@meter=Living"],
            )

        assert result.exit_code == 0
        assert "wrote 1 points" in result.stdout
        targets, scan_timeout = collect.call_args[0]
        assert targets == [BleTarget(mac="AA:BB:CC:DD:EE:FF", device_type="meter", alias="Living")]
        assert scan_timeout == 5.0
        lines, influx_url, timeout_s = write.call_args[0]
        assert influx_url == "http://influx:8428"
        assert timeout_s == 10.0
        assert lines[0].startswith("climate,location=home.Living")

    def test_ble_devices_fall_back_to_env(self):
        env = env_with(
            SWITCHBOT_BLE_DEVICES="AA:BB:CC:DD:EE:01@meter=Living, 11:22:33:44:55:66@co2=Office",
        )
        with patch("cli.sync_data._load_env", return_value=env), \
             patch("cli.sync_data.collect_ble_readings", return_value=[make_reading()]) as collect, \
             patch("cli.sync_data._write_influx"):
            result = runner.invoke(sync_data.app, ["push"])

        assert result.exit_code == 0
        targets = collect.call_args[0][0]
        assert [t.alias for t in targets] == ["Living", "Office"]
        assert targets[1].device_type == "co2"

    def test_no_datapoints_exits_zero(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data.collect_ble_readings", return_value=[]), \
             patch("cli.sync_data._write_influx") as write:
            result = runner.invoke(
                sync_data.app,
                ["push", "--ble-device", "AA:BB:CC:DD:EE:FF"],
            )

        assert result.exit_code == 0
        assert "no datapoints" in result.stdout
        write.assert_not_called()

    def test_invalid_ble_spec_is_bad_parameter(self):
        with patch("cli.sync_data._load_env", return_value=env_with()):
            result = runner.invoke(sync_data.app, ["push", "--ble-device", "not-a-mac"])
        assert result.exit_code == 2

    def test_ef_model_falls_back_to_env(self):
        with patch("cli.sync_data._load_env", return_value=env_with(EF_MODEL="buck")), \
             patch("cli.sync_data.collect_ble_readings", return_value=[make_reading()]), \
             patch("cli.sync_data._reading_to_line", return_value="climate,location=x t=1.0 1") as to_line, \
             patch("cli.sync_data._write_influx"):
            result = runner.invoke(
                sync_data.app,
                ["push", "--ble-device", "AA:BB:CC:DD:EE:FF"],
            )

        assert result.exit_code == 0
        assert to_line.call_args[0][2] == "buck"  # ef_model


# ---------------------------------------------------------------------
# run コマンド (引数バリデーションのみ。ループ動作は既存テストで担保)
# ---------------------------------------------------------------------

class TestRunCommandValidation:
    def test_invalid_env_ble_spec_is_bad_parameter(self):
        env = env_with(SWITCHBOT_BLE_DEVICES="broken-spec")
        with patch("cli.sync_data._load_env", return_value=env):
            result = runner.invoke(sync_data.app, ["run"])
        assert result.exit_code == 2

    def test_run_reports_no_datapoints(self):
        env = env_with(SWITCHBOT_BLE_DEVICES="AA:BB:CC:DD:EE:FF")
        effects = [[], KeyboardInterrupt()]
        with patch("cli.sync_data._load_env", return_value=env), \
             patch("cli.sync_data._collect_once_async", side_effect=effects) as collect, \
             patch("cli.sync_data._write_influx") as write, \
             patch("cli.sync_data.asyncio.sleep", return_value=None):
            result = runner.invoke(sync_data.app, ["run", "--interval", "60"])

        assert result.exit_code == 0
        assert "no datapoints" in result.stdout
        assert "stopped" in result.stdout
        assert collect.call_count == 2
        write.assert_not_called()


# ---------------------------------------------------------------------
# _write_influx (HTTP レイヤを responses でモック)
# ---------------------------------------------------------------------

class TestWriteInflux:
    @responses.activate
    def test_endpoint_and_params(self):
        responses.add(
            responses.POST,
            "http://vm:8428/api/v2/write",
            status=204,
            match=[matchers.query_param_matcher({"precision": "ns"})],
        )
        sync_data._write_influx(["climate,location=a temperature=1.0 1"], "http://vm:8428", 5.0)
        request = responses.calls[0].request
        assert request.body == b"climate,location=a temperature=1.0 1"
        assert request.headers["Content-Type"] == "text/plain"

    @responses.activate
    def test_http_error_is_raised(self):
        responses.add(responses.POST, "http://vm:8428/api/v2/write", status=500)
        with pytest.raises(requests.HTTPError):
            sync_data._write_influx(["m f=1.0 1"], "http://vm:8428", 5.0)


# ---------------------------------------------------------------------
# Line Protocol (_lp / _reading_to_line)
# ---------------------------------------------------------------------

class TestLineProtocol:
    def test_lp_escapes_space_and_comma(self):
        line = sync_data._lp("climate", {"location": "home 1F,寝室"}, {"temperature": 20.0}, 1000)
        assert line == r"climate,location=home\ 1F\,寝室 temperature=20.0 1000000000"

    def test_lp_field_types(self):
        line = sync_data._lp("m", {"t": "x"}, {"i": 5, "f": 1.5, "b": True}, 1)
        fields = line.split(" ")[1]
        assert fields == "i=5i,f=1.5,b=true"

    def test_lp_skips_none_tags(self):
        line = sync_data._lp("m", {"a": "1", "b": None}, {"f": 1.0}, 1)
        assert line.startswith("m,a=1 ")

    @given(location=st.text(alphabet="ab XY,湿度リビング寝室01", min_size=1))
    def test_lp_line_always_has_three_sections(self, location):
        # エスケープされていない空白で区切ると必ず
        # 「measurement+tags / fields / timestamp」の3要素になる
        line = sync_data._lp("climate", {"location": location}, {"temperature": 21.5}, 123)
        sections = re.split(r"(?<!\\) ", line)
        assert len(sections) == 3
        assert sections[1] == "temperature=21.5"
        assert sections[2] == str(123 * 1_000_000)
        # タグ部もエスケープされていないカンマは measurement とタグの区切り1つだけ
        tag_parts = re.split(r"(?<!\\),", sections[0])
        assert len(tag_parts) == 2
        assert tag_parts[0] == "climate"
        assert tag_parts[1].startswith("location=")

    def test_reading_to_line_japanese_location(self):
        reading = make_reading(name="1F-寝室")
        line = sync_data._reading_to_line(reading, "home-", "none", ts_ms=1000)
        assert line.startswith("climate,location=home-1F-寝室,")

    def test_reading_to_line_returns_none_without_fields(self):
        reading = make_reading(temperature=None, humidity=None, co2=None, battery=None)
        assert sync_data._reading_to_line(reading, "", "none", ts_ms=1000) is None

    def test_reading_to_line_co2_only(self):
        reading = make_reading(temperature=None, humidity=None, co2=650)
        line = sync_data._reading_to_line(reading, "", "none", ts_ms=1000)
        assert "co2=650i" in line
        assert "abs_humidity" not in line

    def test_reading_to_line_survives_abs_humidity_error(self):
        reading = make_reading()
        with patch("cli.sync_data.calc_abs_humidity_gm3_okada", side_effect=ValueError("boom")):
            line = sync_data._reading_to_line(reading, "", "none", ts_ms=1000)
        assert "temperature=25.0" in line
        assert "abs_humidity" not in line


# ---------------------------------------------------------------------
# _load_env の既定値
# ---------------------------------------------------------------------

class TestLoadEnvDefaults:
    ENV_KEYS = [
        "INFLUX_URL", "LOCATION_PREFIX", "REQUEST_TIMEOUT_S",
        "EF_MODEL", "SWITCHBOT_BLE_DEVICES", "SWITCHBOT_BLE_SCAN_TIMEOUT",
    ]
    REMOVED_KEYS = [
        # 2026-07-16 撤去 (Cloud API / InfluxDB 認証の名残)
        "SWITCHBOT_TOKEN", "SWITCHBOT_SECRET", "SWITCHBOT_MODE",
        "INFLUX_BUCKET_OR_DB", "INFLUX_TOKEN", "USE_V3_NATIVE",
    ]

    def test_defaults(self, monkeypatch):
        for key in self.ENV_KEYS + self.REMOVED_KEYS:
            monkeypatch.delenv(key, raising=False)
        with patch("cli.sync_data.load_dotenv"):
            env = sync_data._load_env()

        assert env["INFLUX_URL"] == "http://localhost:8428"
        assert env["LOCATION_PREFIX"] == ""
        assert env["REQUEST_TIMEOUT_S"] == 10.0
        assert env["EF_MODEL"] == "none"
        assert env["SWITCHBOT_BLE_DEVICES"] == ""
        assert env["SWITCHBOT_BLE_SCAN_TIMEOUT"] == 5.0
        for key in self.REMOVED_KEYS:
            assert key not in env

    def test_overrides(self, monkeypatch):
        monkeypatch.setenv("EF_MODEL", "its90")
        monkeypatch.setenv("REQUEST_TIMEOUT_S", "3.5")
        with patch("cli.sync_data.load_dotenv"):
            env = sync_data._load_env()

        assert env["EF_MODEL"] == "its90"
        assert env["REQUEST_TIMEOUT_S"] == 3.5

# ---------------------------------------------------------------------
# 小物 (enhancement_factor / 氷フォールバック)
# ---------------------------------------------------------------------

@pytest.mark.parametrize("model,expected", [
    ("none", 1.0),
    (None, 1.0),
    ("unknown-model", 1.0),
    ("buck", 1.0007 + 3.46e-6 * 1013.25),
    ("its90", 1.00062 + 3.14e-6 * 1013.25 + 5.6e-7 * 625.0),
])
def test_enhancement_factor(model, expected):
    assert sync_data.enhancement_factor(25.0, 1013.25, model) == pytest.approx(expected)


def test_es_goff_gratch_ice_near_zero():
    assert sync_data.es_goff_gratch_ice_hpa(0.0) == pytest.approx(6.11, abs=0.05)


def test_abs_humidity_below_minus30_uses_ice_fallback():
    ah = sync_data.calc_abs_humidity_gm3_okada(-40.0, 50.0)
    assert 0.0 < ah < 0.2


# ---------------------------------------------------------------------
# scan-ble コマンドのエラーパス
# ---------------------------------------------------------------------

class TestScanBleCommandEdgeCases:
    def test_scan_error_exits_nonzero(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data.scan_switchbot_devices", side_effect=RuntimeError("bleak is required")):
            result = runner.invoke(sync_data.app, ["scan-ble"])
        assert result.exit_code == 1
        assert "error: bleak is required" in result.stdout

    def test_no_devices_found(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data.scan_switchbot_devices", return_value=[]):
            result = runner.invoke(sync_data.app, ["scan-ble"])
        assert result.exit_code == 0
        assert "no BLE devices found" in result.stdout

    def test_switchbot_without_type_prints_unknown(self):
        info = {
            "mac": "AA:BB:CC:DD:EE:FF",
            "name": "",
            "device_type": None,
            "device_model": None,
            "device_code": None,
            "rssi": None,
            "reading": None,
            "is_switchbot": True,
        }
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data.scan_switchbot_devices", return_value=[info]):
            result = runner.invoke(sync_data.app, ["scan-ble"])
        assert result.exit_code == 0
        assert "type=unknown" in result.stdout
        assert "name=-" in result.stdout

