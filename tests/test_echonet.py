"""ECHONET Lite モジュールのテスト。hex フィクスチャは 2026-07-12 に
実機 (Panasonic MKN7350S1 / ダイキン機器) から取得した実応答。"""

from unittest.mock import MagicMock

import pytest

from cli import echonet
from cli.echonet import (
    ElTarget,
    Reading,
    build_get_frame,
    decode_circuit_list,
    decode_property_map,
    parse_el_target,
    parse_response,
    read_aircon,
    read_ecocute,
    read_powerboard,
    read_solar,
    readings_to_lines,
)

# 実機フィクスチャ
BOARD_B7 = bytes.fromhex(
    "011c000000260000000f0000000700000000000000000000000d000000070000000000000000"
    "0000000000000066000000000000000000000000000000000000011a000000b5000000000000"
    "0000000000310000001900000000000000000000000000000000000000000000000000000000"
)
BOARD_MAP = bytes.fromhex("49f9f9f9e9e0e0f9fafbe96960616a6a62")
AIRCON_MAP = bytes.fromhex("160d01010f090100000101090800020a03")


class TestFrame:
    def test_build_get_frame(self):
        frame = build_get_frame(0x0010, bytes([0x02, 0x79, 0x01]), [0xE0, 0xE1])
        assert frame.hex() == "1081001005ff010279016202e000e100"

    def test_parse_response_solar(self):
        # 実機応答の再構成: TID=0x10, SEOJ=solar, ESV=Get_Res, E0=749W, E1=7828523
        data = bytes.fromhex("1081001002790105ff017202e00202ede1040077742b")
        parsed = parse_response(data)
        assert parsed is not None
        tid, seoj, esv, props = parsed
        assert tid == 0x10
        assert seoj == bytes([0x02, 0x79, 0x01])
        assert esv == 0x72
        assert int.from_bytes(props[0xE0], "big") == 749
        assert int.from_bytes(props[0xE1], "big") == 7828523

    def test_parse_response_rejects_garbage(self):
        assert parse_response(b"\x00\x01\x02") is None
        assert parse_response(b"") is None


class TestDecoders:
    def test_property_map_bitmap_format(self):
        epcs = decode_property_map(BOARD_MAP)
        assert len(epcs) == 0x49  # 宣言数と一致
        for expected in (0x80, 0xB7, 0xC0, 0xC1, 0xC2, 0xC6, 0x9F):
            assert expected in epcs

    def test_property_map_aircon_has_power(self):
        epcs = decode_property_map(AIRCON_MAP)
        assert 0x84 in epcs
        assert 0xBB in epcs

    def test_circuit_list_real_payload(self):
        circuits = decode_circuit_list(BOARD_B7)
        assert len(circuits) == 28
        nonzero = {ch: w for ch, w in circuits.items() if w}
        assert nonzero == {1: 38, 2: 15, 3: 7, 6: 13, 7: 7, 11: 102, 16: 282, 17: 181, 20: 49, 21: 25}
        assert circuits[4] == 0

    def test_circuit_list_skips_unmeasurable_marker(self):
        payload = bytes([1, 2]) + (0x7FFFFFFE).to_bytes(4, "big") + (100).to_bytes(4, "big")
        assert decode_circuit_list(payload) == {2: 100}

    def test_circuit_list_short_payload(self):
        assert decode_circuit_list(b"") == {}
        assert decode_circuit_list(b"\x01") == {}


class TestTargetParse:
    def test_parse_full_spec(self):
        t = parse_el_target("192.168.11.10@powerboard=分電盤")
        assert t == ElTarget(ip="192.168.11.10", device_type="powerboard", alias="分電盤")

    def test_parse_without_alias_uses_ip(self):
        t = parse_el_target("192.168.11.12@aircon")
        assert t.alias == "192.168.11.12"

    @pytest.mark.parametrize("spec", ["", "192.168.11.10", "192.168.11.10@fridge", "notanip@solar"])
    def test_parse_rejects_invalid(self, spec):
        with pytest.raises(ValueError):
            parse_el_target(spec)


class TestReaders:
    def _client(self, props):
        client = MagicMock()
        client.get.return_value = props
        return client

    def test_read_solar(self):
        client = self._client({0xE0: bytes.fromhex("02ed"), 0xE1: bytes.fromhex("0077742b")})
        readings = read_solar(client, ElTarget("192.168.11.10", "solar", "太陽光"))
        assert len(readings) == 1
        assert readings[0].fields["generation_w"] == 749
        assert readings[0].fields["generation_total_kwh"] == pytest.approx(7828.523)

    def test_read_powerboard_real_values(self):
        client = self._client({
            0xC2: bytes.fromhex("02"),
            0xC6: bytes.fromhex("0000008c"),
            0xC0: bytes.fromhex("0006648f"),
            0xC1: bytes.fromhex("0006fd9a"),
            0xB7: BOARD_B7,
        })
        readings = read_powerboard(client, ElTarget("192.168.11.10", "powerboard", "分電盤"))
        main = readings[0]
        assert main.fields["grid_w"] == 140
        assert main.fields["buy_total_kwh"] == pytest.approx(4189.59)
        assert main.fields["sell_total_kwh"] == pytest.approx(4581.38)
        circuits = [r for r in readings if r.measurement == "power_circuit"]
        assert len(circuits) == 28
        ch16 = next(r for r in circuits if r.tags["circuit"] == "16")
        assert ch16.tags == {"location": "分電盤", "circuit": "16"}
        assert ch16.fields["watts"] == 282

    def test_read_powerboard_negative_grid_means_selling(self):
        client = self._client({0xC6: bytes.fromhex("fffffd12")})  # -750W = 売電中
        readings = read_powerboard(client, ElTarget("192.168.11.10", "powerboard", "分電盤"))
        assert readings[0].fields["grid_w"] == -750

    def test_read_aircon_skips_undefined_setpoint(self):
        client = self._client({
            0x80: b"\x30",
            0x84: bytes.fromhex("012c"),
            0xB3: b"\xfd",  # 自動運転時の未定義値
            0xBB: b"\x18",
            0xBE: b"\x1c",
        })
        readings = read_aircon(client, ElTarget("192.168.11.181", "aircon", "エアコンB"))
        f = readings[0].fields
        assert f["on"] == 1
        assert f["power_w"] == 300
        assert f["room_temp"] == 24.0
        assert f["outdoor_temp"] == 28.0
        assert "setpoint" not in f

    def test_read_ecocute(self):
        client = self._client({0x80: b"\x30", 0x84: bytes.fromhex("0064"), 0xE1: bytes.fromhex("01f4")})
        readings = read_ecocute(client, ElTarget("192.168.11.169", "ecocute", "エコキュート"))
        f = readings[0].fields
        assert f == {"on": 1, "power_w": 100, "tank_l": 500}


class TestCircuitConfig:
    def test_parse_circuit_names(self):
        names = echonet.parse_circuit_names("1=リビング, 11=冷蔵庫 ,27=IHクッキングヒーター")
        assert names == {1: "リビング", 11: "冷蔵庫", 27: "IHクッキングヒーター"}
        assert echonet.parse_circuit_names("") == {}

    def test_parse_circuit_names_rejects_invalid(self):
        with pytest.raises(ValueError):
            echonet.parse_circuit_names("リビング")

    def test_apply_circuit_config_names_and_excludes(self):
        readings = [
            Reading("power", {"location": "分電盤", "type": "powerboard"}, {"grid_w": 100}),
            Reading("power_circuit", {"location": "分電盤", "circuit": "01"}, {"watts": 38}),
            Reading("power_circuit", {"location": "分電盤", "circuit": "26"}, {"watts": 0}),
        ]
        out = echonet.apply_circuit_config(readings, {1: "リビング"}, {26, 28})
        assert len(out) == 2  # 26 は除外
        assert out[1].tags == {"location": "分電盤", "circuit": "01", "name": "リビング"}
        assert out[0].measurement == "power"  # 回路以外は素通し


class TestLineProtocol:
    def test_readings_to_lines_prefix_and_types(self):
        readings = [
            Reading("power", {"location": "太陽光", "type": "solar"},
                    {"generation_w": 749, "generation_total_kwh": 7828.523}),
            Reading("power_circuit", {"location": "分電盤", "circuit": "19"}, {"watts": 282}),
        ]
        lines = readings_to_lines(readings, "home-", ts_ms=1000)
        assert lines[0].startswith("power,location=home-太陽光,type=solar ")
        assert "generation_w=749i" in lines[0]
        assert "generation_total_kwh=7828.523" in lines[0]
        assert lines[0].endswith(" 1000000000")
        assert lines[1] == "power_circuit,location=home-分電盤,circuit=19 watts=282i 1000000000"

    def test_escaping_spaces_and_commas(self):
        readings = [Reading("power", {"location": "a b,c"}, {"grid_w": 1})]
        lines = readings_to_lines(readings, "", ts_ms=0)
        assert lines[0].startswith(r"power,location=a\ b\,c ")
