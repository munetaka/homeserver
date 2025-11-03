import unittest
from unittest.mock import patch

from typer.testing import CliRunner

from cli import sync_data
from cli.switchbot_ble import SwitchBotReading


class SyncDataHelpersTest(unittest.TestCase):
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
        self.assertIsNotNone(line)
        self.assertIn("temperature=25.0", line)
        self.assertIn("humidity=50.0", line)
        self.assertIn("abs_humidity=", line)
        self.assertIn("battery=82i", line)
        self.assertTrue(line.startswith("climate,location=home.Living"))

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
        self.assertEqual(len(lines), 1)
        mock_collect.assert_called_once()


class SyncDataCliTest(unittest.TestCase):
    def setUp(self):
        self.runner = CliRunner()

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
            result = self.runner.invoke(sync_data.app, ["scan-ble", "--timeout-s", "1"])

        self.assertEqual(result.exit_code, 0)
        self.assertIn("AA:BB:CC:DD:EE:FF", result.stdout)
        self.assertIn("temp=23.4C", result.stdout)
        self.assertIn("hum=55%", result.stdout)
        self.assertIn("source=switchbot", result.stdout)
        self.assertIn("type=meter", result.stdout)
        self.assertIn("model=meter", result.stdout)
        self.assertIn("code=0x54", result.stdout)


if __name__ == "__main__":
    unittest.main()
