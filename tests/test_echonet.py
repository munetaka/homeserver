"""ECHONET Lite モジュールのテスト。hex フィクスチャは 2026-07-12 に
実機 (Panasonic MKN7350S1 / ダイキン機器) から取得した実応答。"""

from unittest.mock import MagicMock, patch

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


HISTORY_HEADER = (
    "計測日時,太陽光発電(創蓄パワコン),蓄電池充電,蓄電池放電,主幹買電,主幹売電,"
    "太陽光発電(PV1),太陽光発電(PV2),HP消費電力量,燃料電池発電電力量,EV充電電力量,EV放電電力量,"
    "無効1,無効2,無効3,無効4,無効5,無効6,無効7,無効8,"
    "リビング,玄関ホール,浴室・洗面所,洋室1・2,寝室,洋室3,2F トイレ・洗面所,台所コンセント,台所コンセント,"
    "レンジフード,冷蔵庫,洗濯機,階段下・コンセント,リビング専用コンセント,洋室3 専用コンセント,寝室エアコン,"
    "2Fエアコン,洋室1 エアコン,洋室2 エアコン,HEMS電源,24H換気,食洗機,予備,電気自動車,エコキュート,"
    "分岐26,IHクッキングヒーター,分岐28,"
    "無効9,無効10,無効11,無効12,無効13,無効14,無効15,無効16,無効17,無効18,無効19,無効20,無効21,無効22,無効23,"
    "使用電力量,ガス使用量,水使用量,お湯使用量,燃料電池ガス使用量,補助熱源ガス使用量,燃料電池お湯使用量,"
    "補助熱源お湯使用量,燃料電池排熱回収量"
).split(",")

HISTORY_ROW = (
    "202607120000+0900,-,-,-,277,0,0,-,-,-,-,-,-,-,-,-,-,-,-,-,"
    "58,7,5,0,0,8,15,0,0,0,47,0,0,5,0,6,82,0,0,24,12,0,0,0,0,0,0,0,"
    "-,-,-,-,-,-,-,-,-,-,-,-,-,-,-,277,-,-,-,-,-,-,-,-"
).split(",")


class TestHistoryImport:
    def test_header_summary_and_circuits(self):
        summary, circuits = echonet.parse_history_header(HISTORY_HEADER)
        assert set(summary.values()) == {"generation", "buy", "sell", "consumption"}
        assert len(circuits) == 28
        chans = {ch: name for ch, name in circuits.values()}
        assert chans[1] == "リビング"
        assert chans[8] == "台所コンセント"
        assert chans[9] == "台所コンセント2"  # 同名の2つ目は連番でライブ収集と揃える
        assert chans[27] == "IHクッキングヒーター"

    def test_timestamp_30min_and_day(self):
        from datetime import datetime, timedelta, timezone
        jst = timezone(timedelta(hours=9))
        ts = echonet.parse_history_timestamp("202607120030+0900")
        assert ts == int(datetime(2026, 7, 12, 0, 30, tzinfo=jst).timestamp() * 1000)
        ts_day = echonet.parse_history_timestamp("20250616")
        assert ts_day == int(datetime(2025, 6, 16, tzinfo=jst).timestamp() * 1000)

    def test_row_to_readings_real_row(self):
        summary, circuits = echonet.parse_history_header(HISTORY_HEADER)
        readings = echonet.history_row_to_readings(
            HISTORY_ROW, "30min", summary, circuits, exclude={26, 28})
        by_kind = {r.tags["kind"]: r.fields["kwh"] for r in readings if r.measurement == "energy_30min"}
        assert by_kind == {"generation": 0.0, "buy": 0.277, "sell": 0.0, "consumption": 0.277}
        circ = {r.tags["name"]: r.fields["kwh"] for r in readings if r.measurement == "energy_30min_circuit"}
        assert circ["リビング"] == pytest.approx(0.058)
        assert circ["冷蔵庫"] == pytest.approx(0.047)
        assert circ["2Fエアコン"] == pytest.approx(0.082)
        assert "分岐26" not in circ and "分岐28" not in circ
        assert len(circ) == 26

    def test_row_skips_missing_values(self):
        summary, circuits = echonet.parse_history_header(HISTORY_HEADER)
        row = ["20250601"] + ["-"] * (len(HISTORY_HEADER) - 1)
        assert echonet.history_row_to_readings(row, "day", summary, circuits, set()) == []


class TestImportHistoryCommand:
    def _write_fixture(self, tmp_path):
        header = ",".join(HISTORY_HEADER)
        row30 = ",".join(HISTORY_ROW)
        day_row = HISTORY_ROW[:]
        day_row[0] = "20260710"
        day_row_future = HISTORY_ROW[:]
        day_row_future[0] = "20260712"
        (tmp_path / "30minhistory_rc_20260712.csv").write_text(
            header + "\n" + row30 + "\n", encoding="utf-8-sig")
        (tmp_path / "dayhistory_rc_202607.csv").write_text(
            header + "\n" + ",".join(day_row) + "\n" + ",".join(day_row_future) + "\n",
            encoding="utf-8-sig")

    def test_import_history_writes_and_respects_max_day(self, tmp_path):
        from typer.testing import CliRunner
        self._write_fixture(tmp_path)
        env = {
            "INFLUX_URL": "http://vm:8428", "INFLUX_BUCKET_OR_DB": "db", "INFLUX_TOKEN": "t",
            "LOCATION_PREFIX": "home-", "REQUEST_TIMEOUT_S": 10.0,
            "ECHONET_DEVICES": "", "ECHONET_TIMEOUT_S": 3.0,
            "ECHONET_CIRCUIT_NAMES": "", "ECHONET_CIRCUIT_EXCLUDE": "26,28",
        }
        with patch("cli.echonet._load_env", return_value=env), \
             patch("cli.echonet._write_influx") as write:
            result = CliRunner().invoke(
                echonet.app, ["import-history", str(tmp_path), "--max-day", "20260711"])
        assert result.exit_code == 0, result.stdout
        # 30分行(30点) + 日行1件のみ(20260712はmax-dayで除外) = 60点
        assert "imported 60 points" in result.stdout
        lines = [l for call in write.call_args_list for l in call.args[0]]
        assert any(l.startswith("energy_30min,kind=buy kwh=0.277") for l in lines)
        assert any(l.startswith("energy_day_circuit,circuit=11,name=冷蔵庫") for l in lines)
        assert not any(",circuit=26" in l or ",circuit=28" in l for l in lines)

    def test_import_history_dry_run_writes_nothing(self, tmp_path):
        from typer.testing import CliRunner
        self._write_fixture(tmp_path)
        with patch("cli.echonet._load_env", return_value={
            "INFLUX_URL": "", "INFLUX_BUCKET_OR_DB": "", "INFLUX_TOKEN": "",
            "LOCATION_PREFIX": "", "REQUEST_TIMEOUT_S": 10.0,
            "ECHONET_DEVICES": "", "ECHONET_TIMEOUT_S": 3.0,
            "ECHONET_CIRCUIT_NAMES": "", "ECHONET_CIRCUIT_EXCLUDE": "",
        }), patch("cli.echonet._write_influx") as write:
            result = CliRunner().invoke(
                echonet.app, ["import-history", str(tmp_path), "--dry-run"])
        assert result.exit_code == 0
        write.assert_not_called()


EL_ENV = {
    "INFLUX_URL": "http://vm:8428",
    "LOCATION_PREFIX": "home-",
    "REQUEST_TIMEOUT_S": 10.0,
    "ECHONET_DEVICES": "192.168.11.10@solar=太陽光",
    "ECHONET_TIMEOUT_S": 3.0,
    "ECHONET_CIRCUIT_NAMES": "",
    "ECHONET_CIRCUIT_EXCLUDE": "",
}


class TestRunCommand:
    def _reading(self):
        return Reading("power", {"location": "太陽光", "type": "solar"}, {"generation_w": 500})

    def test_run_writes_then_stops(self):
        from typer.testing import CliRunner
        effects = [([self._reading()], []), KeyboardInterrupt()]
        with patch("cli.echonet._load_env", return_value=dict(EL_ENV)), \
             patch("cli.echonet.EchonetClient"), \
             patch("cli.echonet.collect_readings", side_effect=effects), \
             patch("cli.echonet._write_influx") as write, \
             patch("cli.echonet.time.sleep"):
            result = CliRunner().invoke(echonet.app, ["run", "--interval", "60"])
        assert result.exit_code == 0
        assert "wrote 1 points" in result.stdout
        assert "stopped" in result.stdout
        write.assert_called_once()

    def test_run_self_exits_after_consecutive_failures(self):
        from typer.testing import CliRunner
        with patch("cli.echonet._load_env", return_value=dict(EL_ENV)), \
             patch("cli.echonet.EchonetClient"), \
             patch("cli.echonet.collect_readings", side_effect=RuntimeError("net down")), \
             patch("cli.echonet._write_influx") as write, \
             patch("cli.echonet.time.sleep"):
            result = CliRunner().invoke(echonet.app, ["run", "--interval", "60"])
        assert result.exit_code == 1
        assert "exiting so systemd can restart the service" in result.stdout
        write.assert_not_called()

    def test_run_counts_empty_cycles_as_errors(self):
        from typer.testing import CliRunner
        with patch("cli.echonet._load_env", return_value=dict(EL_ENV)), \
             patch("cli.echonet.EchonetClient"), \
             patch("cli.echonet.collect_readings", return_value=([], ["太陽光 (192.168.11.10): timeout"])), \
             patch("cli.echonet._write_influx"), \
             patch("cli.echonet.time.sleep"):
            result = CliRunner().invoke(echonet.app, ["run", "--interval", "60"])
        assert result.exit_code == 1
        assert "no datapoints" in result.stdout
        assert "warn: 太陽光" in result.stdout

    def test_push_no_datapoints_exits_nonzero(self):
        from typer.testing import CliRunner
        with patch("cli.echonet._load_env", return_value=dict(EL_ENV)), \
             patch("cli.echonet.EchonetClient"), \
             patch("cli.echonet.collect_readings", return_value=([], [])), \
             patch("cli.echonet._write_influx") as write:
            result = CliRunner().invoke(echonet.app, ["push"])
        assert result.exit_code == 1
        assert "no datapoints" in result.stdout
        write.assert_not_called()


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
