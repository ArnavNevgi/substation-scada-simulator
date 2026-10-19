# Live Overview acceptance — completed 19 October 2026

Project: **SubstationSCADA**, Ignition **8.3.10 (b2026100610)**.

The user performed these checks on the local Windows simulator and Ignition runtime. The supplied screenshots were visually reviewed. All eight observations below pass.

| Check | Observed result | Screenshot |
|---|---|---|
| Normal | Native imported Overview renders with CB1 CLOSED, PV RUNNING, model VALID, bus ENERGIZED and telemetry HEALTHY. Grid 8056 kW, PV 2000 kW, bus 13.61 kV, T1 42.1%. | [01_normal.png](01_normal.png) |
| Pv stopped | PV reads STOPPED and 0 kW. Grid-fed bus and T2 AC conductors stay green; CB1 remains CLOSED and telemetry HEALTHY. Grid 10053 kW, bus 13.60 kV, T1 51.9%. | [02_pv_stopped.png](02_pv_stopped.png) |
| Pv restarted | Controls displays START PV1 sequence 7 APPLIED. PV is RUNNING, CB1 CLOSED and model VALID; grid 8056 kW and PV 2000 kW. | [03_pv_restarted_controls.png](03_pv_restarted_controls.png) |
| Cb1 open | CB1 OPEN has a visible gap. Upstream grid remains green; downstream conductors are gray, bus DE-ENERGIZED, PV TRIPPED in red. Grid/PV/bus/T1/served load read zero. The isolated model is VALID and telemetry HEALTHY. | [04_cb1_open.png](04_cb1_open.png) |
| Cb1 restored | Overview is back to CB1 CLOSED, PV RUNNING, model VALID, bus ENERGIZED and telemetry HEALTHY. Grid 8056 kW, PV 2000 kW, bus 13.61 kV, T1 42.1%, served load 10000 kW. | [05_cb1_restored.png](05_cb1_restored.png) |
| Restoration audit | CommandHistory shows operator1 APPLIED for sequences 8 OPEN CB1, 9 RESET TRIPS, 10 CLOSE CB1 and 11 START PV1. Matching REQUESTED/APPLIED pairs are fully visible for 9, 10 and 11; the sequence 8 REQUESTED row is partly below the screenshot crop. | [06_restoration_audit.png](06_restoration_audit.png) |
| Communication outage | Overview reads DATA UNAVAILABLE. All conductors are amber; CB1 has a question mark and UNKNOWN text; PV, model and bus are UNKNOWN. All four KPIs and served-load text are unavailable. Telemetry reads CHECK ALARMS - data unavailable or invalid. | [07_communication_outage.png](07_communication_outage.png) |
| Communication recovery | Overview again reads telemetry HEALTHY, model VALID, CB1 CLOSED, PV RUNNING and bus ENERGIZED. Conductors are green. Grid 8056 kW, PV 2000 kW, bus 13.61 kV, T1 42.1%, served load 10000 kW. | [08_communication_recovery.png](08_communication_recovery.png) |

The eight images in this folder are live runtime evidence supplied by the user. The native project export in this repository preserves the reviewed Overview.

The audit screenshot fully shows matching REQUESTED/APPLIED pairs for sequences 9–11, and APPLIED for sequence 8. The sequence 8 REQUESTED row is partially below the crop.

This is visual acceptance of the displayed states. No packet capture, measured response latency, measured timeout duration, or field-device validation is claimed. A simulator restart may initialize its states.

`live_acceptance.json` records the observations, each screenshot hash, and the complete latest native project export reference. Its Overview JSON matches the reviewed live Overview.
