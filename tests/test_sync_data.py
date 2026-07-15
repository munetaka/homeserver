from unittest.mock import AsyncMock, patch

import pytest
from typer.testing import CliRunner

from cli import sync_data
from cli.switchbot_ble import SwitchBotReading

runner = CliRunner()


class TestAbsHumidity:
    def test_es_okada_at_zero_celsius(self):
        # 0℃の飽和水蒸気圧は約 6.11 hPa
        assert sync_data.es_okada_water_hpa(0.0) == pytest.approx(6.107, abs=0.01)

    def test_es_okada_at_room_temperature(self):
        # 26.1℃の飽和水蒸気圧は約 33.8 hPa
        assert sync_data.es_okada_water_hpa(26.1) == pytest.approx(33.80, abs=0.1)

    def test_abs_humidity_reported_case(self):
        # 実測で 142.2 g/m^3 が記録されていたケース。正しくは約 14.5 g/m^3
        ah = sync_data.calc_abs_humidity_gm3_okada(26.1, 59.0, f_model="buck")
        assert ah == pytest.approx(14.5, abs=0.1)

    def test_abs_humidity_no_enhancement(self):
        # 25℃/50%RH の絶対湿度は約 11.5 g/m^3
        ah = sync_data.calc_abs_humidity_gm3_okada(25.0, 50.0, f_model="none")
        assert ah == pytest.approx(11.5, abs=0.2)

    @pytest.mark.parametrize("temp", [0.0, 10.0, 20.0, 30.0, 40.0])
    @pytest.mark.parametrize("rh", [10.0, 50.0, 90.0])
    def test_abs_humidity_physically_plausible_range(self, temp, rh):
        # 室内域では 0〜50 g/m^3 に収まるはず（10倍バグの再発防止）
        ah = sync_data.calc_abs_humidity_gm3_okada(temp, rh)
        assert ah > 0.0
        assert ah < 60.0, f"temp={temp}, rh={rh}, ah={ah}"


class TestLoadEnv:
    def test_bucket_and_token_default_for_victoriametrics(self):
        # VictoriaMetrics 運用では bucket/token を書かなくても起動できる
        import os
        with patch.dict(os.environ, {}, clear=True), \
             patch("cli.sync_data.load_dotenv"):
            env = sync_data._load_env()
        assert env["INFLUX_BUCKET_OR_DB"] == "home"
        assert env["INFLUX_TOKEN"] == "none"
        assert env["SWITCHBOT_TOKEN"] is None  # Cloud API 系は既定値なし(必要時のみ)


class TestSyncDataHelpers:
    def test_reading_to_line_includes_abs_and_battery(self):
        reading = SwitchBotReading(
            device_id="AA:BB:CC:DD:EE:FF",
            name="Living",
            device_type="meter",
            temperature=25.0,
            humidity=50.0,
            battery=82,
        )
        line = sync_data._reading_to_line(reading, "home.", "none", ts_ms=123456789)
        assert line is not None
        assert "temperature=25.0" in line
        assert "humidity=50.0" in line
        assert "abs_humidity=" in line
        assert "battery=82i" in line
        assert line.startswith("climate,location=home.Living")

    def test_collect_once_ble_mode_uses_ble_targets(self):
        reading = SwitchBotReading(
            device_id="AA:BB:CC:DD:EE:FF",
            name="Living",
            device_type="meter",
            temperature=21.5,
            humidity=45.0,
        )
        with patch("cli.sync_data.collect_ble_readings", return_value=[reading]) as mock_collect:
            lines = sync_data._collect_once(
                location_prefix="",
                mode="ble",
                timeout_s=5.0,
                ef_model="none",
                ble_targets=[],
                ble_scan_timeout_s=3.0,
            )
        assert len(lines) == 1
        mock_collect.assert_called_once()


class TestSyncDataCli:
    def test_scan_ble_command_outputs_devices(self):
        reading = SwitchBotReading(
            device_id="AA:BB:CC:DD:EE:FF",
            name="Living",
            device_type="meter",
            temperature=23.4,
            humidity=55.0,
            battery=80,
        )
        with patch("cli.sync_data.scan_switchbot_devices", return_value=[{
            "mac": "AA:BB:CC:DD:EE:FF",
            "name": "Living",
            "device_type": "meter",
            "device_model": "meter",
            "device_code": 0x54,
            "rssi": -65,
            "reading": reading,
            "is_switchbot": True,
        }]):
            result = runner.invoke(sync_data.app, ["scan-ble", "--timeout-s", "1"])

        assert result.exit_code == 0
        assert "AA:BB:CC:DD:EE:FF" in result.stdout
        assert "temp=23.4C" in result.stdout
        assert "hum=55%" in result.stdout
        assert "source=switchbot" in result.stdout
        assert "type=meter" in result.stdout
        assert "model=meter" in result.stdout
        assert "code=0x54" in result.stdout

    def test_compare_command(self):
        api_reading = SwitchBotReading(
            device_id="B0E9FE54488F",
            name="MeterPro",
            device_type="co2",
            temperature=25.0,
            humidity=55.0,
            co2=650,
            battery=80,
        )
        ble_reading = SwitchBotReading(
            device_id="b0:e9:fe:54:48:8f",
            name="B0E9FE54488F",
            device_type="co2",
            temperature=24.8,
            humidity=54.0,
            co2=645,
            battery=79,
        )
        with patch("cli.sync_data._load_env", return_value={
            "SWITCHBOT_TOKEN": "token",
            "SWITCHBOT_SECRET": "secret",
            "REQUEST_TIMEOUT_S": 10.0,
            "SWITCHBOT_BLE_SCAN_TIMEOUT": 5.0,
        }), patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "B0E9FE54488F", "deviceType": "MeterPro(CO2)", "deviceName": "CO2 Meter"},
        ]), patch("cli.sync_data._collect_api_readings", return_value=[api_reading]), patch("cli.sync_data.collect_ble_readings", return_value=[ble_reading]):
            result = runner.invoke(
                sync_data.app,
                ["compare", "--pair", "B0E9FE54488F=b0:e9:fe:54:48:8f"],
            )
        assert result.exit_code == 0
        assert "B0E9FE54488F (CO2 Meter)" in result.stdout
        assert "API: temp=25.0C, hum=55%" in result.stdout
        assert "BLE: temp=24.8C, hum=54%" in result.stdout
        assert "Δco2=-5" in result.stdout

    def test_devices_command_prints_status(self):
        with patch("cli.sync_data._load_env", return_value={
            "SWITCHBOT_TOKEN": "token",
            "SWITCHBOT_SECRET": "secret",
            "REQUEST_TIMEOUT_S": 10.0,
        }), patch("cli.sync_data._get_devices", return_value=[
            {"deviceId": "dev1", "deviceType": "Meter", "deviceName": "Room"},
        ]), patch("cli.sync_data._get_status", return_value={"temperature": 24.0, "humidity": 40, "battery": 95}):
            result = runner.invoke(sync_data.app, ["devices"])

        assert result.exit_code == 0
        assert "Room (type=Meter, id=dev1)" in result.stdout
        assert "battery: 95" in result.stdout
        assert "humidity: 40" in result.stdout


class TestRunLoopSelfExit:
    """BLE/D-Bus が壊れたままエラーループし続けた障害 (2026-07-06) の再発防止。"""

    BLE_ENV = {
        "SWITCHBOT_TOKEN": None,
        "SWITCHBOT_SECRET": None,
        "INFLUX_URL": "http://localhost:8428",
        "INFLUX_BUCKET_OR_DB": "db",
        "INFLUX_TOKEN": "influx-token",
        "LOCATION_PREFIX": "",
        "REQUEST_TIMEOUT_S": 10.0,
        "USE_V3_NATIVE": False,
        "EF_MODEL": "none",
        "SWITCHBOT_MODE": "ble",
        "SWITCHBOT_BLE_DEVICES": "",
        "SWITCHBOT_BLE_SCAN_TIMEOUT": 5.0,
    }

    def test_run_exits_nonzero_after_consecutive_errors(self):
        with patch("cli.sync_data._load_env", return_value=self.BLE_ENV), \
             patch("cli.sync_data._collect_once_async",
                   new=AsyncMock(side_effect=RuntimeError("dbus down"))), \
             patch("cli.sync_data.asyncio.sleep", new=AsyncMock()):
            result = runner.invoke(sync_data.app, ["run", "--interval", "60"])

        assert result.exit_code == 1
        assert result.stdout.count("error: dbus down") == sync_data.MAX_CONSECUTIVE_ERRORS
        assert "exiting so systemd can restart the service" in result.stdout

    def test_run_error_counter_resets_on_success(self):
        # 上限直前まで失敗 → 1回成功 → また上限直前まで失敗、では終了しない
        almost = sync_data.MAX_CONSECUTIVE_ERRORS - 1
        effects = (
            [RuntimeError("dbus down")] * almost
            + [["climate,location=a temperature=1.0"]]
            + [RuntimeError("dbus down")] * almost
            + [KeyboardInterrupt()]
        )
        with patch("cli.sync_data._load_env", return_value=self.BLE_ENV), \
             patch("cli.sync_data._collect_once_async", new=AsyncMock(side_effect=effects)), \
             patch("cli.sync_data._write_influx"), \
             patch("cli.sync_data.asyncio.sleep", new=AsyncMock()):
            result = runner.invoke(sync_data.app, ["run", "--interval", "60"])

        assert result.exit_code == 0
        assert "stopped" in result.stdout
        assert "exiting so systemd can restart the service" not in result.stdout
