# Substation design and signal definitions

These are the engineering assumptions and signal definitions for the implemented local educational SCADA project. Equipment ratings, impedances, short-circuit strength and protection thresholds are illustrative assumptions. They are not parameters from an operating utility substation. The design uses repeated balanced steady-state power-flow solutions, with separate fault calculations and explicit equipment-state logic in Python.

## Electrical connections

GRID_115 supplies L1, a 5 km line ending at LINE_SIDE_115. CB1 connects LINE_SIDE_115 to TX_HV_115. T1 connects TX_HV_115 to BUS_13_8. LOAD1 connects to BUS_13_8. T2 connects BUS_13_8 to SOLAR_AC_0_69, where PV1 injects active and reactive power.

T1 is rated 20 MVA at 115/13.8 kV. T2 is rated 3 MVA at 13.8/0.69 kV. PV1 is rated 2 MW active power and 2.5 MVA apparent power. All voltage ratings are line-to-line RMS. Main operating inputs start at a 10 MW + 2 MVAr load, 1000 W/m2 sunlight, 2 MW PV active-power cap, and zero PV reactive-power request.

T1 parameters: vk = 10%, vkr = 0.5%, no-load active loss = 20 kW, no-load current = 0.1%. T2 parameters: vk = 6%, vkr = 1%, no-load active loss = 3 kW, no-load current = 0.2%. In this model both transformers have fixed nominal ratios and zero assumed phase shift. L1 uses R = 0.08 ohm/km, X = 0.40 ohm/km and C = 10 nF/km at the assumed 60 Hz frequency. The grid short-circuit strengths are assumed 1000 MVA maximum and 800 MVA minimum, with R/X = 0.1. Exact values live in `simulator/config/substation.json`.

## Electrical calculations and operating rules

pandapower calculates bus voltage, branch current, active/reactive power, losses and transformer loading. Python models sunlight, inverter efficiency, P/Q limits, breaker operation, command acknowledgements, protection latches and illustrative temperature. System frequency is fixed at 60 Hz; this is not a frequency-stability or synchrophasor model.

PV1 uses a grid-following P/Q injection. It cannot provide an island's grid reference. Opening CB1 deenergizes the downstream network in our model and stops the inverter's AC injection. Upstream line-side voltage can remain present. DC voltage may remain present in sunlight even while the inverter is disconnected. The upstream line can still draw small charging current; CB1 current is defined at T1's HV winding so it becomes zero when the breaker opens.

At full sunlight, available DC power is 2.1 MW. AC active power is limited to the smaller of 98% of the available DC power and the active-power cap, provided operating permissives are satisfied. Actual DC input is AC active power / 0.98. This distinguishes available solar power from the DC power actually drawn during curtailment. The efficiency display is N/A when DC input is zero. Standby losses and switching waveforms are outside this model.

The apparent-power limit is enforced explicitly in Python: P^2 + Q^2 <= (2.5 MVA)^2, with active-power priority. At 2 MW, the ideal remaining reactive capability is +/-1.5 MVAr. A rating field alone does not enforce this operating circle in an ordinary static-generator power-flow calculation. There is no night reactive-power operation in this model.

Positive Grid_P means import; negative means export. Positive Grid_Q means reactive import. Positive PV_Q means reactive injection by the inverter; negative means absorption. At the same load, less solar normally increases grid import. Grid import plus solar generation equals served load plus network losses. The packaged power-flow verifier checks these balances across the implemented operating scenarios.

## Modbus data contract

Full-server connection: 127.0.0.1:1502, unit ID 1, Ignition device SubstationSim, Zero-based Addressing enabled. The combined server allocates holding registers 0..127 and discrete inputs 0..15 as separate blocks. Allocated reserved addresses read as zero; they are not meaningful points. This avoids undefined holes inside a driver's grouped read. The current server enforces telemetry write restrictions and validates writable input ranges.

The current combined server implements all 41 mapped points. Register 0 stays the voltage in hundredths of kV, and register 1 stays the heartbeat. Scaling is engineering value = decoded raw value * scale. Use HRUS for unsigned words and HR for signed words; use DI for Boolean status feedback. The point count is 41 Modbus signals; scaled/derived Ignition tags are additional.

## Commands and feedback

HR100 is the command code. HR101 is the commit sequence. Write the command code, wait for its write to finish successfully, and then write a new sequence. The application processes each new sequence once. HR21 echoes the acknowledged sequence; HR19 reports pending/applied/rejected/failed, and HR20 gives the reason. Wait for a final status before issuing another command. Counter values are uint16 and wrap modulo 65536; a new sequence must differ from the preceding one.

Command codes: 0 none, 1 open CB1, 2 close CB1, 3 reset cleared trip latches, 4 start PV1, 5 stop PV1. Command status: 0 idle, 1 pending, 2 applied, 3 rejected, 4 failed. Reasons and model-state enums are documented in substation.json.

CB1_Closed is physical-position feedback, not a command. Close requires remote mode, no active fault, cleared trip latch, available upstream grid and valid model state. Inverter start also requires an energized AC bus and other inverter permissives. Protection remains able to trip in local mode. Reset clears eligible latches after the fault has been removed and never automatically closes CB1. Test controls for demand, sunlight, fault enable and local/remote selection belong in a clearly labelled simulation-input panel.

## Faults and data quality

The first fault example is a three-phase bolted fault at BUS_13_8. Calculate its grid contribution separately on a network copy using IEC 60909 assumptions, with PV excluded from the initial short-circuit study. The HV overcurrent pickup of 200 A, one-second timer and 0.3-second breaker movement delay are illustrative training settings. They are not relay-coordination results or protection-grade real-time specifications. Polling and event timing do not represent captured transient waveforms. LastFault_Ikss_kA retains the latest event's estimated initial symmetrical RMS current after a trip and across latch reset, and initializes to zero on server restart; it is not a live current reading after deenergization.

Communication quality, model validity and model heartbeat are separate checks. A failed solve keeps last valid analog values and sets Model_Valid false; it must not manufacture a false zero-voltage indication. A known deenergized state is valid and can legitimately show zero downstream voltage and served power. The heartbeat advances after each completed model-update attempt, including handled failures; it stops when model progress is deliberately frozen. A five-second heartbeat timeout is a training threshold. Low-voltage alarms require valid, energized-bus measurements so an intentional open breaker does not masquerade as a voltage-regulation fault.

## Sources for software behaviour

- [Static-generator conventions and limitations](https://pandapower.readthedocs.io/en/v3.5.6/elements/sgen.html)
- [Transformer models](https://pandapower.readthedocs.io/en/v3.5.6/elements/trafo.html)
- [Switch models](https://pandapower.readthedocs.io/en/v3.5.6/elements/switch.html)
- [Short-circuit calculation scope](https://pandapower.readthedocs.io/en/stable/shortcircuit.html)
- [Ignition Modbus addressing](https://www.docs.inductiveautomation.com/docs/8.3/ignition-modules/opc-ua/opc-ua-drivers/modbus/modbus-addressing)

These sources describe software behaviour. They are not sources for the assumed equipment ratings or utility settings.

## Complete point list

### Measurements and diagnostics

| Signal | OPC item path | Type | Scale | Unit | Access |
|---|---|---|---:|---|---|
| LineSide_Voltage_kV | `[SubstationSim]1.HRUS0` | uint16 | 0.01 | kV | R |
| Heartbeat | `[SubstationSim]1.HRUS1` | uint16 | 1 | count | R |
| Bus_Voltage_kV | `[SubstationSim]1.HRUS2` | uint16 | 0.01 | kV | R |
| CB1_Current_A | `[SubstationSim]1.HRUS3` | uint16 | 0.1 | A | R |
| Grid_P_kW | `[SubstationSim]1.HR4` | int16 | 1 | kW | R |
| Grid_Q_kVAr | `[SubstationSim]1.HR5` | int16 | 1 | kVAr | R |
| Load_P_kW | `[SubstationSim]1.HRUS6` | uint16 | 1 | kW | R |
| Load_Q_kVAr | `[SubstationSim]1.HR7` | int16 | 1 | kVAr | R |
| T1_Loading_pct | `[SubstationSim]1.HRUS8` | uint16 | 0.1 | % | R |
| PV_Available_DC_kW | `[SubstationSim]1.HRUS9` | uint16 | 1 | kW | R |
| PV_DC_Power_kW | `[SubstationSim]1.HRUS10` | uint16 | 1 | kW | R |
| PV_DC_Voltage_V | `[SubstationSim]1.HRUS11` | uint16 | 1 | V | R |
| PV_DC_Current_A | `[SubstationSim]1.HRUS12` | uint16 | 0.1 | A | R |
| PV_AC_Power_kW | `[SubstationSim]1.HRUS13` | uint16 | 1 | kW | R |
| PV_Q_kVAr | `[SubstationSim]1.HR14` | int16 | 1 | kVAr | R |
| PV_Efficiency_pct | `[SubstationSim]1.HRUS15` | uint16 | 0.01 | % | R |
| PV_Temperature_C | `[SubstationSim]1.HR16` | int16 | 0.1 | degC | R |
| Grid_Frequency_Hz | `[SubstationSim]1.HRUS17` | uint16 | 0.01 | Hz | R |
| Model_State | `[SubstationSim]1.HRUS18` | uint16 | 1 | enum | R |
| Command_Status | `[SubstationSim]1.HRUS19` | uint16 | 1 | enum | R |
| Command_Reason | `[SubstationSim]1.HRUS20` | uint16 | 1 | enum | R |
| Command_Ack_Sequence | `[SubstationSim]1.HRUS21` | uint16 | 1 | count | R |
| LastFault_Ikss_kA | `[SubstationSim]1.HRUS22` | uint16 | 0.001 | kA | R |


### Operating commands

| Signal | OPC item path | Type | Scale | Unit | Access |
|---|---|---|---:|---|---|
| Command_Code | `[SubstationSim]1.HRUS100` | uint16 | 1 | enum | RW |
| Command_Sequence | `[SubstationSim]1.HRUS101` | uint16 | 1 | count | RW |


### Operating setpoints and load inputs

| Signal | OPC item path | Type | Scale | Unit | Access |
|---|---|---|---:|---|---|
| Load_P_Set_kW | `[SubstationSim]1.HRUS110` | uint16 | 1 | kW | RW |
| Load_Q_Set_kVAr | `[SubstationSim]1.HR111` | int16 | 1 | kVAr | RW |
| Irradiance_Set_W_m2 | `[SubstationSim]1.HRUS112` | uint16 | 1 | W/m2 | RW |
| PV_P_Limit_Set_kW | `[SubstationSim]1.HRUS113` | uint16 | 1 | kW | RW |
| PV_Q_Set_kVAr | `[SubstationSim]1.HR114` | int16 | 1 | kVAr | RW |


### Scenario inputs

| Signal | OPC item path | Type | Scale | Unit | Access |
|---|---|---|---:|---|---|
| Fault_Enable | `[SubstationSim]1.HRUS115` | uint16 | 1 | bool | RW |
| Remote_Mode_Set | `[SubstationSim]1.HRUS116` | uint16 | 1 | bool | RW |


### Equipment and model feedback

| Signal | OPC item path | Type | Scale | Unit | Access |
|---|---|---|---:|---|---|
| CB1_Closed | `[SubstationSim]1.DI0` | bool | 1 | bool | R |
| Trip_Latched | `[SubstationSim]1.DI1` | bool | 1 | bool | R |
| PV_Running | `[SubstationSim]1.DI2` | bool | 1 | bool | R |
| Grid_Available | `[SubstationSim]1.DI3` | bool | 1 | bool | R |
| Model_Valid | `[SubstationSim]1.DI4` | bool | 1 | bool | R |
| Bus_Energized | `[SubstationSim]1.DI5` | bool | 1 | bool | R |
| Remote_Mode | `[SubstationSim]1.DI6` | bool | 1 | bool | R |
| Fault_Active | `[SubstationSim]1.DI7` | bool | 1 | bool | R |
| PV_Tripped | `[SubstationSim]1.DI8` | bool | 1 | bool | R |

