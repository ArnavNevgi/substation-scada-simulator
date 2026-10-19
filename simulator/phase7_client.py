r"""Probe Phase 7 or issue one simulation operation.

PowerShell:
    .\.venv\Scripts\python.exe .\phase7_client.py
    .\.venv\Scripts\python.exe .\phase7_client.py --command open
    .\.venv\Scripts\python.exe .\phase7_client.py --fault on
"""

import argparse
import json
from pathlib import Path
import time

from pymodbus.client import ModbusTcpClient

from modbus_points import ACTIVE_SETPOINTS, decode_word, encode_word, load_points
from phase5_client import read_snapshot, require_ok
from phase7_control import COMMANDS, COMMAND_NAMES, STATUSES, REASONS

ROOT = Path(__file__).resolve().parent


def command(client, unit, name, timeout):
    status = require_ok(client.read_holding_registers(19, count=3, device_id=unit)).registers
    if status[0] == 1:
        raise ValueError("Another command is pending; wait for its result.")
    previous = require_ok(client.read_holding_registers(101, count=1, device_id=unit)).registers[0]
    sequence = (previous + 1) % 65536
    # FC16 commits payload and sequence together, in one atomic Modbus request.
    require_ok(client.write_registers(100, [COMMANDS[name], sequence], device_id=unit))
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status, reason, ack = require_ok(
            client.read_holding_registers(19, count=3, device_id=unit)
        ).registers
        if ack == sequence and status in (2, 3, 4):
            print(f"Command #{sequence}: {COMMAND_NAMES[COMMANDS[name]]} -> "
                  f"{STATUSES[status]} | Reason: {REASONS.get(reason, str(reason))}")
            return status == 2
        if ack not in (previous, sequence):
            raise ValueError("Another writer changed command feedback. Use one command writer at a time.")
        time.sleep(0.1)
    raise ValueError("Command result timed out; read status and feedback before retrying.")


def wait_state(client, unit, timeout, fault_on=False, observe_heartbeat=None):
    deadline = time.monotonic() + timeout
    latest = None
    while time.monotonic() < deadline:
        latest = read_snapshot(client, unit)
        words, bits, _ = latest
        normal_ready = bits[4] and words[1] != observe_heartbeat
        tripped_ready = not bits[0] and bits[1] and bits[4]
        ready = tripped_ready if fault_on else normal_ready
        if ready:
            return latest
        time.sleep(0.1)
    raise ValueError("Model/state did not settle in time; inspect the server log and read status.")


def print_snapshot(snapshot, points, first_heartbeat):
    words, bits, inputs = snapshot
    print(f"Heartbeat: {first_heartbeat} -> {words[1]} | Model state: {words[18]}")
    print(f"Model_Valid: {bits[4]} | CB1_Closed: {bits[0]} | Bus_Energized: {bits[5]}")
    print(f"PV_Running: {bits[2]} | Trip_Latched: {bits[1]} | PV_Tripped: {bits[8]}")
    print(f"Fault_Active: {bits[7]} | Remote_Mode: {bits[6]}")
    print(f"Command ack: {words[21]} | {STATUSES.get(words[19], words[19])} | "
          f"Reason: {REASONS.get(words[20], words[20])}")
    for name in ("LineSide_Voltage_kV", "Bus_Voltage_kV", "CB1_Current_A", "Grid_P_kW",
                 "Grid_Q_kVAr", "T1_Loading_pct", "PV_AC_Power_kW", "PV_Q_kVAr", "LastFault_Ikss_kA"):
        p = points[name]
        value = decode_word(p, words[p["offset_zero_based"]])
        print(f"{name:<25} {value:>10.3f} {p['engineering_unit']}")
    print("LastFault_Ikss_kA is retained event data, not present current after opening.")
    print("Simulation inputs:")
    for name in (*ACTIVE_SETPOINTS[:-1], "Fault_Enable", "Remote_Mode_Set"):
        p = points[name]
        print(f"  {name:<24} {decode_word(p, inputs[p['offset_zero_based'] - 110]):>8.0f}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--command", choices=COMMANDS)
    action.add_argument("--fault", choices=("on", "off"))
    action.add_argument("--set", nargs=2, metavar=("SIGNAL", "VALUE"), dest="setpoint")
    parser.add_argument("--port", type=int)
    parser.add_argument("--timeout", type=float, default=10.0)
    args = parser.parse_args()
    try:
        if not 0 < args.timeout <= 120:
            raise ValueError("timeout must be within 0..120 seconds.")
        settings = json.loads((ROOT / "config/substation.json").read_text(encoding="utf-8"))["modbus"]
        points = load_points()
        unit = settings["device_id"]
        with ModbusTcpClient(settings["host"], port=args.port or settings["port"], timeout=3, retries=0) as client:
            if not client.connected:
                raise ValueError("Cannot connect. Start phase7_modbus_server.py first.")
            first = require_ok(client.read_holding_registers(1, count=1, device_id=unit)).registers[0]
            applied = True
            if args.command:
                applied = command(client, unit, args.command, args.timeout)
            elif args.fault:
                require_ok(client.write_register(115, int(args.fault == "on"), device_id=unit))
                print("Simulation fault " + ("injected." if args.fault == "on" else "removed; latches remain."))
            elif args.setpoint:
                name, text = args.setpoint
                if name not in ACTIVE_SETPOINTS:
                    raise ValueError("Supported --set inputs: " + ", ".join(ACTIVE_SETPOINTS))
                value = float(text)
                if not value.is_integer():
                    raise ValueError("Setpoints require whole engineering units.")
                p = points[name]
                require_ok(client.write_register(p["offset_zero_based"], encode_word(p, value), device_id=unit))
                print(f"Accepted simulation input: {name} = {value:g}")
            # A rejected command can be inspected immediately, including an
            # intentionally invalid energized-fault state.
            if not applied:
                snapshot = read_snapshot(client, unit)
            else:
                initial_bits = require_ok(client.read_discrete_inputs(0, count=16, device_id=unit)).bits
                fault_on = args.fault == "on" and initial_bits[0]
                snapshot = wait_state(client, unit, args.timeout, fault_on, first)
            print_snapshot(snapshot, points, first)
            print("Phase 7 communication/status check: " + ("PASS" if snapshot[1][4] else "MODEL NOT VALID"))
            return 0 if applied and snapshot[1][4] else 1
    except Exception as error:
        print(f"ERROR: {error}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
