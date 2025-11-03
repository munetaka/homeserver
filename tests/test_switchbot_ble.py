import unittest
from unittest import mock

from cli.switchbot_ble import (
    BleTarget,
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


class SwitchBotBleParsingTest(unittest.TestCase):
    def test_parse_single_target(self):
        target = parse_ble_target("AA:BB:CC:DD:EE:FF@co2=Living")
        self.assertEqual(target.mac, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(target.device_type, "co2")
        self.assertEqual(target.alias, "Living")

    def test_parse_multiple_targets(self):
        specs = ["AA:BB:CC:DD:EE:01", "aa:bb:cc:dd:ee:02@meter=Kitchen"]
        targets = parse_ble_targets(specs)
        self.assertEqual(len(targets), 2)
        self.assertEqual(targets[0].alias, "AA:BB:CC:DD:EE:01")
        self.assertEqual(targets[1].alias, "Kitchen")
        self.assertEqual(targets[1].device_type, "meter")

    def test_parse_uuid_address(self):
        uid = "8de2d14e-3b1c-2e66-d617-17f304c864ea"
        target = parse_ble_target(f"{uid}=Sensor")
        self.assertEqual(target.mac, uid.upper())

    def test_decode_meter_payload(self):
        payload = bytes.fromhex("540064")
        manufacturer = bytes.fromhex("f2b202064a8ba90208973a00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer},
        )
        target = BleTarget(mac="AA:BB:CC:DD:EE:FF", device_type="meter", alias="Living")
        reading = decode_switchbot_advertisement(target, adv)
        self.assertIsNotNone(reading)
        self.assertAlmostEqual(reading.temperature, 23.8)
        self.assertAlmostEqual(reading.humidity, 58.0)
        self.assertEqual(reading.battery, 100)
        self.assertEqual(reading.device_id, "AA:BB:CC:DD:EE:FF")
        self.assertEqual(reading.name, "Living")

    def test_decode_co2_payload(self):
        payload = bytes.fromhex("350064")
        manufacturer = bytes.fromhex("b0e9fe54488ffde403983a0026035d00")
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload},
            manufacturer_data={0x0969: manufacturer},
        )
        target = BleTarget(mac="AA:BB:CC:DD:EE:00", device_type="co2", alias="Office")
        reading = decode_switchbot_advertisement(target, adv)
        self.assertIsNotNone(reading)
        self.assertAlmostEqual(reading.temperature, 24.3, places=1)
        self.assertAlmostEqual(reading.humidity, 58.0, places=1)
        self.assertEqual(reading.co2, 0x035D)
        self.assertEqual(reading.battery, 100)

    def test_normalize_address_accepts_uuid(self):
        uuid_addr = "8de2d14e-3b1c-2e66-d617-17f304c864ea"
        self.assertEqual(normalize_address(uuid_addr), uuid_addr.upper())

    def test_normalize_address_accepts_hex32(self):
        hex_addr = "8de2d14e3b1c2e66d61717f304c864ea"
        self.assertEqual(normalize_address(hex_addr), hex_addr.upper())

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

        self.assertEqual(len(devices), 1)
        info = devices[0]
        self.assertEqual(info["mac"], "AA:BB:CC:DD:EE:FF")
        self.assertEqual(info["name"], "Thermo")
        self.assertEqual(info["device_type"], "meter")
        self.assertEqual(info["device_model"], "meter")
        self.assertEqual(info["device_code"], 0x54)
        self.assertEqual(info["rssi"], -70)
        self.assertIsNotNone(info["reading"])
        self.assertTrue(info["is_switchbot"])

    def test_scan_switchbot_devices_unknown_type(self):
        payload = bytes([0x99, 0x00, 80])
        key = "0000fd3d-0000-1000-8000-00805F9B34FB"
        adv = _FakeAdvertisement(service_data={key: payload})
        device = _FakeDevice(address="8DE2D14E-3B1C-2E66-D617-17F304C864EA")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        self.assertTrue(info["is_switchbot"])
        self.assertEqual(info["device_type"], "unknown")
        self.assertIsNone(info["reading"])

    def test_scan_switchbot_devices_unknown_code_with_meter_layout(self):
        payload = bytes([0x51, 0x00, 92, 0x02, 0x97, 0x32])
        adv = _FakeAdvertisement(
            service_data={"0000fd3d-0000-1000-8000-00805F9B34FB": payload}
        )
        device = _FakeDevice(address="AA:AA:AA:AA:AA:AA", name="UnknownMeter")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        self.assertTrue(info["is_switchbot"])
        self.assertEqual(info["device_type"], "unknown")
        self.assertIsNone(info["device_model"])
        self.assertIsNone(info["reading"])

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
        self.assertEqual(info["device_type"], "co2")
        self.assertEqual(info["device_model"], "co2_meter")
        self.assertEqual(info["device_code"], 0x35)
        self.assertEqual(info["reading"].co2, 0x035D)

    def test_scan_switchbot_devices_other_vendor(self):
        adv = _FakeAdvertisement(manufacturer_data={0x004C: b"\x02\x15"})
        device = _FakeDevice(address="C9B94B1E-518F-0823-6396-47CC48557242", name="Other")

        with mock.patch("cli.switchbot_ble._run_coroutine", return_value=[(device, adv)]):
            devices = scan_switchbot_devices(3.0)

        info = devices[0]
        self.assertFalse(info["is_switchbot"])
        self.assertIsNone(info["device_type"])
        self.assertIsNone(info["reading"])


if __name__ == "__main__":
    unittest.main()
