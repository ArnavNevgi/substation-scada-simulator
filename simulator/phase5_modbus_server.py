r"""Phase 5: continuous educational power-flow telemetry over Modbus TCP.

PowerShell: .\.venv\Scripts\python.exe .\phase5_modbus_server.py
Requires the Phase 3 config files and the existing phase4_powerflow.py.
Breaker command and fault input writes are reserved for Phase 7.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import math
from pathlib import Path
import time

import pymodbus
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice

from modbus_points import ACTIVE_SETPOINTS, decode_word, encode_word, load_points
from phase4_powerflow import (
    BALANCE_TOLERANCE, OperatingPoint, bus_value, load_config, run_powerflow,
)

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("substation")


class TelemetryBridge:
    """Own the model snapshot; network requests never run an electrical solve."""

    def __init__(self, config, points, solver=run_powerflow):
        self.config = config
        self.points = points
        self.solver = solver
        self.hr = [0] * 128
        self.di = [False] * 16
        self.heartbeat = 0
        self.last_good = None
        self.last_error = None
        self.temperature_c = 25.0
        self.thermal_updated_at = time.monotonic()
        self.breaker_closed = bool(config["breaker"]["initial_closed"])
        self.interval_s = float(config["simulation"]["power_flow_interval_s"])
        if not math.isfinite(self.interval_s) or self.interval_s < 0.1:
            raise ValueError("power_flow_interval_s must be finite and at least 0.1.")
        if config["modbus"]["holding_register_allocation_inclusive"] != [0, 127]:
            raise ValueError("Phase 5 requires the planned HR allocation 0..127.")
        if config["modbus"]["discrete_input_allocation_inclusive"] != [0, 15]:
            raise ValueError("Phase 5 requires the planned DI allocation 0..15.")

        initial = {
            "Load_P_Set_kW": config["load"]["initial_p_mw"] * 1000,
            "Load_Q_Set_kVAr": config["load"]["initial_q_mvar"] * 1000,
            "Irradiance_Set_W_m2": config["solar"]["initial_irradiance_w_m2"],
            "PV_P_Limit_Set_kW": config["solar"]["initial_p_limit_kw"],
            "PV_Q_Set_kVAr": config["solar"]["initial_q_set_kvar"],
            "Remote_Mode_Set": 1,
        }
        self.limits = {
            "Load_P_Set_kW": [x * 1000 for x in config["load"]["p_range_mw"]],
            "Load_Q_Set_kVAr": [x * 1000 for x in config["load"]["q_range_mvar"]],
            "Irradiance_Set_W_m2": [0, 1000],
            "PV_P_Limit_Set_kW": [0, config["solar"]["p_ac_max_mw"] * 1000],
            "PV_Q_Set_kVAr": [-config["solar"]["s_rated_mva"] * 1000, config["solar"]["s_rated_mva"] * 1000],
            "Remote_Mode_Set": [0, 1],
        }
        self.writable = {
            points[name]["offset_zero_based"]: name for name in ACTIVE_SETPOINTS
        }
        for name, value in initial.items():
            self.hr[points[name]["offset_zero_based"]] = encode_word(points[name], value)
        self.di[0] = self.breaker_closed
        self.di[3] = True  # Upstream external grid is available in this phase.
        self.di[6] = True

    def input_value(self, signal):
        point = self.points[signal]
        return decode_word(point, self.hr[point["offset_zero_based"]])

    def operating_point(self):
        return OperatingPoint(
            load_p_mw=self.input_value("Load_P_Set_kW") / 1000,
            load_q_mvar=self.input_value("Load_Q_Set_kVAr") / 1000,
            irradiance_w_m2=self.input_value("Irradiance_Set_W_m2"),
            pv_p_limit_kw=self.input_value("PV_P_Limit_Set_kW"),
            pv_q_set_kvar=self.input_value("PV_Q_Set_kVAr"),
            breaker_closed=self.breaker_closed, pv_enabled=True,
        )

    async def access(self, function_code, start_address, address, count,
                     current_registers, set_values):
        """Populate reads and validate writes using the public simulator hook."""
        if function_code == 2 and set_values is None:
            current_registers[0] = sum(int(bit) << i for i, bit in enumerate(self.di))
            return None
        # PyModbus reads back FC6 after accepting a single-register write.
        if function_code in (3, 6) and set_values is None:
            current_registers[:128] = self.hr
            return None
        if function_code not in (6, 16) or set_values is None:
            return ExcCodes.ILLEGAL_FUNCTION

        # Validate the complete request before changing any input.
        # Telemetry, reserved addresses, commands and fault-enable are protected.
        for offset, word in enumerate(set_values, start=address):
            if offset not in self.writable:
                return ExcCodes.ILLEGAL_ADDRESS
            name = self.writable[offset]
            value = decode_word(self.points[name], word)
            low, high = self.limits[name]
            if not low <= value <= high:
                return ExcCodes.ILLEGAL_VALUE
        for offset, word in enumerate(set_values, start=address):
            self.hr[offset] = word
        self.di[6] = bool(self.input_value("Remote_Mode_Set"))
        current_registers[:128] = self.hr
        return None

    def device(self):
        return SimDevice(
            id=self.config["modbus"]["device_id"],
            simdata=(
                [SimData(address=0, values=[False] * 16, datatype=DataType.BITS, readonly=True)],
                [SimData(address=0, values=[False] * 16, datatype=DataType.BITS, readonly=True)],
                [SimData(address=0, values=[0] * 128, datatype=DataType.REGISTERS)],
                [SimData(address=0, values=[0] * 16, datatype=DataType.REGISTERS, readonly=True)],
            ),
            action=self.access,
        )

    async def update(self):
        """Complete one model attempt; publish all analogs only after validation."""
        point = self.operating_point()
        success = False
        try:
            # Keep the asyncio network loop responsive while scipy calculates.
            snapshot = await asyncio.to_thread(self.solver, self.config, point)
            if not snapshot["converged"] or not snapshot["model_valid"]:
                raise ValueError("Electrical solver returned an invalid snapshot.")
            if any(abs(snapshot[key]) > BALANCE_TOLERANCE for key in (
                "active_balance_residual_mw", "reactive_balance_residual_mvar"
            )):
                raise ValueError("Electrical snapshot failed the power-balance check.")
            now = time.monotonic()
            fraction = math.hypot(snapshot["solved_pv_p_mw"], snapshot["solved_pv_q_mvar"]) / self.config["solar"]["s_rated_mva"]
            # Illustrative thermal model: ambient 25 C, rise 35 C at rated S,
            # first-order time constant 60 s. Not manufacturer-calibrated.
            target = 25.0 + 35.0 * fraction ** 2
            temperature = self.temperature_c + (target - self.temperature_c) * (
                1 - math.exp(-(now - self.thermal_updated_at) / 60.0)
            )
            pv = snapshot["pv"]
            values = {
                "LineSide_Voltage_kV": bus_value(snapshot, self.config["breaker"]["bus"], "voltage_kv"),
                "Heartbeat": self.heartbeat,
                "Bus_Voltage_kV": bus_value(snapshot, self.config["load"]["bus"], "voltage_kv"),
                "CB1_Current_A": snapshot["cb1_current_a"],
                "Grid_P_kW": snapshot["grid_p_mw"] * 1000,
                "Grid_Q_kVAr": snapshot["grid_q_mvar"] * 1000,
                "Load_P_kW": snapshot["served_load_p_mw"] * 1000,
                "Load_Q_kVAr": snapshot["served_load_q_mvar"] * 1000,
                "T1_Loading_pct": snapshot["t1_loading_pct"],
                "PV_Available_DC_kW": pv["available_dc_mw"] * 1000,
                "PV_DC_Power_kW": pv["dc_power_mw"] * 1000,
                "PV_DC_Voltage_V": pv["dc_voltage_v"],
                "PV_DC_Current_A": pv["dc_current_a"],
                "PV_AC_Power_kW": snapshot["solved_pv_p_mw"] * 1000,
                "PV_Q_kVAr": snapshot["solved_pv_q_mvar"] * 1000,
                "PV_Efficiency_pct": pv["efficiency_pct"] or 0,
                "PV_Temperature_C": temperature,
                "Grid_Frequency_Hz": self.config["simulation"]["frequency_hz"],
                "Model_State": 1 if snapshot["state"] == "normal" else 2,
                "Command_Status": 0, "Command_Reason": 0,
                "Command_Ack_Sequence": 0, "LastFault_Ikss_kA": 0,
            }
            encoded = [0] * 23
            for name, value in values.items():
                p = self.points[name]
                encoded[p["offset_zero_based"]] = encode_word(p, value)
            # No awaits during publication: each read sees one complete update.
            self.hr[:23] = encoded
            self.di[:9] = [
                self.breaker_closed, False, bool(pv["running"]), True, True,
                snapshot["state"] == "normal",
                bool(self.input_value("Remote_Mode_Set")), False, False,
            ]
            self.temperature_c, self.thermal_updated_at = temperature, now
            self.last_good = snapshot
            self.last_error = None
            success = True
        except Exception as error:
            # Preserve last valid analogs. Transport can remain healthy while
            # electrical validity is false; never turn a failed solve into zeros.
            self.hr[18] = 4
            self.di[4] = False
            self.last_error = str(error)
            LOG.error("Model update failed; holding last valid measurements: %s", error)
        finally:
            self.heartbeat = (self.heartbeat + 1) % 65536
            self.hr[1] = self.heartbeat
        return success

    def print_summary(self):
        def value(name):
            p = self.points[name]
            return decode_word(p, self.hr[p["offset_zero_based"]])
        print(
            f"HB={self.heartbeat:5d} | Valid={self.di[4]} | "
            f"Bus={value('Bus_Voltage_kV'):.2f} kV | "
            f"PV={value('PV_AC_Power_kW'):.0f} kW | "
            f"Grid={value('Grid_P_kW'):.0f} kW | "
            f"T1={value('T1_Loading_pct'):.1f} %",
            flush=True,
        )


async def serve(port=None):
    config = load_config(ROOT / "config" / "substation.json")
    points = load_points()
    bridge = TelemetryBridge(config, points)
    settings = config["modbus"]
    port = port if port is not None else settings["port"]
    if not 1 <= port <= 65535:
        raise ValueError("TCP port must be within 1..65535.")
    server = ModbusTcpServer(context=bridge.device(), address=(settings["host"], port))
    started = False
    try:
        await bridge.update()
        await server.serve_forever(background=True)
        started = True
        print(
            f"Phase 5 Modbus TCP: {settings['host']}:{port} | "
            f"Unit ID: {settings['device_id']} | PyModbus {pymodbus.__version__}",
            flush=True,
        )
        print("HR 0..127 and DI 0..15; zero-based addressing. Ctrl+C stops.", flush=True)
        print("Load/solar setpoints enabled; command/fault writes reserved for Phase 7.", flush=True)
        bridge.print_summary()
        deadline = time.monotonic() + bridge.interval_s
        while True:
            await asyncio.sleep(max(0.0, deadline - time.monotonic()))
            start = time.monotonic()
            await bridge.update()
            bridge.print_summary()
            elapsed = time.monotonic() - start
            if elapsed > bridge.interval_s:
                LOG.warning("Model solve took %.2f s; requested cadence is %.2f s.", elapsed, bridge.interval_s)
            # Account for solve duration. Overruns do not cause overlapping work.
            deadline = start + bridge.interval_s
    finally:
        if started:
            await server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, help="Override the configured port for isolated local testing.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(serve(args.port))
        return 0
    except KeyboardInterrupt:
        print("\nPhase 5 server stopped.")
        return 0
    except (OSError, ValueError, KeyError, TypeError, RuntimeError) as error:
        print(f"ERROR: {error}")
        print("Check Phase 3 files and stop the Phase 2 server before using port 1502.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
