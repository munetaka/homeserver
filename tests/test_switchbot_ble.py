import asyncio
from unittest import mock

import pytest

from cli.switchbot_ble import (
    BleTarget,
    collect_ble_readings_async,
    decode_switchbot_advertisement,
    normalize_address,
    parse_ble_target,
    parse_ble_targets,
    scan_switchbot_devices,
)


class _FakeDevice:
    def __init__(self, address, name="", rssi=-60, metadata=None):
        self.address = address
        self.name = name
        self.rssi = rssi
        self.metadata = metadata or {}


class _FakeAdvertisement:
    def __init__(self, service_data=None, manufacturer_data=None, rssi=-60):
        self.service_data = service_data or {}
        self.manufacturer_data = manufacturer_data or {}
        self.rssi = rssi


class TestSwitchBotBleParsing:
    def test_parse_single_target(self):
        target = parse_ble_target("AA:BB:CC:DD:EE:FF@co2=Living")
        assert target.mac == "AA:BB:CC:DD:EE:FF"
        assert target.device_type == "co2"
        assert target.alias == "Living"

    def test_parse_multiple_targets(self):
        specs = ["AA:BB:CC:DD:EE:01", "aa:bb:cc:dd:ee:02@meter=Kitchen"]
        targets = parse_ble_targets(specs)
        assert len(targets) == 2
        assert targets[0].alias == "AA:BB:CC:DD:EE:01"
        assert targets[1].alias == "Kitchen"
        assert targets[1].device_type == "meter"

    def test_parse_uuid_address(self):
        uid = "8de2d14e-3b1c-2e66-d617-17f304c864ea"
        target = parse_ble_target(f"{uid}=Sensor")
        assert target.mac == uid.upper()

    def test_decode_meter_payload(self):
        payload = bytes.fromhex("540064")
        manufacturer = bytes.fromhex("f2b202064a8ba90208973a00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer},
        )
        target = BleTarget(mac="AA:BB:CC:DD:EE:FF", device_type="meter", alias="Living")
        reading = decode_switchbot_advertisement(target, adv)
        assert reading is not None
        assert reading.temperature == pytest.approx(23.8)
        assert reading.humidity == pytest.approx(58.0)
        assert reading.battery == 100
        assert reading.device_id == "AA:BB:CC:DD:EE:FF"
        assert reading.name == "Living"

    def test_decode_meter_payload_fahrenheit_display_stays_celsius(self):
        # Same layout as test_decode_meter_payload but with bit 7 of the
        # humidity byte set (device display set to Fahrenheit). The encoded
        # value is Celsius regardless of the display unit.
        manufacturer = bytes.fromhex("f2b202064a8ba9020897ba00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": bytes.fromhex("5400e4")},
            manufacturer_data={0x0969: manufacturer},
        )
        target = BleTarget(mac="AA:BB:CC:DD:EE:FF", device_type="meter", alias="WIC")
        reading = decode_switchbot_advertisement(target, adv)
        assert reading is not None
        assert reading.temperature == pytest.approx(23.8)
        assert reading.humidity == pytest.approx(58.0)

    def test_decode_hub2_payload(self):
        # Real advertisement captured from a Hub 2 (MAC D3:27:F3:64:6B:34)
        # on 2026-07-06: temperature 25.9 C, humidity 59 %. Bytes 0-5 of the
        # manufacturer data are the MAC, byte 11 is a sequence counter, and
        # bytes 13-15 carry the meter-style temperature/humidity block.
        payload = bytes.fromhex("7600")
        manufacturer = bytes.fromhex("d327f3646b3400ff6a4b93250509993b00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805f9b34fb": payload},
            manufacturer_data={0x0969: manufacturer},
        )
        target = BleTarget(mac="D3:27:F3:64:6B:34", device_type="hub2", alias="Utility")
        reading = decode_switchbot_advertisement(target, adv)
        assert reading is not None
        assert reading.temperature == pytest.approx(25.9)
        assert reading.humidity == pytest.approx(59.0)
        assert reading.co2 is None
        # Hub 2 is mains powered and does not report battery.
        assert reading.battery is None
        assert reading.device_id == "D3:27:F3:64:6B:34"
        assert reading.name == "Utility"

    def test_decode_hub2_short_manufacturer_data_has_no_values(self):
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805f9b34fb": bytes.fromhex("7600")},
            manufacturer_data={0x0969: bytes.fromhex("d327f3646b3400ff6a4b93")},
        )
        target = BleTarget(mac="D3:27:F3:64:6B:34", device_type="hub2", alias="Utility")
        reading = decode_switchbot_advertisement(target, adv)
        assert reading is not None
        assert reading.temperature is None
        assert reading.humidity is None

    def test_parse_target_hub2_type(self):
        target = parse_ble_target("D3:27:F3:64:6B:34@hub2=Utility")
        assert target.device_type == "hub2"
        assert target.alias == "Utility"

    def test_scan_switchbot_devices_hub2(self):
        payload = bytes.fromhex("7600")
        manufacturer_payload = bytes.fromhex("d327f3646b3400ff6a4b93250509993b00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805f9b34fb": payload},
            manufacturer_data={0x0969: manufacturer_payload},
            rssi=-79,
        )
        device = _FakeDevice(address="D3:27:F3:64:6B:34", name="Hub 2")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        assert info["is_switchbot"] is True
        assert info["device_type"] == "hub2"
        assert info["device_model"] == "hub2"
        assert info["device_code"] == 0x76
        assert info["reading"].temperature == pytest.approx(25.9)
        assert info["reading"].humidity == pytest.approx(59.0)

    def test_collect_ble_readings_async_runs_on_caller_loop(self):
        # The run loop awaits this from one persistent event loop; make sure it
        # scans and decodes without spawning its own loop (no _run_coroutine).
        payload = bytes.fromhex("540064")
        manufacturer = bytes.fromhex("f2b202064a8ba90208973a00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer},
        )
        device = _FakeDevice(address="F2:B2:02:06:4A:8B")
        target = BleTarget(mac="F2:B2:02:06:4A:8B", device_type="meter", alias="Toilet")

        async def fake_discover(timeout):
            return [(device, adv)]

        with mock.patch(
            "cli.switchbot_ble._discover_with_advertisements", side_effect=fake_discover
        ), mock.patch("cli.switchbot_ble._run_coroutine") as run_coroutine:
            readings = asyncio.run(collect_ble_readings_async([target], 3.0))

        run_coroutine.assert_not_called()
        assert len(readings) == 1
        assert readings[0].name == "Toilet"
        assert readings[0].temperature == pytest.approx(23.8)

    def test_collect_ble_readings_async_requires_targets(self):
        with pytest.raises(RuntimeError):
            asyncio.run(collect_ble_readings_async([], 3.0))

    def test_decode_co2_payload(self):
        payload = bytes.fromhex("350064")
        manufacturer = bytes.fromhex("b0e9fe54488ffde403983a0026035d00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer},
        )
        target = BleTarget(mac="AA:BB:CC:DD:EE:00", device_type="co2", alias="Office")
        reading = decode_switchbot_advertisement(target, adv)
        assert reading is not None
        assert reading.temperature == pytest.approx(24.3, abs=0.05)
        assert reading.humidity == pytest.approx(58.0, abs=0.05)
        assert reading.co2 == 0x035D
        assert reading.battery == 100

    def test_normalize_address_accepts_uuid(self):
        uuid_addr = "8de2d14e-3b1c-2e66-d617-17f304c864ea"
        assert normalize_address(uuid_addr) == uuid_addr.upper()

    def test_normalize_address_accepts_hex32(self):
        hex_addr = "8de2d14e3b1c2e66d61717f304c864ea"
        assert normalize_address(hex_addr) == hex_addr.upper()

    def test_scan_switchbot_devices_aggregates(self):
        payload = bytes.fromhex("540064")
        manufacturer_payload = bytes.fromhex("aabbccddee00360302963700")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer_payload},
            rssi=-70,
        )
        device = _FakeDevice(address="AA:BB:CC:DD:EE:FF", name="Thermo")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        assert len(devices) == 1
        info = devices[0]
        assert info["mac"] == "AA:BB:CC:DD:EE:FF"
        assert info["name"] == "Thermo"
        assert info["device_type"] == "meter"
        assert info["device_model"] == "meter"
        assert info["device_code"] == 0x54
        assert info["rssi"] == -70
        assert info["reading"] is not None
        assert info["is_switchbot"] is True

    def test_scan_switchbot_devices_unknown_type(self):
        payload = bytes([0x99, 0x00, 80])
        key = "0000fd3d-0000-1000-8000-00805F9B34FB"
        adv = _FakeAdvertisement(service_data={key: payload})
        device = _FakeDevice(address="8DE2D14E-3B1C-2E66-D617-17F304C864EA")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        assert info["is_switchbot"] is True
        assert info["device_type"] == "unknown"
        assert info["reading"] is None

    def test_scan_switchbot_devices_unknown_code_with_meter_layout(self):
        payload = bytes([0x51, 0x00, 92, 0x02, 0x97, 0x32])
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload}
        )
        device = _FakeDevice(address="AA:AA:AA:AA:AA:AA", name="UnknownMeter")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        assert info["is_switchbot"] is True
        assert info["device_type"] == "unknown"
        assert info["device_model"] is None
        assert info["reading"] is None

    def test_scan_switchbot_devices_co2(self):
        payload = bytes.fromhex("350064")
        manufacturer_payload = bytes.fromhex("b0e9fe54488ffde403983a0026035d00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer_payload},
            rssi=-55,
        )
        device = _FakeDevice(address="E1:89:AF:42:E5:51", name="CO2")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        assert info["device_type"] == "co2"
        assert info["device_model"] == "co2_meter"
        assert info["device_code"] == 0x35
        assert info["reading"].co2 == 0x035D

    def test_scan_switchbot_devices_other_vendor(self):
        adv = _FakeAdvertisement(manufacturer_data={0x004C: b"\x02\x15"})
        device = _FakeDevice(address="C9B94B1E-518F-0823-6396-47CC48557242", name="Other")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        assert info["is_switchbot"] is False
        assert info["device_type"] is None
        assert info["reading"] is None
