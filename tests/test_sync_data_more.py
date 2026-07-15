# tests/test_sync_data_more.py
"""カバレッジ拡充テスト (pytest スタイル)。

対象: push コマンド / _write_influx / _sb_headers / _sb_get /
_collect_api_readings / _lp / _reading_to_line / _load_env ほか。
HTTP レイヤは responses、時刻凍結は time-machine、エスケープの
プロパティテストは hypothesis を使う。
"""

import asyncio
import re
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
import requests
import responses
import time_machine
from hypothesis import given
from hypothesis import strategies as st
from responses import matchers
from typer.testing import CliRunner

from cli import sync_data
from cli.switchbot_ble import BleTarget, SwitchBotReading

runner = CliRunner()

BASE_ENV = {
    "SWITCHBOT_TOKEN": "sb-token",
    "SWITCHBOT_SECRET": "sb-secret",
    "INFLUX_URL": "http://influx:8086",
    "INFLUX_BUCKET_OR_DB": "climate",
    "INFLUX_TOKEN": "influx-token",
    "LOCATION_PREFIX": "home.",
    "REQUEST_TIMEOUT_S": 10.0,
    "USE_V3_NATIVE": False,
    "EF_MODEL": "none",
    "SWITCHBOT_MODE": "api",
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
        with patch("cli.sync_data._load_env", return_value=env_with(SWITCHBOT_MODE="ble")), \
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
        lines, influx_url, bucket, token, timeout_s, use_v3 = write.call_args[0]
        assert influx_url == "http://influx:8086"
        assert bucket == "climate"
        assert token == "influx-token"
        assert use_v3 is False
        assert lines[0].startswith("climate,location=home.Living")

    def test_api_mode_uses_credentials(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data._collect_api_readings", return_value=[make_reading()]) as collect, \
             patch("cli.sync_data._write_influx"):
            result = runner.invoke(sync_data.app, ["push"])

        assert result.exit_code == 0
        assert "wrote 1 points" in result.stdout
        collect.assert_called_once_with("sb-token", "sb-secret", 10.0)

    def test_ble_devices_fall_back_to_env(self):
        env = env_with(
            SWITCHBOT_MODE="ble",
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
        with patch("cli.sync_data._load_env", return_value=env_with(SWITCHBOT_MODE="ble")), \
             patch("cli.sync_data.collect_ble_readings", return_value=[]), \
             patch("cli.sync_data._write_influx") as write:
            result = runner.invoke(
                sync_data.app,
                ["push", "--ble-device", "AA:BB:CC:DD:EE:FF"],
            )

        assert result.exit_code == 0
        assert "no datapoints" in result.stdout
        write.assert_not_called()

    def test_invalid_mode_is_bad_parameter(self):
        with patch("cli.sync_data._load_env", return_value=env_with()):
            result = runner.invoke(sync_data.app, ["push", "--mode", "zigbee"])
        assert result.exit_code == 2

    def test_invalid_ble_spec_is_bad_parameter(self):
        with patch("cli.sync_data._load_env", return_value=env_with(SWITCHBOT_MODE="ble")):
            result = runner.invoke(sync_data.app, ["push", "--ble-device", "not-a-mac"])
        assert result.exit_code == 2

    def test_bucket_and_token_are_optional(self):
        # VictoriaMetrics は bucket/token を無視するので未設定でも起動できる (2026-07-15緩和)
        with patch("cli.sync_data._load_env",
                   return_value=env_with(INFLUX_BUCKET_OR_DB="home", INFLUX_TOKEN="none",
                                         SWITCHBOT_MODE="ble")), \
             patch("cli.sync_data.collect_ble_readings", return_value=[make_reading()]), \
             patch("cli.sync_data._write_influx") as write:
            result = runner.invoke(sync_data.app, ["push", "--ble-device", "AA:BB:CC:DD:EE:FF"])
        assert result.exit_code == 0
        write.assert_called_once()

    def test_api_mode_requires_switchbot_token(self):
        with patch("cli.sync_data._load_env", return_value=env_with(SWITCHBOT_TOKEN=None)):
            result = runner.invoke(sync_data.app, ["push"])
        assert result.exit_code == 2

    def test_ef_model_falls_back_to_env(self):
        with patch("cli.sync_data._load_env", return_value=env_with(EF_MODEL="buck", SWITCHBOT_MODE="ble")), \
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
    def test_invalid_mode_is_bad_parameter(self):
        with patch("cli.sync_data._load_env", return_value=env_with()):
            result = runner.invoke(sync_data.app, ["run", "--mode", "zigbee"])
        assert result.exit_code == 2

    def test_invalid_env_ble_spec_is_bad_parameter(self):
        env = env_with(SWITCHBOT_MODE="ble", SWITCHBOT_BLE_DEVICES="broken-spec")
        with patch("cli.sync_data._load_env", return_value=env):
            result = runner.invoke(sync_data.app, ["run"])
        assert result.exit_code == 2

    def test_api_mode_requires_switchbot_secret(self):
        with patch("cli.sync_data._load_env", return_value=env_with(SWITCHBOT_SECRET=None)):
            result = runner.invoke(sync_data.app, ["run"])
        assert result.exit_code == 2

    def test_run_reports_no_datapoints(self):
        env = env_with(SWITCHBOT_MODE="ble", SWITCHBOT_BLE_DEVICES="AA:BB:CC:DD:EE:FF")
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
    def test_v2_endpoint_and_params(self):
        responses.add(
            responses.POST,
            "http://influx:8086/api/v2/write",
            status=204,
            match=[matchers.query_param_matcher({"bucket": "climate", "precision": "ns"})],
        )

        sync_data._write_influx(
            ["climate,location=a temperature=1.0 1", "climate,location=b temperature=2.0 2"],
            influx_url="http://influx:8086",
            bucket_or_db="climate",
            token="influx-token",
            timeout_s=5.0,
            use_v3_native=False,
        )

        request = responses.calls[0].request
        assert request.headers["Authorization"] == "Token influx-token"
        assert request.headers["Content-Type"] == "text/plain"
        assert request.body == (
            b"climate,location=a temperature=1.0 1\nclimate,location=b temperature=2.0 2"
        )

    @responses.activate
    def test_v3_native_endpoint_and_params(self):
        responses.add(
            responses.POST,
            "http://influx:8086/api/v3/write_lp",
            status=204,
            match=[matchers.query_param_matcher({"db": "climate", "precision": "ns"})],
        )

        sync_data._write_influx(
            ["climate,location=a temperature=1.0 1"],
            influx_url="http://influx:8086",
            bucket_or_db="climate",
            token="influx-token",
            timeout_s=5.0,
            use_v3_native=True,
        )

        assert len(responses.calls) == 1

    @responses.activate
    def test_http_error_is_raised(self):
        responses.add(responses.POST, "http://influx:8086/api/v2/write", status=500)

        with pytest.raises(requests.HTTPError):
            sync_data._write_influx(
                ["climate,location=a temperature=1.0 1"],
                influx_url="http://influx:8086",
                bucket_or_db="climate",
                token="influx-token",
                timeout_s=5.0,
                use_v3_native=False,
            )


# ---------------------------------------------------------------------
# SwitchBot API (_sb_headers / _sb_get / _get_devices / _get_status)
# ---------------------------------------------------------------------

class TestSwitchBotApi:
    @time_machine.travel(datetime(2026, 1, 1, tzinfo=timezone.utc), tick=False)
    def test_sb_headers_known_hmac_vector(self):
        fixed = uuid.UUID("12345678-1234-5678-1234-567812345678")
        with patch("cli.sync_data.uuid.uuid4", return_value=fixed):
            headers = sync_data._sb_headers("test-token", "test-secret")

        assert headers["authorization"] == "test-token"
        assert headers["t"] == "1767225600000"
        assert headers["nonce"] == "12345678-1234-5678-1234-567812345678"
        # token + t + nonce を secret で HMAC-SHA256 → base64 した既知ベクタ
        assert headers["sign"] == "MSlt2RbM582JxglB98UMsybJCcNycKQezLc18ZBb3Lc="
        assert headers["Content-Type"] == "application/json; charset=utf8"

    @responses.activate
    def test_get_devices_returns_device_list(self):
        responses.add(
            responses.GET,
            "https://api.switch-bot.com/v1.1/devices",
            json={"statusCode": 100, "body": {"deviceList": [{"deviceId": "dev1"}]}},
        )

        devices = sync_data._get_devices("t", "s", 5.0)
        assert devices == [{"deviceId": "dev1"}]
        assert responses.calls[0].request.headers["authorization"] == "t"

    @responses.activate
    def test_get_status_hits_device_endpoint(self):
        responses.add(
            responses.GET,
            "https://api.switch-bot.com/v1.1/devices/dev1/status",
            json={"statusCode": 100, "body": {"temperature": 24.5}},
        )

        status = sync_data._get_status("dev1", "t", "s", 5.0)
        assert status == {"temperature": 24.5}

    @responses.activate
    def test_non_100_status_code_raises(self):
        responses.add(
            responses.GET,
            "https://api.switch-bot.com/v1.1/devices",
            json={"statusCode": 190, "message": "device internal error"},
        )

        with pytest.raises(RuntimeError, match="SwitchBot API error"):
            sync_data._sb_get("devices", "t", "s", 5.0)

    @responses.activate
    def test_http_error_raises(self):
        responses.add(
            responses.GET,
            "https://api.switch-bot.com/v1.1/devices",
            status=401,
        )

        with pytest.raises(requests.HTTPError):
            sync_data._sb_get("devices", "t", "s", 5.0)


# ---------------------------------------------------------------------
# _collect_api_readings のデバイスフィルタリング
# ---------------------------------------------------------------------

class TestCollectApiReadings:
    def test_meter_device_is_collected(self):
        with patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "dev1", "deviceType": "MeterPro", "deviceName": "Living"},
        ]), patch("cli.sync_data._get_status", return_value={
            "temperature": "24.5", "humidity": 50, "battery": "87.6",
        }):
            readings = sync_data._collect_api_readings("t", "s", 5.0)

        assert len(readings) == 1
        reading = readings[0]
        assert reading.device_id == "dev1"
        assert reading.name == "Living"
        assert reading.temperature == 24.5
        assert reading.humidity == 50.0
        assert reading.battery == 88  # "87.6" → round → int
        assert reading.co2 is None

    def test_non_meter_device_with_status_error_is_skipped(self):
        with patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "bot1", "deviceType": "Bot", "deviceName": "Switch"},
        ]), patch("cli.sync_data._get_status", side_effect=RuntimeError("no status")):
            readings = sync_data._collect_api_readings("t", "s", 5.0)

        assert readings == []

    def test_non_meter_device_without_climate_keys_is_skipped(self):
        with patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "plug1", "deviceType": "Plug", "deviceName": "Plug"},
        ]), patch("cli.sync_data._get_status", return_value={"power": "on"}):
            readings = sync_data._collect_api_readings("t", "s", 5.0)

        assert readings == []

    def test_non_meter_device_with_climate_keys_is_collected(self):
        # Hub 2 のように METER_TYPES 外でも温湿度を返すデバイスは拾う
        with patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "hub2", "deviceType": "Hub 2", "deviceName": "Utility"},
        ]), patch("cli.sync_data._get_status", return_value={"temperature": 25.9, "humidity": 59}):
            readings = sync_data._collect_api_readings("t", "s", 5.0)

        assert len(readings) == 1
        assert readings[0].device_type == "Hub 2"

    def test_meter_device_with_empty_status_is_skipped(self):
        with patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "dev1", "deviceType": "Meter", "deviceName": "Living"},
        ]), patch("cli.sync_data._get_status", return_value={"temperature": "n/a"}):
            readings = sync_data._collect_api_readings("t", "s", 5.0)

        assert readings == []

    def test_missing_ids_fall_back(self):
        with patch("cli.sync_data._get_devices", return_value=[
            {"deviceType": "Meter"},
        ]), patch("cli.sync_data._get_status", return_value={"temperature": 20.0}):
            readings = sync_data._collect_api_readings("t", "s", 5.0)

        assert readings[0].device_id == "Meter"
        assert readings[0].name == "Meter"


# ---------------------------------------------------------------------
# _gather_readings / _collect_once_async のモード分岐
# ---------------------------------------------------------------------

class TestGatherReadings:
    def test_api_mode_requires_credentials(self):
        with pytest.raises(RuntimeError, match="token/secret"):
            sync_data._gather_readings("api", None, None, 5.0, None, 5.0)

    def test_unsupported_mode_raises(self):
        with pytest.raises(RuntimeError, match="Unsupported mode"):
            sync_data._gather_readings("zigbee", "t", "s", 5.0, None, 5.0)

    def test_collect_once_async_api_mode(self):
        with patch("cli.sync_data._collect_api_readings", return_value=[make_reading()]):
            lines = asyncio.run(sync_data._collect_once_async(
                location_prefix="", mode="api", timeout_s=5.0, ef_model="none",
                token="t", secret="s",
            ))
        assert len(lines) == 1

    def test_collect_once_async_api_requires_credentials(self):
        with pytest.raises(RuntimeError, match="token/secret"):
            asyncio.run(sync_data._collect_once_async(
                location_prefix="", mode="api", timeout_s=5.0, ef_model="none",
            ))

    def test_collect_once_async_unsupported_mode(self):
        with pytest.raises(RuntimeError, match="Unsupported mode"):
            asyncio.run(sync_data._collect_once_async(
                location_prefix="", mode="zigbee", timeout_s=5.0, ef_model="none",
            ))


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
        "SWITCHBOT_TOKEN", "SWITCHBOT_SECRET", "INFLUX_URL", "INFLUX_BUCKET_OR_DB",
        "INFLUX_TOKEN", "LOCATION_PREFIX", "REQUEST_TIMEOUT_S", "USE_V3_NATIVE",
        "EF_MODEL", "SWITCHBOT_MODE", "SWITCHBOT_BLE_DEVICES", "SWITCHBOT_BLE_SCAN_TIMEOUT",
    ]

    def test_defaults(self, monkeypatch):
        for key in self.ENV_KEYS:
            monkeypatch.delenv(key, raising=False)
        with patch("cli.sync_data.load_dotenv"):
            env = sync_data._load_env()

        assert env["SWITCHBOT_TOKEN"] is None
        assert env["SWITCHBOT_SECRET"] is None
        assert env["INFLUX_URL"] == "http://localhost:8086"
        assert env["INFLUX_BUCKET_OR_DB"] == "home"
        assert env["INFLUX_TOKEN"] == "none"
        assert env["LOCATION_PREFIX"] == ""
        assert env["REQUEST_TIMEOUT_S"] == 10.0
        assert env["USE_V3_NATIVE"] is False
        assert env["EF_MODEL"] == "none"
        assert env["SWITCHBOT_MODE"] == "api"
        assert env["SWITCHBOT_BLE_DEVICES"] == ""
        assert env["SWITCHBOT_BLE_SCAN_TIMEOUT"] == 5.0

    def test_overrides(self, monkeypatch):
        monkeypatch.setenv("USE_V3_NATIVE", "TRUE")
        monkeypatch.setenv("SWITCHBOT_MODE", "BLE")
        monkeypatch.setenv("REQUEST_TIMEOUT_S", "3.5")
        with patch("cli.sync_data.load_dotenv"):
            env = sync_data._load_env()

        assert env["USE_V3_NATIVE"] is True
        assert env["SWITCHBOT_MODE"] == "ble"
        assert env["REQUEST_TIMEOUT_S"] == 3.5


# ---------------------------------------------------------------------
# 小物 (_coerce_* / enhancement_factor / 氷フォールバック / _format_reading)
# ---------------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("abc", None),
    ("1.5", 1.5),
    (2, 2.0),
])
def test_coerce_float(value, expected):
    assert sync_data._coerce_float(value) == expected


@pytest.mark.parametrize("value,expected", [
    (None, None),
    ("abc", None),
    ("87.6", 88),
    (99, 99),
])
def test_coerce_int(value, expected):
    assert sync_data._coerce_int(value) == expected


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


def test_format_reading_none_is_na():
    assert sync_data._format_reading(None) == "n/a"


def test_format_reading_empty_is_no_data():
    reading = make_reading(temperature=None, humidity=None)
    assert sync_data._format_reading(reading) == "no data"


@pytest.mark.parametrize("device_type,expected", [
    ("MeterPro(CO2)", "co2"),
    ("Air Quality Monitor", "co2"),
    ("Meter", "meter"),
    ("", "meter"),
])
def test_guess_ble_type(device_type, expected):
    assert sync_data._guess_ble_type(device_type) == expected


# ---------------------------------------------------------------------
# devices / scan-ble / compare コマンドのエラーパス
# ---------------------------------------------------------------------

class TestDevicesCommandEdgeCases:
    def test_no_devices(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data._get_devices", return_value=[]):
            result = runner.invoke(sync_data.app, ["devices"])
        assert result.exit_code == 0
        assert "no devices" in result.stdout

    def test_status_error_is_reported(self):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data._get_devices", return_value=[
                 {"deviceId": "dev1", "deviceType": "Meter", "deviceName": "Room"},
             ]), \
             patch("cli.sync_data._get_status", side_effect=RuntimeError("offline")):
            result = runner.invoke(sync_data.app, ["devices"])
        assert result.exit_code == 0
        assert "status error: offline" in result.stdout


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


class TestCompareCommandEdgeCases:
    def _invoke(self, pair_value, devices=None):
        with patch("cli.sync_data._load_env", return_value=env_with()), \
             patch("cli.sync_data._get_devices", return_value=devices or []):
            return runner.invoke(sync_data.app, ["compare", "--pair", pair_value])

    def test_pair_without_equals_is_bad_parameter(self):
        result = self._invoke("B0E9FE54488F")
        assert result.exit_code == 2

    def test_pair_with_empty_side_is_bad_parameter(self):
        result = self._invoke("=AA:BB:CC:DD:EE:FF")
        assert result.exit_code == 2

    def test_unknown_device_id_is_bad_parameter(self):
        result = self._invoke("UNKNOWN=AA:BB:CC:DD:EE:FF")
        assert result.exit_code == 2

    def test_invalid_ble_mac_is_bad_parameter(self):
        result = self._invoke(
            "B0E9FE54488F=not-a-mac",
            devices=[{"deviceId": "B0E9FE54488F", "deviceType": "MeterPro(CO2)"}],
        )
        assert result.exit_code == 2
