"""Shared word encoding and point-map loading for the educational device."""

import json
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ACTIVE_SETPOINTS = (
    "Load_P_Set_kW", "Load_Q_Set_kVAr", "Irradiance_Set_W_m2",
    "PV_P_Limit_Set_kW", "PV_Q_Set_kVAr", "Remote_Mode_Set",
)


def load_points(path=None):
    path = Path(path) if path else ROOT / "config" / "points.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    if document.get("schema_version") != 1:
        raise ValueError("Expected Phase 3 point-map schema_version 1.")
    points = {}
    addresses = set()
    for point in document["points"]:
        name = point["signal"]
        address = (point["area"], point["offset_zero_based"])
        if name in points or address in addresses:
            raise ValueError("Duplicate signal or address in point map.")
        area, offset = address
        limit = {"HR": 128, "DI": 16}.get(area, 0)
        if not isinstance(offset, int) or not 0 <= offset < limit:
            raise ValueError(f"Address outside allocated {area} block: {name}")
        if not math.isfinite(point["scale"]) or point["scale"] <= 0:
            raise ValueError(f"Invalid scale: {name}")
        if area == "HR" and point["raw_type"] not in ("uint16", "int16"):
            raise ValueError(f"Expected a 16-bit word: {name}")
        if area == "DI" and point["raw_type"] != "bool":
            raise ValueError(f"Expected a Boolean status: {name}")
        points[name] = point
        addresses.add(address)
    if len(points) != document["point_count"]:
        raise ValueError("point_count does not match the point list.")
    return points


def encode_word(point, engineering_value):
    """Convert engineering units to one word; reject overflow, never wrap it."""
    value = float(engineering_value)
    if not math.isfinite(value):
        raise ValueError(f"Non-finite value: {point['signal']}")
    raw = round(value / point["scale"])
    low, high = (-32768, 32767) if point["raw_type"] == "int16" else (0, 65535)
    if not low <= raw <= high:
        raise ValueError(f"16-bit overflow: {point['signal']} = {engineering_value}")
    return raw & 0xFFFF


def decode_word(point, word):
    if not isinstance(word, int) or not 0 <= word <= 65535:
        raise ValueError("Expected an unsigned wire word in 0..65535.")
    raw = word - 65536 if point["raw_type"] == "int16" and word >= 32768 else word
    return raw * point["scale"]
