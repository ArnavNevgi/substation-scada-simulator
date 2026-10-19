r"""Phase 4: checked balanced AC power flow for the educational substation.

Run from PowerShell:
    .\.venv\Scripts\python.exe .\phase4_powerflow.py
    .\.venv\Scripts\python.exe .\phase4_powerflow.py --verify

Reads the existing config/substation.json relative to this script.
This program does not start, stop, or write to a Modbus server.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys

import pandapower as pp
from pandapower.auxiliary import LoadflowNotConverged
from pandapower.topology import unsupplied_buses


ROOT = Path(__file__).resolve().parent
SCENARIOS = (
    "normal", "cloudy", "high-load", "breaker-open", "q-support",
    "q-absorb", "q-limit", "curtailed", "export", "night", "pv-stopped",
)
BALANCE_TOLERANCE = 1e-6  # MW / MVAr: one watt / one var.


@dataclass(frozen=True)
class OperatingPoint:
    load_p_mw: float
    load_q_mvar: float
    irradiance_w_m2: float
    pv_p_limit_kw: float
    pv_q_set_kvar: float
    breaker_closed: bool = True
    pv_enabled: bool = True


def load_config(path: Path) -> dict:
    c = json.loads(path.read_text(encoding="utf-8"))
    if c.get("schema_version") != 1:
        raise ValueError("Expected Phase 3 configuration schema_version 1.")
    names = [b["name"] for b in c["buses"]]
    if len(names) != len(set(names)):
        raise ValueError("Bus names must be unique.")
    for bus in c["buses"]:
        if not math.isfinite(bus["vn_kv"]) or bus["vn_kv"] <= 0:
            raise ValueError("Every nominal bus voltage must be finite and positive.")
    for key in ("main_transformer", "solar_transformer"):
        t = c[key]
        if not (0 <= t["vkr_percent"] <= t["vk_percent"] and t["vk_percent"] > 0):
            raise ValueError(f"{key}: require 0 <= vkr_percent <= vk_percent, vk > 0.")
    solar = c["solar"]
    if not 0 < solar["efficiency_fraction"] <= 1:
        raise ValueError("Solar efficiency_fraction must be between zero and one.")
    if not 0 < solar["p_ac_max_mw"] <= solar["s_rated_mva"]:
        raise ValueError("Solar ratings must satisfy 0 < P_max <= S_rated.")
    if c["simulation"]["frequency_hz"] != c["line"]["parameter_frequency_hz"]:
        raise ValueError("Line parameter frequency must match simulation frequency.")
    return c


def operating_point(c: dict, scenario: str) -> OperatingPoint:
    point = OperatingPoint(
        load_p_mw=c["load"]["initial_p_mw"],
        load_q_mvar=c["load"]["initial_q_mvar"],
        irradiance_w_m2=c["solar"]["initial_irradiance_w_m2"],
        pv_p_limit_kw=c["solar"]["initial_p_limit_kw"],
        pv_q_set_kvar=c["solar"]["initial_q_set_kvar"],
        breaker_closed=c["breaker"]["initial_closed"],
    )
    changes = {
        "normal": {},
        "cloudy": {"irradiance_w_m2": 300.0},
        "high-load": {"load_p_mw": 25.0},
        "breaker-open": {"breaker_closed": False},
        "q-support": {"pv_q_set_kvar": 1000.0},
        "q-absorb": {"pv_q_set_kvar": -1000.0},
        "q-limit": {"pv_q_set_kvar": 2500.0},
        "curtailed": {"pv_p_limit_kw": 1000.0},
        "export": {"load_p_mw": 0.5, "load_q_mvar": 0.0},
        "night": {"irradiance_w_m2": 0.0, "pv_q_set_kvar": 1000.0},
        "pv-stopped": {"pv_enabled": False},
    }
    return replace(point, **changes[scenario])


def validate_operating_point(c: dict, point: OperatingPoint) -> None:
    ranges = {
        "load_p_mw": c["load"]["p_range_mw"],
        "load_q_mvar": c["load"]["q_range_mvar"],
        "irradiance_w_m2": [0, 1000],
        "pv_p_limit_kw": [0, c["solar"]["p_ac_max_mw"] * 1000],
        "pv_q_set_kvar": [
            -c["solar"]["s_rated_mva"] * 1000,
            c["solar"]["s_rated_mva"] * 1000,
        ],
    }
    for field, (low, high) in ranges.items():
        value = getattr(point, field)
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"{field} must be finite and within [{low}, {high}].")
    if type(point.breaker_closed) is not bool or type(point.pv_enabled) is not bool:
        raise ValueError("Breaker and inverter enable inputs must be Boolean.")


def build_network(c: dict, point: OperatingPoint):
    net = pp.create_empty_network(
        name=c["project"], f_hz=c["simulation"]["frequency_hz"], sn_mva=100.0
    )
    buses = {
        b["name"]: pp.create_bus(net, vn_kv=b["vn_kv"], name=b["name"])
        for b in c["buses"]
    }
    grid = c["external_grid"]
    grid_id = pp.create_ext_grid(
        net, bus=buses[grid["bus"]], vm_pu=grid["vm_pu"],
        va_degree=grid["va_degree"], name="External grid",
        s_sc_max_mva=grid["s_sc_max_mva"], s_sc_min_mva=grid["s_sc_min_mva"],
        rx_max=grid["rx_max"], rx_min=grid["rx_min"],
    )
    line = c["line"]
    line_id = pp.create_line_from_parameters(
        net, from_bus=buses[line["from_bus"]], to_bus=buses[line["to_bus"]],
        length_km=line["length_km"], r_ohm_per_km=line["r_ohm_per_km"],
        x_ohm_per_km=line["x_ohm_per_km"], c_nf_per_km=line["c_nf_per_km"],
        max_i_ka=line["max_i_ka"], name=line["name"],
    )
    breaker = c["breaker"]
    breaker_id = pp.create_switch(
        net, bus=buses[breaker["bus"]], element=buses[breaker["element_bus"]],
        et="b", closed=point.breaker_closed, type="CB", name=breaker["name"],
    )
    trafos = {}
    fields = (
        "sn_mva", "vn_hv_kv", "vn_lv_kv", "vk_percent", "vkr_percent",
        "pfe_kw", "i0_percent", "shift_degree",
    )
    for key in ("main_transformer", "solar_transformer"):
        t = c[key]
        trafos[key] = pp.create_transformer_from_parameters(
            net, hv_bus=buses[t["hv_bus"]], lv_bus=buses[t["lv_bus"]],
            name=t["name"], **{field: t[field] for field in fields},
        )
    load_id = pp.create_load(
        net, bus=buses[c["load"]["bus"]], p_mw=point.load_p_mw,
        q_mvar=point.load_q_mvar, name=c["load"]["name"],
    )
    pv_id = pp.create_sgen(
        net, bus=buses[c["solar"]["bus"]], p_mw=0.0, q_mvar=0.0,
        sn_mva=c["solar"]["s_rated_mva"], name=c["solar"]["name"], type="PV",
    )
    return net, {
        "buses": buses, "grid": grid_id, "line": line_id,
        "breaker": breaker_id, "trafos": trafos, "load": load_id, "pv": pv_id,
    }


def inverter_dispatch(c: dict, point: OperatingPoint, supplied: bool) -> dict:
    solar = c["solar"]
    available_dc_mw = (
        solar["dc_available_at_1000_w_m2_mw"] * point.irradiance_w_m2 / 1000.0
    )
    # DC voltage may remain present while AC output is disconnected.
    dc_voltage_v = solar["dc_voltage_when_sun_available_v"] if available_dc_mw > 0 else 0.0
    p_ac_mw = q_mvar = 0.0
    if supplied and point.pv_enabled and available_dc_mw > 0:
        p_ac_mw = min(
            available_dc_mw * solar["efficiency_fraction"],
            solar["p_ac_max_mw"], point.pv_p_limit_kw / 1000.0,
        )
        # sn_mva alone does not enforce this limit in ordinary power flow.
        q_limit_mvar = math.sqrt(max(0.0, solar["s_rated_mva"] ** 2 - p_ac_mw ** 2))
        q_mvar = max(-q_limit_mvar, min(point.pv_q_set_kvar / 1000.0, q_limit_mvar))
    dc_power_mw = p_ac_mw / solar["efficiency_fraction"]
    return {
        "available_dc_mw": available_dc_mw,
        "dc_power_mw": dc_power_mw,
        "dc_voltage_v": dc_voltage_v,
        "dc_current_a": dc_power_mw * 1e6 / dc_voltage_v if dc_voltage_v else 0.0,
        "ac_p_mw": p_ac_mw,
        "ac_q_mvar": q_mvar,
        "efficiency_pct": 100.0 * solar["efficiency_fraction"] if dc_power_mw > 0 else None,
        "running": p_ac_mw > 0 or abs(q_mvar) > 0,
    }


def finite_result(table, index: int, column: str) -> float:
    value = float(table.at[index, column])
    if not math.isfinite(value):
        raise ValueError(f"Non-finite result at {column}, element {index}.")
    return value


def run_powerflow(c: dict, point: OperatingPoint) -> dict:
    validate_operating_point(c, point)
    net, ids = build_network(c, point)
    buses = ids["buses"]
    # A grid-following static generator is not a voltage/frequency reference.
    # Reachability from the external grid determines the supplied buses.
    unsupplied = set(unsupplied_buses(net, respect_switches=True))
    pv_supplied = buses[c["solar"]["bus"]] not in unsupplied
    load_supplied = buses[c["load"]["bus"]] not in unsupplied
    pv = inverter_dispatch(c, point, pv_supplied)
    net.sgen.at[ids["pv"], "p_mw"] = pv["ac_p_mw"]
    net.sgen.at[ids["pv"], "q_mvar"] = pv["ac_q_mvar"]
    net.sgen.at[ids["pv"], "in_service"] = bool(pv["running"])
    net.load.at[ids["load"], "in_service"] = load_supplied
    pp.runpp(
        net, algorithm="nr", init="flat", calculate_voltage_angles=True,
        check_connectivity=True, numba=False, lightsim2grid=False,
        trafo_model="t", trafo_loading="current",
        max_iteration=30, tolerance_mva=1e-8,
    )
    if not net.converged:
        raise ValueError("The power flow did not converge.")

    bus_results = []
    for specification in c["buses"]:
        name = specification["name"]
        index = buses[name]
        supplied = index not in unsupplied
        # Zero is assigned only to a bus independently known to be unsupplied.
        # An unexpected NaN on a supplied bus is an error, never a false zero.
        vm_pu = finite_result(net.res_bus, index, "vm_pu") if supplied else 0.0
        bus_results.append({
            "name": name, "nominal_kv": specification["vn_kv"],
            "energized": supplied, "voltage_pu": vm_pu,
            "voltage_kv": vm_pu * specification["vn_kv"],
            "voltage_basis": "power_flow" if supplied else "known_unsupplied_state",
        })

    trafo_results = {}
    for key, index in ids["trafos"].items():
        supplied = all(buses[c[key][side]] not in unsupplied for side in ("hv_bus", "lv_bus"))
        fields = ("pl_mw", "ql_mvar", "i_hv_ka", "loading_percent")
        trafo_results[key] = {
            field: finite_result(net.res_trafo, index, field) if supplied else 0.0
            for field in fields
        }
    line_p_loss = finite_result(net.res_line, ids["line"], "pl_mw")
    line_q_net = finite_result(net.res_line, ids["line"], "ql_mvar")
    p_loss = line_p_loss + sum(t["pl_mw"] for t in trafo_results.values())
    # Reactive balance includes signed line charging, which can be negative.
    q_net = line_q_net + sum(t["ql_mvar"] for t in trafo_results.values())
    grid_p = finite_result(net.res_ext_grid, ids["grid"], "p_mw")
    grid_q = finite_result(net.res_ext_grid, ids["grid"], "q_mvar")
    served_p = finite_result(net.res_load, ids["load"], "p_mw") if load_supplied else 0.0
    served_q = finite_result(net.res_load, ids["load"], "q_mvar") if load_supplied else 0.0
    solved_pv_p = finite_result(net.res_sgen, ids["pv"], "p_mw")
    solved_pv_q = finite_result(net.res_sgen, ids["pv"], "q_mvar")
    t1 = trafo_results["main_transformer"]
    return {
        "inputs": asdict(point),
        "converged": bool(net.converged),
        "model_valid": True,
        "state": "normal" if load_supplied else "downstream_deenergized",
        "buses": bus_results,
        "grid_p_mw": grid_p, "grid_q_mvar": grid_q,
        "served_load_p_mw": served_p, "served_load_q_mvar": served_q,
        "pv": pv, "solved_pv_p_mw": solved_pv_p, "solved_pv_q_mvar": solved_pv_q,
        "cb1_current_a": t1["i_hv_ka"] * 1000.0,
        "t1_loading_pct": t1["loading_percent"],
        "upstream_line_current_a": finite_result(net.res_line, ids["line"], "i_from_ka") * 1000.0,
        "network_active_loss_mw": p_loss,
        "network_reactive_net_mvar": q_net,
        "active_balance_residual_mw": grid_p + solved_pv_p - served_p - p_loss,
        "reactive_balance_residual_mvar": grid_q + solved_pv_q - served_q - q_net,
    }


def bus_value(result: dict, name: str, field: str) -> float:
    return next(b[field] for b in result["buses"] if b["name"] == name)


def acceptance_checks(c: dict, results: dict) -> list[dict]:
    checks = []

    def check(name: str, passed: bool):
        checks.append({"name": name, "passed": bool(passed)})

    for name, r in results.items():
        check(f"{name}: solver converged", r["converged"])
        check(
            f"{name}: active and reactive power balance within 1 W / 1 var",
            abs(r["active_balance_residual_mw"]) <= BALANCE_TOLERANCE
            and abs(r["reactive_balance_residual_mvar"]) <= BALANCE_TOLERANCE,
        )
        check(
            f"{name}: positive active losses and inverter within MVA rating",
            r["network_active_loss_mw"] >= -1e-9
            and math.hypot(r["solved_pv_p_mw"], r["solved_pv_q_mvar"])
            <= c["solar"]["s_rated_mva"] + 1e-9,
        )
        check(
            f"{name}: DC availability, efficiency and AC dispatch consistent",
            0 <= r["pv"]["dc_power_mw"] <= r["pv"]["available_dc_mw"] + 1e-9
            and abs(r["solved_pv_p_mw"] - r["pv"]["dc_power_mw"] * c["solar"]["efficiency_fraction"]) < 1e-9
            and abs(r["solved_pv_p_mw"] - r["pv"]["ac_p_mw"]) < 1e-9
            and abs(r["solved_pv_q_mvar"] - r["pv"]["ac_q_mvar"]) < 1e-9,
        )

    normal, cloudy = results["normal"], results["cloudy"]
    high, opened = results["high-load"], results["breaker-open"]
    support, absorb = results["q-support"], results["q-absorb"]
    limited, curtailed = results["q-limit"], results["curtailed"]
    night, stopped = results["night"], results["pv-stopped"]
    lv_bus = c["load"]["bus"]
    check("Normal: grid imports load minus solar plus losses", normal["grid_p_mw"] > 0)
    check(
        "Clouds reduce solar and increase grid import",
        cloudy["solved_pv_p_mw"] < normal["solved_pv_p_mw"]
        and cloudy["grid_p_mw"] > normal["grid_p_mw"],
    )
    check(
        "Higher load increases breaker current and transformer loading",
        high["cb1_current_a"] > normal["cb1_current_a"]
        and high["t1_loading_pct"] > normal["t1_loading_pct"],
    )
    check("25 MW demand demonstrates T1 overload", high["t1_loading_pct"] > 100.0)
    check(
        "Open breaker: downstream zero volts, no served load or inverter AC output",
        opened["state"] == "downstream_deenergized"
        and bus_value(opened, lv_bus, "voltage_kv") == 0.0
        and opened["served_load_p_mw"] == 0.0
        and opened["served_load_q_mvar"] == 0.0
        and opened["solved_pv_p_mw"] == opened["solved_pv_q_mvar"] == 0.0
        and opened["cb1_current_a"] == 0.0,
    )
    check(
        "Open breaker: upstream remains energized and solar DC voltage remains present",
        bus_value(opened, c["breaker"]["bus"], "voltage_kv") > 0
        and opened["pv"]["dc_voltage_v"] > 0 and opened["pv"]["dc_current_a"] == 0,
    )
    check(
        "Reactive injection reduces grid Q import and raises the load-bus voltage",
        support["grid_q_mvar"] < normal["grid_q_mvar"]
        and bus_value(support, lv_bus, "voltage_kv") > bus_value(normal, lv_bus, "voltage_kv"),
    )
    check(
        "Reactive absorption increases grid Q import and lowers the load-bus voltage",
        absorb["grid_q_mvar"] > normal["grid_q_mvar"]
        and bus_value(absorb, lv_bus, "voltage_kv") < bus_value(normal, lv_bus, "voltage_kv"),
    )
    check(
        "P priority: excessive Q request is limited without reducing active power",
        abs(limited["solved_pv_p_mw"] - normal["solved_pv_p_mw"]) < 1e-9
        and 0 < limited["solved_pv_q_mvar"] < limited["inputs"]["pv_q_set_kvar"] / 1000.0
        and math.isclose(
            math.hypot(limited["solved_pv_p_mw"], limited["solved_pv_q_mvar"]),
            c["solar"]["s_rated_mva"], abs_tol=1e-9,
        ),
    )
    check(
        "Curtailment: 1 MW AC cap reduces actual DC draw below available solar",
        math.isclose(curtailed["solved_pv_p_mw"], 1.0, abs_tol=1e-9)
        and curtailed["pv"]["dc_power_mw"] < curtailed["pv"]["available_dc_mw"],
    )
    check("Low load: grid active power becomes negative during export", results["export"]["grid_p_mw"] < 0)
    check(
        "Night: no P or Q despite a reactive request",
        night["solved_pv_p_mw"] == night["solved_pv_q_mvar"] == 0.0
        and night["pv"]["dc_voltage_v"] == 0 and night["pv"]["efficiency_pct"] is None,
    )
    check(
        "Stopped inverter: no AC output; DC voltage can remain present in sunlight",
        stopped["solved_pv_p_mw"] == stopped["solved_pv_q_mvar"] == 0.0
        and stopped["pv"]["dc_voltage_v"] > 0 and stopped["pv"]["dc_power_mw"] == 0,
    )
    return checks


def print_snapshot(name: str, r: dict) -> None:
    print(f"\nScenario: {name}")
    print(f"Power-flow converged: {r['converged']}")
    print(f"Model state: {r['state']}")
    print(f"\n{'Bus':<20} {'Nominal kV':>11} {'Actual kV':>11} {'Voltage pu':>11} {'Energized':>10}")
    for b in r["buses"]:
        print(
            f"{b['name']:<20} {b['nominal_kv']:>11.3f} {b['voltage_kv']:>11.3f}"
            f" {b['voltage_pu']:>11.6f} {str(b['energized']):>10}"
        )
    print(f"\nLoad demand:          {r['inputs']['load_p_mw']:.4f} MW")
    print(f"Served load:          {r['served_load_p_mw']:.4f} MW")
    print(f"Solar AC:             {r['solved_pv_p_mw']:.4f} MW / {r['solved_pv_q_mvar']:.4f} MVAr")
    print(f"Available solar DC:   {r['pv']['available_dc_mw']:.4f} MW")
    print(f"Actual solar DC draw: {r['pv']['dc_power_mw']:.4f} MW")
    print(f"Grid exchange:        {r['grid_p_mw']:.4f} MW / {r['grid_q_mvar']:.4f} MVAr")
    print(f"Network active loss:  {r['network_active_loss_mw'] * 1000:.3f} kW")
    print(f"CB1 HV current:       {r['cb1_current_a']:.3f} A")
    print(f"T1 current loading:   {r['t1_loading_pct']:.3f} %")
    print(f"Active balance error: {r['active_balance_residual_mw'] * 1e6:.6f} W")
    print(f"Reactive balance err: {r['reactive_balance_residual_mvar'] * 1e6:.6f} var")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="normal")
    parser.add_argument("--verify", action="store_true", help="Run all 11 scenarios and engineering checks.")
    parser.add_argument("--config", type=Path, default=ROOT / "config" / "substation.json")
    args = parser.parse_args()
    try:
        c = load_config(args.config)
        expected = c["software"]["pandapower"]
        print(f"pandapower: {pp.__version__} | Python: {sys.version.split()[0]}")
        if pp.__version__ != expected:
            print(f"NOTE: Phase 3 specifies pandapower {expected}; installed version differs.")
        print("Educational balanced steady-state model; assumed equipment parameters.")
        scenarios = SCENARIOS if args.verify else (args.scenario,)
        results = {name: run_powerflow(c, operating_point(c, name)) for name in scenarios}
        print_snapshot(args.scenario, results[args.scenario])
        if not args.verify:
            print("\nRun with --verify for the complete Phase 4 checks.")
            return 0

        print("\nScenario comparison (positive grid MW = import; negative = export)")
        print(f"{'Scenario':<15} {'Grid MW':>10} {'PV MW':>9} {'PV MVAr':>10} {'Load-bus kV':>12} {'T1 %':>10}")
        for name, r in results.items():
            print(
                f"{name:<15} {r['grid_p_mw']:>10.4f} {r['solved_pv_p_mw']:>9.4f}"
                f" {r['solved_pv_q_mvar']:>10.4f}"
                f" {bus_value(r, c['load']['bus'], 'voltage_kv'):>12.4f}"
                f" {r['t1_loading_pct']:>10.3f}"
            )
        checks = acceptance_checks(c, results)
        failures = [check for check in checks if not check["passed"]]
        report = {
            "phase": 4,
            "created_utc": datetime.now(timezone.utc).isoformat(),
            "python_version": sys.version.split()[0],
            "pandapower_version": pp.__version__,
            "config_path": str(args.config.resolve()),
            "config_sha256": hashlib.sha256(args.config.read_bytes()).hexdigest(),
            "model_scope": "Balanced steady-state AC; assumed equipment; static P/Q inverter with explicit limits.",
            "validation_scope": "Numerical power balance and scenario checks; no field/manufacturer validation or switching-waveform model.",
            "all_checks_passed": not failures,
            "checks": checks, "scenarios": results,
        }
        output = ROOT / "artifacts" / "phase4_results.json"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(f"\nChecks passed: {len(checks) - len(failures)}/{len(checks)}")
        for check in failures:
            print(f"FAIL: {check['name']}")
        print(f"Report: {output}")
        print("PHASE 4 VERIFICATION: " + ("FAILED" if failures else "PASS"))
        return 1 if failures else 0
    except (OSError, ValueError, KeyError, TypeError, LoadflowNotConverged) as error:
        print(f"\nERROR: {error}", file=sys.stderr)
        print("No valid snapshot was produced for this run. Check the configuration and error.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
