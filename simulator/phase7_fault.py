"""Grid-only IEC 60909 three-phase fault study for the fixed Phase 3 network."""

from copy import deepcopy
import math

from pandapower.shortcircuit import calc_sc

from phase4_powerflow import build_network, operating_point


def independent_series_current(config):
    """Independent positive-sequence series calculation, referred to T1 HV.

    Valid for this radial, single-source, fixed nominal-ratio network only.
    Uses IEC max voltage factor 1.1 and the two-winding transformer K_T factor.
    Loads, line shunts, T2's unloaded branch and PV are excluded from the study.
    """
    g, line, t = (config[k] for k in ("external_grid", "line", "main_transformer"))
    voltage_kv = t["vn_hv_kv"]
    cmax = 1.1
    z_grid = cmax * voltage_kv ** 2 / g["s_sc_max_mva"]
    x_grid = z_grid / math.sqrt(1 + g["rx_max"] ** 2)
    r_grid = g["rx_max"] * x_grid
    base_z = voltage_kv ** 2 / t["sn_mva"]
    z_t = t["vk_percent"] / 100 * base_z
    r_t = t["vkr_percent"] / 100 * base_z
    x_t = math.sqrt(z_t ** 2 - r_t ** 2)
    k_t = 0.95 * cmax / (1 + 0.6 * x_t / base_z)
    z = complex(r_grid, x_grid) + k_t * complex(r_t, x_t)
    z += line["length_km"] * complex(line["r_ohm_per_km"], line["x_ohm_per_km"])
    hv_a = cmax * voltage_kv * 1000 / (math.sqrt(3) * abs(z))
    return hv_a * voltage_kv / t["vn_lv_kv"] / 1000


def calculate_fault_study(config):
    """Calculate once; equipment/source parameters stay fixed while running.

    Use bus results, then refer current through the sole supplying transformer
    using its fixed nominal turns ratio. No beta branch-result routine is used.
    """
    t = config["main_transformer"]
    if config["protection"]["fault_location_bus"] != t["lv_bus"]:
        raise ValueError("This Phase 7 study requires a fault at T1's LV bus.")
    if t["vn_hv_kv"] <= 1 or t["vn_lv_kv"] <= 1:
        raise ValueError("The reference check assumes both T1 buses exceed 1 kV.")
    net, ids = build_network(config, operating_point(config, "normal"))
    study = deepcopy(net)
    study.switch.at[ids["breaker"], "closed"] = True
    # Both transformers belong to the substation, not a synchronous generator
    # power-station unit. Explicit Boolean dtype also avoids a pandas warning.
    study.trafo["power_station_unit"] = False
    study.sgen["in_service"] = False
    study.load["in_service"] = False
    fault_bus = ids["buses"][config["protection"]["fault_location_bus"]]
    calc_sc(study, bus=fault_bus, fault="3ph", case="max",
            r_fault_ohm=0.0, x_fault_ohm=0.0, branch_results=False)
    bus_ka = float(study.res_bus_sc.at[fault_bus, "ikss_ka"])
    hv_a = bus_ka * 1000 * t["vn_lv_kv"] / t["vn_hv_kv"]
    reference_ka = independent_series_current(config)
    if not all(math.isfinite(x) and x > 0 for x in (bus_ka, hv_a, reference_ka)):
        raise ValueError("Fault study returned invalid current.")
    relative_error = abs(bus_ka - reference_ka) / reference_ka
    if relative_error > 1e-6:
        raise ValueError("Fault study disagrees with the independent series-impedance check.")
    return {
        "bus": config["protection"]["fault_location_bus"],
        "fault": "3ph bolted", "case": "max", "contributions": "external grid only",
        "bus_ikss_ka": bus_ka,
        "cb1_hv_current_a": hv_a,
        "independent_bus_ikss_ka": reference_ka,
        "relative_error": relative_error,
        "hv_current_basis": "LV Ikss referred by fixed T1 nominal turns ratio; radial single-source study",
    }
