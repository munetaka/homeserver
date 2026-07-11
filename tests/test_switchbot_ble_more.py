# tests/test_switchbot_ble_more.py
"""カバレッジ拡充テスト (pytest スタイル)。

対象: _decode_meter_bytes のプロパティテスト (hypothesis) /
normalize_address・parse_ble_target のエラーパス / _run_coroutine の
フォールバック / _scan_once_async のアドレス照合。
実 BLE ハードウェアには一切触らない。
"""

import asyncio
from unittest.mock import patch

import pytest
from hypothesis import given
from hypothesis import strategies as st

from cli import switchbot_ble
from cli.switchbot_ble import (
    BleTarget,
    collect_ble_readings,
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


SERVICE_KEY = "0000fd3d-0000-1000-8000-00805f9b34fb"


# ---------------------------------------------------------------------
# _decode_meter_bytes / ペイロード解析のプロパティテスト
# ---------------------------------------------------------------------

class TestDecodeMeterBytesProperties:
    @given(data=st.binary(max_size=20))
    def test_never_raises_and_values_bounded(self, data):
        result = switchbot_ble._decode_meter_bytes(data)
        if len(data) < 6:
            assert result == {}
            return
        assert set(result) == {"temperature", "humidity"}
        # 整数部7bit + 小数部(0-15)*0.1 が符号付きで入る
        assert abs(result["temperature"]) <= 127 + 1.5
        assert 0 <= result["humidity"] <= 127

    @given(payload=st.binary(max_size=20), manufacturer=st.none() | st.binary(max_size=24))
    def test_service_payload_parsing_never_raises(self, payload, manufacturer):
        code, model, category, values = switchbot_ble._parse_switchbot_service_payload(
            payload, manufacturer
        )
        if not payload:
            assert (code, model, category, values) == (None, None, None, {})
        else:
            assert code == payload[0] & 0x7F
            assert isinstance(values, dict)

    def test_negative_temperature(self):
        # temp_byte の bit7 が立っていない → 負の温度
        # data[2]=decimals, data[3]=temp, data[4]=humidity
        data = bytes([0, 0, 0x05, 0x0A, 0x28, 0])
        result = switchbot_ble._decode_meter_bytes(data)
        assert result["temperature"] == pytest.approx(-10.5)
        assert result["humidity"] == 40.0

    def test_short_manufacturer_payload_is_ignored(self):
        assert switchbot_ble._meter_data_from_manufacturer(None) is None
        assert switchbot_ble._meter_data_from_manufacturer(b"\x00" * 11) is None
        assert switchbot_ble._meter_data_from_manufacturer(b"\x00" * 12) == b"\x00" * 6


# ---------------------------------------------------------------------
# normalize_address / parse_ble_target のエラーパス
# ---------------------------------------------------------------------

class TestNormalizeAddressErrors:
    @pytest.mark.parametrize("addr", ["", "   ", None])
    def test_empty_address_raises(self, addr):
        with pytest.raises(ValueError, match="Empty address"):
            normalize_address(addr)

    @pytest.mark.parametrize("addr", [
        "GG:HH:II:JJ:KK:LL",          # 16進ではない
        "AA:BB:CC:DD:EE",             # 桁不足
        "AA:BB:CC:DD:EE:FF:00",       # 桁過多
        "12345",                      # ただの数字
    ])
    def test_unsupported_format_raises(self, addr):
        with pytest.raises(ValueError, match="Unsupported BLE address format"):
            normalize_address(addr)

    def test_dash_separated_mac_is_normalized(self):
        assert normalize_address("aa-bb-cc-dd-ee-ff") == "AA:BB:CC:DD:EE:FF"


class TestParseBleTargetErrors:
    def test_empty_spec_raises(self):
        with pytest.raises(ValueError, match="Empty BLE target specification"):
            parse_ble_target("")

    def test_unsupported_device_type_raises(self):
        with pytest.raises(ValueError, match="Unsupported device type 'plug'"):
            parse_ble_target("AA:BB:CC:DD:EE:FF@plug")

    def test_empty_alias_falls_back_to_mac(self):
        target = parse_ble_target("AA:BB:CC:DD:EE:FF=")
        assert target.alias == "AA:BB:CC:DD:EE:FF"

    def test_empty_type_falls_back_to_default(self):
        target = parse_ble_target("AA:BB:CC:DD:EE:FF@=Living")
        assert target.device_type == "meter"
        assert target.alias == "Living"

    def test_parse_ble_targets_skips_blank_entries(self):
        targets = parse_ble_targets(["", "  ", "AA:BB:CC:DD:EE:FF"])
        assert len(targets) == 1


# ---------------------------------------------------------------------
# _run_coroutine のフォールバック
# ---------------------------------------------------------------------

class TestRunCoroutine:
    async def _value(self):
        return 42

    def test_happy_path_uses_asyncio_run(self):
        assert switchbot_ble._run_coroutine(self._value) == 42

    def test_falls_back_to_new_loop_when_asyncio_run_unavailable(self):
        def fake_run(coro):
            coro.close()  # "never awaited" 警告を避ける
            raise RuntimeError("asyncio.run() cannot be called from a running event loop")

        with patch("cli.switchbot_ble.asyncio.run", side_effect=fake_run):
            assert switchbot_ble._run_coroutine(self._value) == 42

    def test_other_runtime_error_is_reraised(self):
        def fake_run(coro):
            coro.close()
            raise RuntimeError("boom")

        with patch("cli.switchbot_ble.asyncio.run", side_effect=fake_run):
            with pytest.raises(RuntimeError, match="boom"):
                switchbot_ble._run_coroutine(self._value)


# ---------------------------------------------------------------------
# アドバタイズ解析・スキャンの分岐
# ---------------------------------------------------------------------

def _meter_adv():
    payload = bytes.fromhex("540064")
    manufacturer = bytes.fromhex("f2b202064a8ba90208973a00")
    return _FakeAdvertisement(
        service_data={SERVICE_KEY: payload},
        manufacturer_data={0x0969: manufacturer},
    )


class TestDecodeAdvertisementBranches:
    def test_non_switchbot_advertisement_returns_none(self):
        target = BleTarget(mac="AA:BB:CC:DD:EE:FF", device_type="meter", alias="Living")
        adv = _FakeAdvertisement(manufacturer_data={0x004C: b"\x02\x15"})
        assert decode_switchbot_advertisement(target, adv) is None

    def test_category_mismatch_returns_none(self):
        target = BleTarget(mac="F2:B2:02:06:4A:8B", device_type="co2", alias="Office")
        assert decode_switchbot_advertisement(target, _meter_adv()) is None

    def test_empty_service_payload_is_not_switchbot(self):
        adv = _FakeAdvertisement(service_data={SERVICE_KEY: b""})
        analysis = switchbot_ble.analyze_switchbot_advertisement(adv)
        assert analysis["is_switchbot"] is False


class TestScanMatching:
    def test_hardware_map_matches_uuid_address(self):
        # macOS はランダム UUID をアドレスとして返すため、manufacturer data の
        # 先頭6バイト (実MAC) で設定済みターゲットへ突き合わせる
        target = BleTarget(mac="F2:B2:02:06:4A:8B", device_type="meter", alias="Toilet")
        device = _FakeDevice(address="8DE2D14E-3B1C-2E66-D617-17F304C864EA")

        async def fake_discover(timeout):
            return [(device, _meter_adv())]

        with patch("cli.switchbot_ble._discover_with_advertisements", side_effect=fake_discover):
            readings = asyncio.run(collect_ble_readings_async([target], 3.0))

        assert len(readings) == 1
        assert readings[0].name == "Toilet"

    def test_uuid_target_matches_by_address(self):
        # UUID を MAC として設定したターゲットは hardware_map には入らないが
        # (コロン区切りでないため)、アドレス一致で照合できる
        uid = "8DE2D14E-3B1C-2E66-D617-17F304C864EA"
        target = BleTarget(mac=uid, device_type="meter", alias="Desk")
        device = _FakeDevice(address=uid)

        async def fake_discover(timeout):
            return [(device, _meter_adv())]

        with patch("cli.switchbot_ble._discover_with_advertisements", side_effect=fake_discover):
            readings = asyncio.run(collect_ble_readings_async([target], 3.0))

        assert len(readings) == 1
        assert readings[0].name == "Desk"

    def test_invalid_address_and_unknown_device_are_skipped(self):
        target = BleTarget(mac="AA:BB:CC:DD:EE:01", device_type="meter", alias="Living")
        bad_device = _FakeDevice(address="not-an-address")
        stranger = _FakeDevice(address="AA:BB:CC:DD:EE:99")  # ターゲット外

        async def fake_discover(timeout):
            return [(bad_device, _meter_adv()), (stranger, _FakeAdvertisement())]

        with patch("cli.switchbot_ble._discover_with_advertisements", side_effect=fake_discover):
            readings = asyncio.run(collect_ble_readings_async([target], 3.0))

        assert readings == []

    def test_sync_wrapper_runs_scan(self):
        target = BleTarget(mac="F2:B2:02:06:4A:8B", device_type="meter", alias="Toilet")
        device = _FakeDevice(address="F2:B2:02:06:4A:8B")

        async def fake_discover(timeout):
            return [(device, _meter_adv())]

        with patch("cli.switchbot_ble._discover_with_advertisements", side_effect=fake_discover):
            readings = collect_ble_readings([target], 3.0)

        assert len(readings) == 1
        assert readings[0].temperature == pytest.approx(23.8)

    def test_scan_switchbot_devices_skips_invalid_address(self):
        bad_device = _FakeDevice(address="not-an-address")
        with patch("cli.switchbot_ble._run_coroutine", return_value=[(bad_device, _meter_adv())]):
            assert scan_switchbot_devices(3.0) == []
