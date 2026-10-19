r"""Read the Phase 5 simulator or write one supported simulation input.

PowerShell:
    .\.venv\Scripts\python.exe .\phase5_client.py
    .\.venv\Scripts\python.exe .\phase5_client.py --set Irradiance_Set_W_m2 300
"""

import argparse
import json
from pathlib import Path
import time

from pymodbus.client import ModbusTcpClient

from modbus_points import ACTIVE_SETPOINTS, decode_word, encode_word, load_points

ROOT = Path(__file__).resolve().parent


def require_ok(response):
    if response.isError():
        raise ValueError(f"Modbus exception: {response}")
    return response


def read_snapshot(client, unit):
    # Retry if a model publication occurred between the HR and DI requests.
    for _ in range(4):
        words = require_ok(client.read_holding_registers(0, count=23, device_id=unit)).registers
        bits = require_ok(client.read_discrete_inputs(0, count=16, device_id=unit)).bits[:16]
        inputs = require_ok(client.read_holding_registers(110, count=7, device_id=unit)).registers
        heartbeat = require_ok(client.read_holding_registers(1, count=1, device_id=unit)).registers[0]
        if heartbeat == words[1]:
            return words, bits, inputs
    raise ValueError("Model changed during each read; try again.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--set", nargs=2, metavar=("SIGNAL", "VALUE"), dest="setpoint")
    parser.add_argument("--port", type=int, help="Override the configured port for isolated local testing.")
    args = parser.parse_args()
    try:
        config = json.loads((ROOT / "config/substation.json").read_text(encoding="utf-8"))
        points = load_points()
        settings = config["modbus"]
        unit = settings["device_id"]
        with ModbusTcpClient(
            settings["host"], port=args.port or settings["port"],
            timeout=3, retries=0,
        ) as client:
            if not client.connected:
                raise ValueError("Cannot connect. Start phase5_modbus_server.py first.")
            first = require_ok(client.read_holding_registers(1, count=1, device_id=unit)).registers[0]
            if args.setpoint:
                name, text = args.setpoint
                if name not in ACTIVE_SETPOINTS:
                    raise ValueError("Supported Phase 5 inputs: " + ", ".join(ACTIVE_SETPOINTS))
                value = float(text)
                if not value.is_integer():
                    raise ValueError("These setpoints require whole engineering units.")
                p = points[name]
                require_ok(client.write_register(p["offset_zero_based"], encode_word(p, value), device_id=unit))
                print(f"Accepted simulation input: {name} = {value:g}")
            time.sleep(max(2.2, 2 * config["simulation"]["power_flow_interval_s"] + 0.2))
            words, bits, inputs = read_snapshot(client, unit)
            print("Modbus communication: PASS")
            print(f"Heartbeat: {first} -> {words[1]}")
            print(f"Model_Valid: {bits[4]} | Bus_Energized: {bits[5]} | CB1_Closed: {bits[0]}")
            if first == words[1]:
                print("NOTE: No model heartbeat change observed during this check.")
            measurements = (
                "LineSide_Voltage_kV", "Bus_Voltage_kV", "CB1_Current_A",
                "Grid_P_kW", "Grid_Q_kVAr", "T1_Loading_pct",
                "PV_AC_Power_kW", "PV_Q_kVAr", "PV_Temperature_C",
            )
            for name in measurements:
                p = points[name]
                value = decode_word(p, words[p["offset_zero_based"]])
                print(f"{name:<24} {value:>10.3f} {p['engineering_unit']}")
            print("\nCurrent simulation inputs:")
            for name in ACTIVE_SETPOINTS:
                p = points[name]
                value = decode_word(p, inputs[p["offset_zero_based"] - 110])
                print(f"{name:<24} {value:>10.0f}")
            print("Phase 5 model status: " + ("VALID" if bits[4] else "INVALID - inspect server log"))
            return 0 if bits[4] else 1
    except Exception as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
