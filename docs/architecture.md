# Architecture and electrical model

## Electrical topology

The external grid at `GRID_115` supplies a 5 km line, the CB1 bus-bus switch, and T1. T1 supplies the 13.8 kV load bus. T2 connects a static solar injection at 0.69 kV to that bus.

| Element | Model / assumed rating |
|---|---|
| Source | 115 kV, 60 Hz external-grid reference |
| L1 | 5 km; explicit R, X and capacitance parameters |
| CB1 | Controllable bus-bus switch; 0.3 s illustrative movement delay |
| T1 | 20 MVA, 115/13.8 kV transformer |
| LOAD1 | Constant demand of 10 MW and 2 MVAr in the normal case |
| T2 | 3 MVA, 13.8/0.69 kV transformer |
| PV1 | 2 MW active-power cap; 2.5 MVA static P/Q capability; assumed 98% conversion efficiency |

The implemented model has one controllable breaker, CB1. Five named buses distinguish the line terminal, breaker terminal, main LV bus and solar AC bus. The [configuration](../simulator/config/substation.json) retains the complete assumptions.

The balanced AC solver calculates voltages, currents, losses, grid exchange and transformer loading. Its verified scenarios include cloud cover, load increase, breaker isolation, reactive support/absorption, apparent-power limiting, curtailment, export, night and PV stop. A known unsupplied bus can have valid zero voltage and zero served load.

The PV active-power request is bounded by available irradiance, equipment capability and its operator limit. Active power has priority within `P² + Q² ≤ S²`. At 2 MW, the 2.5 MVA capability leaves 1.5 MVAr of reactive capability. A stopped or islanded inverter supplies no active or reactive power under the implemented rules.

## Runtime ownership

`phase7_modbus_server.py` combines the operating model, control engine and Modbus interface. A control task advances delayed operations. A model task solves operating points in a worker thread. Each electrical-input change increments a control revision; a result calculated for an obsolete revision is discarded before publication.

The device exposes separate HR and DI areas. It rejects writes to telemetry, reserved points and out-of-range inputs. The complete allocated HR block can be read without holes, while reserved locations return zero. Encoding checks signedness, scaling and 16-bit range.

Ignition's OPC device polls the simulated device and presents the values as tags. A Gateway timer observes 59 required tag paths, publishes health state and drives alarm enable conditions. The Overview combines current quality, communications and model validity with electrical status to determine its display state.

## Protection and fault path

`phase7_fault.py` computes a grid-only three-phase bolted-fault reference using pandapower's short-circuit calculation. The radial series-impedance calculation includes the external-grid source, L1 and corrected T1 impedance, and independently checks the result. The reference is approximately 7.445 kA at the 13.8 kV bus and 893 A referred to the CB1 HV side.

The training control engine uses an illustrative 200 A pickup and 1.0 s definite-time delay, followed by the configured breaker movement. Clearing a fault retains trip latches. Reset grants permission without moving the breaker or enabling PV. Close and PV enable are subsequent explicit commands with their own permissives.

## Display semantics

Green conductors mean the modeled connection is energized; gray means known de-energized; amber means data is unknown/unusable. Red device indication identifies a latched trip. PV operating state and its grid-fed AC connection are separate: stopping PV leaves T2's AC branch energized when the bus is supplied.

CB1 position is also separate from voltage availability. An isolated bus may have healthy data and valid zeros. Unusable telemetry produces UNKNOWN states and unavailable readings in the Overview.

## Persistence

The historian stores timestamped tag values. The database alarm journal stores alarm lifecycle events. `SubstationAudit` records explicit equipment-command requests/outcomes, plus automatic project auditing for configured tag writes. These are separate persistence mechanisms with different identifiers and meanings.

The simulator's equipment state and sequence counters are process-local. Its restart behavior initializes a new modeled run. The native project export and tag JSON preserve configuration; the historical databases are Gateway dependencies.
