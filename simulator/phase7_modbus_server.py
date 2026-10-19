r"""Phase 7 server: power flow, remote controls and educational fault protection.

PowerShell: .\.venv\Scripts\python.exe .\phase7_modbus_server.py
Reuses the Phase 3/4/5 files; stop the Phase 5 server before using port 1502.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import replace
import logging
import math
from pathlib import Path
import time

import pymodbus
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer

from modbus_points import decode_word, encode_word, load_points
from phase4_powerflow import BALANCE_TOLERANCE, bus_value, load_config, run_powerflow
from phase5_modbus_server import TelemetryBridge
from phase7_control import ControlEngine, STATUSES, REASONS
from phase7_fault import calculate_fault_study

ROOT = Path(__file__).resolve().parent
LOG = logging.getLogger("substation.phase7")


class ControlBridge(TelemetryBridge):
    def __init__(self, config, points, fault_study, solver=run_powerflow):
        super().__init__(config, points, solver)
        self.engine = ControlEngine(config, fault_study)
        self.limits["Fault_Enable"] = [0, 1]
        self.writable[115] = "Fault_Enable"
        self.model_requested = asyncio.Event()
        self._observed_revision = self.engine.revision
        self.control_tick_s = float(config["simulation"]["control_tick_s"])
        if not math.isfinite(self.control_tick_s) or not 0.01 <= self.control_tick_s <= 1.0:
            raise ValueError("control_tick_s must be finite and within 0.01..1.0.")
        encode_word(points["LastFault_Ikss_kA"], fault_study["bus_ikss_ka"])
        self.publish_feedback()

    def operating_point(self):
        point = super().operating_point()
        return replace(point, breaker_closed=self.engine.breaker_closed,
                       pv_enabled=self.engine.pv_enabled and not self.engine.pv_tripped)

    def publish_feedback(self):
        e = self.engine
        self.breaker_closed = e.breaker_closed
        if self.last_error and not e.model_valid:
            state = 4
        elif e.fault_active:
            state = 3
        elif e.model_valid:
            state = 1 if e.bus_energized else 2
        else:
            state = 4 if self.last_error else 0
        self.hr[18:23] = [state, e.command_status, e.command_reason, e.ack_sequence,
                          encode_word(self.points["LastFault_Ikss_kA"], e.last_fault_ikss_ka)]
        self.di[:9] = [
            e.breaker_closed, e.trip_latched, e.pv_running, e.grid_available,
            e.model_valid, e.bus_energized, e.remote_mode, e.fault_active, e.pv_tripped,
        ]
        if e.revision != self._observed_revision:
            self._observed_revision = e.revision
            self.model_requested.set()

    def tick(self, now=None):
        previous_trip = self.engine.trip_latched
        self.engine.tick(time.monotonic() if now is None else now)
        if self.engine.trip_latched and not previous_trip:
            LOG.warning("Overcurrent operated: trip latched; CB1 opening requested.")
        self.publish_feedback()

    async def access(self, function_code, start_address, address, count,
                     current_registers, set_values):
        if function_code == 2 and set_values is None:
            current_registers[0] = sum(int(bit) << i for i, bit in enumerate(self.di))
            return None
        if function_code in (3, 6) and set_values is None:
            current_registers[:128] = self.hr
            return None
        if function_code not in (6, 16) or set_values is None:
            return ExcCodes.ILLEGAL_FUNCTION

        changes = dict(enumerate(set_values, start=address))
        for offset, word in changes.items():
            if offset in (100, 101):
                continue
            if offset not in self.writable:
                return ExcCodes.ILLEGAL_ADDRESS
            name = self.writable[offset]
            low, high = self.limits[name]
            if not low <= decode_word(self.points[name], word) <= high:
                return ExcCodes.ILLEGAL_VALUE
        # No partial writes. Retry of the same payload/sequence is harmless;
        # a different command cannot overwrite a pending operation.
        if self.engine.command_status == 1 and any(x in changes for x in (100, 101)):
            if any(changes[x] != self.hr[x] for x in changes if x in (100, 101)):
                return ExcCodes.ILLEGAL_VALUE

        electrical_inputs_changed = any(
            offset in changes and changes[offset] != self.hr[offset]
            for offset in (110, 111, 112, 113, 114)
        )
        for offset, word in changes.items():
            self.hr[offset] = word
        if electrical_inputs_changed:
            self.engine.changed()
        if 115 in changes or 116 in changes:
            self.engine.environment(fault=bool(self.hr[115]), remote=bool(self.hr[116]))
        if 101 in changes:
            self.engine.submit(self.hr[100], self.hr[101], time.monotonic())
        self.tick()
        current_registers[:128] = self.hr
        return None

    async def update(self):
        """Publish only a snapshot calculated for the current control revision."""
        revision = self.engine.revision
        point = self.operating_point()
        try:
            if self.engine.fault_fed:
                # A normal AC power flow is not a bolted-fault solution.
                # Hold its analogs, mark them invalid, and use the separate
                # fault study only for relay input and LastFault_Ikss_kA.
                self.engine.invalidate()
                self.last_error = None
                return False
            snapshot = await asyncio.to_thread(self.solver, self.config, point)
            if revision != self.engine.revision or self.engine.fault_fed:
                LOG.info("Discarded obsolete electrical snapshot after a control/input change.")
                self.model_requested.set()
                return False
            if not snapshot["converged"] or not snapshot["model_valid"]:
                raise ValueError("Electrical solver returned an invalid snapshot.")
            if any(abs(snapshot[key]) > BALANCE_TOLERANCE for key in (
                "active_balance_residual_mw", "reactive_balance_residual_mvar"
            )):
                raise ValueError("Electrical snapshot failed the power-balance check.")
            now = time.monotonic()
            fraction = math.hypot(snapshot["solved_pv_p_mw"], snapshot["solved_pv_q_mvar"]) / self.config["solar"]["s_rated_mva"]
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
            }
            encoded = [0] * 18
            for name, value in values.items():
                p = self.points[name]
                encoded[p["offset_zero_based"]] = encode_word(p, value)
            # No awaits during validation/publication; control and Modbus tasks
            # cannot mix states from different revisions into this publication.
            if not self.engine.accept_snapshot(snapshot, revision):
                return False
            self.hr[:18] = encoded
            self.temperature_c, self.thermal_updated_at = temperature, now
            self.last_good = snapshot
            self.last_error = None
            return True
        except Exception as error:
            self.engine.invalidate()
            self.last_error = str(error)
            LOG.error("Model update failed; holding last valid measurements: %s", error)
            return False
        finally:
            self.heartbeat = (self.heartbeat + 1) % 65536
            self.hr[1] = self.heartbeat
            self.publish_feedback()

    def print_summary(self):
        e = self.engine
        print(
            f"HB={self.heartbeat:5d} | Valid={e.model_valid} | "
            f"CB1={'CLOSED' if e.breaker_closed else 'OPEN'} | "
            f"PV={'RUNNING' if e.pv_running else 'IDLE'} | "
            f"Fault={e.fault_active} | Trip={e.trip_latched} | "
            f"Cmd#{e.ack_sequence} {STATUSES[e.command_status]} / {REASONS[e.command_reason]}",
            flush=True,
        )


async def control_loop(bridge):
    while True:
        bridge.tick()
        await asyncio.sleep(bridge.control_tick_s)


async def model_loop(bridge, verbose=True):
    while True:
        try:
            await asyncio.wait_for(bridge.model_requested.wait(), timeout=bridge.interval_s)
        except TimeoutError:
            pass
        bridge.model_requested.clear()
        await bridge.update()
        if verbose:
            bridge.print_summary()


async def serve(port=None):
    config = load_config(ROOT / "config/substation.json")
    points = load_points(ROOT / "config/points.json")
    settings = config["modbus"]
    port = settings["port"] if port is None else port
    if not 1 <= port <= 65535:
        raise ValueError("TCP port must be within 1..65535.")
    print("Calculating grid-only IEC 60909 reference fault...", flush=True)
    study = await asyncio.to_thread(calculate_fault_study, config)
    print(f"BUS_13_8 Ikss: {study['bus_ikss_ka']:.6f} kA | "
          f"CB1 HV: {study['cb1_hv_current_a']:.3f} A | Independent check: PASS", flush=True)
    bridge = ControlBridge(config, points, study)
    await bridge.update()
    server = ModbusTcpServer(context=bridge.device(), address=(settings["host"], port))
    tasks = []
    try:
        await server.serve_forever(background=True)
        print(f"Phase 7 Modbus TCP: {settings['host']}:{port} | Unit {settings['device_id']} | "
              f"PyModbus {pymodbus.__version__}", flush=True)
        print("Commands HR100/101; fault input HR115; existing addresses unchanged. Ctrl+C stops.", flush=True)
        print("Educational simulation; assumed parameters and delays, no real-time relay or converter dynamics.", flush=True)
        bridge.print_summary()
        tasks = [asyncio.create_task(control_loop(bridge)), asyncio.create_task(model_loop(bridge))]
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await server.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, help="Override port for isolated testing.")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(serve(args.port))
        return 0
    except KeyboardInterrupt:
        print("\nPhase 7 server stopped.")
        return 0
    except Exception as error:
        print(f"ERROR: {error}")
        print("Check the Phase 3/4/5 files and stop the Phase 5 server before using port 1502.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

