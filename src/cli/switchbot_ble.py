"""
Bluetooth Low Energy helpers for SwitchBot environmental sensors.

The BLE payload formats are based on publicly documented reverse engineering
efforts and the upstream SwitchBot mobile applications. Temperature is encoded
in 0.1 °C increments as a signed little-endian 16-bit integer, while humidity,
CO2, and battery data are encoded as unsigned integers.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass
from typing import Any, Callable, Coroutine, Iterable, Optional, TypeVar

# Supported BLE sensor types.
SUPPORTED_DEVICE_TYPES = {"meter", "co2"}
DEFAULT_DEVICE_TYPE = "meter"

SWITCHBOT_SERVICE_UUID_FRAGMENT = "FD3D"
SWITCHBOT_MANUFACTURER_ID = 0x0969  # decimal 2409

DEVICE_CODE_MAP: dict[int, tuple[str, Optional[str]]] = {
    0x48: ("bot", None),
    0x54: ("meter", "meter"),
    0x69: ("meter_plus", "meter"),
    0x74: ("meter_add", "meter"),
    0x75: ("meter_simple", "meter"),
    0x77: ("meter_outdoor", "meter"),
    0x35: ("co2_meter", "co2"),
    0x79: ("air_quality_monitor", "co2"),
}

_MAC_RE = re.compile(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$")
_UUID_RE = re.compile(r"^[0-9A-F]{8}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{4}-[0-9A-F]{12}$")
_HEX32_RE = re.compile(r"^[0-9A-F]{32}$")


@dataclass(frozen=True)
class BleTarget:
    """Configuration for a SwitchBot device to capture via BLE."""

    mac: str
    device_type: str
    alias: str


@dataclass
class SwitchBotReading:
    """SwitchBot sensor measurement."""

    device_id: str
    name: str
    device_type: str
    temperature: Optional[float] = None
    humidity: Optional[float] = None
    co2: Optional[int] = None
    battery: Optional[int] = None


def normalize_address(addr: str) -> str:
    """
    Normalize BLE addresses. SwitchBot devices on macOS may expose randomized UUIDs
    instead of traditional MAC addresses, so we accept both.
    """
    s = (addr or "").strip().upper()
    if not s:
        raise ValueError("Empty address")

    candidate = s.replace("-", ":")
    if _MAC_RE.match(candidate):
        return candidate

    if _UUID_RE.match(s):
        return s

    compact = s.replace(":", "").replace("-", "")
    if _HEX32_RE.match(compact):
        # Return uppercase 32 hex chars without separators.
        return compact

    raise ValueError(f"Unsupported BLE address format: {addr}")


def parse_ble_target(spec: str) -> BleTarget:
    """
    Parse a target specification of the form ``MAC[@type][=alias]``.

    Example inputs:
        ``AA:BB:CC:DD:EE:FF``
        ``AA:BB:CC:DD:EE:FF@co2``
        ``AA:BB:CC:DD:EE:FF=LivingRoom``
        ``AA:BB:CC:DD:EE:FF@meter=Kitchen``
    """
    if not spec:
        raise ValueError("Empty BLE target specification")

    alias = None
    target_type = DEFAULT_DEVICE_TYPE
    mac_part = spec

    if "=" in spec:
        left, alias = spec.split("=", 1)
        mac_part = left.strip()
        alias = alias.strip() or None

    if "@" in mac_part:
        mac_only, type_part = mac_part.split("@", 1)
        mac_part = mac_only.strip()
        type_part = type_part.strip().lower()
        if type_part:
            target_type = type_part
    else:
        mac_part = mac_part.strip()

    mac = normalize_address(mac_part)
    if target_type not in SUPPORTED_DEVICE_TYPES:
        raise ValueError(f"Unsupported device type '{target_type}' in '{spec}'")
    if not alias:
        alias = mac
    return BleTarget(mac=mac, device_type=target_type, alias=alias)


def parse_ble_targets(values: Iterable[str]) -> list[BleTarget]:
    targets: list[BleTarget] = []
    for raw in values:
        spec = raw.strip()
        if not spec:
            continue
        targets.append(parse_ble_target(spec))
    return targets


def _meter_data_from_manufacturer(manufacturer_payload: Optional[bytes]) -> Optional[bytes]:
    if not manufacturer_payload or len(manufacturer_payload) < 12:
        return None
    data = manufacturer_payload[6:]
    if len(data) < 6:
        return None
    return data


def _decode_meter_bytes(data: bytes) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    if len(data) < 6:
        return result

    decimals = data[2] & 0x0F
    temp_byte = data[3]
    humidity_byte = data[4]

    integer = temp_byte & 0x7F
    is_positive = bool(temp_byte & 0x80)
    temperature = integer + decimals * 0.1
    if not is_positive:
        temperature = -temperature

    # Bit 7 of the humidity byte only reflects the device's display unit
    # (Celsius/Fahrenheit); the encoded value is always Celsius.
    humidity = humidity_byte & 0x7F

    result["temperature"] = float(temperature)
    result["humidity"] = float(humidity)
    return result


def _parse_meter_payload(
    payload: bytes,
    manufacturer_payload: Optional[bytes],
) -> dict[str, float | int]:
    result: dict[str, float | int] = {}
    if len(payload) >= 3:
        result["battery"] = int(payload[2] & 0x7F)

    if len(payload) >= 6:
        service_data_bytes = bytes([0, 0, payload[3], payload[4], payload[5], 0])
        result.update(_decode_meter_bytes(service_data_bytes))

    data_bytes = _meter_data_from_manufacturer(manufacturer_payload)
    if data_bytes:
        result.update(_decode_meter_bytes(data_bytes[:6]))
    return result


def _parse_co2_payload(
    payload: bytes,
    manufacturer_payload: Optional[bytes],
) -> dict[str, float | int]:
    result = _parse_meter_payload(payload, manufacturer_payload)
    data_bytes = _meter_data_from_manufacturer(manufacturer_payload)
    if data_bytes and len(data_bytes) >= 9:
        co2_raw = (data_bytes[7] << 8) | data_bytes[8]
        if co2_raw:
            result["co2"] = int(co2_raw)
    return result


def _parse_switchbot_service_payload(
    payload: bytes,
    manufacturer_payload: Optional[bytes],
) -> tuple[Optional[int], Optional[str], Optional[str], dict[str, float | int]]:
    if not payload:
        return None, None, None, {}

    device_code = payload[0] & 0x7F
    model, category = DEVICE_CODE_MAP.get(device_code, (None, None))
    values: dict[str, float | int] = {}

    if category == "meter":
        values = _parse_meter_payload(payload, manufacturer_payload)
    elif category == "co2":
        values = _parse_co2_payload(payload, manufacturer_payload)

    return device_code, model, category, values


def analyze_switchbot_advertisement(adv: Any) -> dict[str, Any]:
    service_data = getattr(adv, "service_data", None) or {}
    manufacturer_data = getattr(adv, "manufacturer_data", None) or {}

    result: dict[str, Any] = {
        "is_switchbot": False,
        "device_code": None,
        "model": None,
        "category": None,
        "fields": {},
    }

    sb_payload: Optional[bytes] = None
    for key, payload in service_data.items():
        key_str = str(key).upper()
        if SWITCHBOT_SERVICE_UUID_FRAGMENT in key_str:
            sb_payload = payload
            break

    if sb_payload is None:
        return result

    manufacturer_payload = manufacturer_data.get(SWITCHBOT_MANUFACTURER_ID)
    device_code, model, category, values = _parse_switchbot_service_payload(sb_payload, manufacturer_payload)

    if device_code is None:
        return result

    result.update(
        {
            "is_switchbot": True,
            "device_code": device_code,
            "model": model,
            "category": category,
            "fields": values,
        }
    )

    return result


def decode_switchbot_advertisement(
    target: BleTarget,
    adv: Any,
) -> Optional[SwitchBotReading]:
    """Decode a single advertisement entry into a reading."""
    analysis = analyze_switchbot_advertisement(adv)
    if not analysis or not analysis["is_switchbot"]:
        return None

    if analysis["category"] != target.device_type:
        return None

    fields = analysis.get("fields") or {}
    return SwitchBotReading(
        device_id=target.mac,
        name=target.alias,
        device_type=target.device_type,
        temperature=fields.get("temperature"),
        humidity=fields.get("humidity"),
        battery=fields.get("battery"),
        co2=fields.get("co2"),
    )
    return None


T = TypeVar("T")


def _run_coroutine(factory: Callable[[], Coroutine[Any, Any, T]]) -> T:
    try:
        return asyncio.run(factory())
    except RuntimeError as exc:
        if "asyncio.run()" in str(exc):
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(factory())
            finally:
                asyncio.set_event_loop(None)
                loop.close()
        raise


async def _discover_with_advertisements(scan_timeout_s: float):
    try:
        from bleak import BleakScanner  # type: ignore
    except ImportError as exc:  # pragma: no cover - import guard
        raise RuntimeError("bleak is required for SwitchBot BLE mode") from exc

    try:
        results = await BleakScanner.discover(timeout=scan_timeout_s, return_adv=True)
        if isinstance(results, dict):
            # Bleak <= 1.1 on macOS may return a dictionary keyed by identifier.
            results = list(results.values())
    except TypeError:
        # Older bleak versions (<0.21) do not support return_adv.
        devices = await BleakScanner.discover(timeout=scan_timeout_s)
        results = []
        for device in devices:
            adv = device.metadata.get("advertisement_data") if device.metadata else None
            if adv is None:
                continue
            results.append((device, adv))
    return list(results)


async def _scan_once_async(
    targets: dict[str, BleTarget],
    hardware_map: dict[bytes, BleTarget],
    scan_timeout_s: float,
) -> list[SwitchBotReading]:
    results = await _discover_with_advertisements(scan_timeout_s)
    readings_map: dict[str, SwitchBotReading] = {}
    for device, adv in results:
        address = getattr(device, "address", "")
        try:
            mac = normalize_address(address)
        except ValueError:
            continue
        target = targets.get(mac)
        manufacturer_data = getattr(adv, "manufacturer_data", None) or {}
        mfg_payload = manufacturer_data.get(SWITCHBOT_MANUFACTURER_ID)
        if not target and mfg_payload and len(mfg_payload) >= 6:
            target = hardware_map.get(mfg_payload[:6])
        if not target:
            continue
        reading = decode_switchbot_advertisement(target, adv)
        if reading:
            readings_map[mac] = reading
    return list(readings_map.values())


def collect_ble_readings(
    targets: list[BleTarget],
    scan_timeout_s: float,
) -> list[SwitchBotReading]:
    """
    Run a single BLE scan and return readings for configured targets.

    The function blocks until the scan completes.
    """
    if not targets:
        raise RuntimeError("At least one BLE device must be configured for BLE mode")
    target_map = {target.mac: target for target in targets}
    hardware_map: dict[bytes, BleTarget] = {}
    for target in targets:
        try:
            mac_bytes = bytes(int(part, 16) for part in target.mac.split(":"))
        except ValueError:
            continue
        hardware_map[mac_bytes] = target

    return _run_coroutine(lambda: _scan_once_async(target_map, hardware_map, scan_timeout_s))


def scan_switchbot_devices(
    scan_timeout_s: float,
) -> list[dict[str, Any]]:
    """
    Discover nearby BLE devices and return SwitchBot candidates with metadata.
    """
    results = _run_coroutine(lambda: _discover_with_advertisements(scan_timeout_s))
    devices: dict[str, dict[str, Any]] = {}
    for device, adv in results:
        address = getattr(device, "address", "")
        try:
            mac = normalize_address(address)
        except ValueError:
            continue
        name = getattr(device, "name", "") or ""
        rssi = getattr(adv, "rssi", None) or getattr(device, "rssi", None)

        analysis = analyze_switchbot_advertisement(adv)
        is_switchbot = analysis["is_switchbot"]
        inferred_type = analysis["category"]
        decoded_fields = analysis.get("fields") or {}
        device_code = analysis.get("device_code")
        model = analysis.get("model")

        detected_reading: Optional[SwitchBotReading] = None
        if decoded_fields:
            temperature = decoded_fields.get("temperature")
            humidity = decoded_fields.get("humidity")
            co2_val = decoded_fields.get("co2")
            battery = decoded_fields.get("battery")
            detected_reading = SwitchBotReading(
                device_id=mac,
                name=name or mac,
                device_type=inferred_type or "unknown",
                temperature=float(temperature) if temperature is not None else None,
                humidity=float(humidity) if humidity is not None else None,
                co2=int(co2_val) if co2_val is not None else None,
                battery=int(battery) if battery is not None else None,
            )

        devices[mac] = {
            "mac": mac,
            "name": name,
            "device_type": inferred_type if inferred_type else ("unknown" if is_switchbot else None),
            "device_model": model,
            "device_code": device_code,
            "rssi": rssi,
            "reading": detected_reading,
            "is_switchbot": is_switchbot,
        }
    return sorted(devices.values(), key=lambda x: x["mac"])
