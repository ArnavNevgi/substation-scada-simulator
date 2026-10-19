# Access controls and data health

## Identity and action permissions

The project selects the `SubstationIdP` identity provider and requires `Authenticated/Roles/Viewer` for a Perspective session. A local Operator account has both Operator and Viewer roles.

All nine equipment/scenario script actions on Controls have native action permissions requiring `Authenticated/Roles/Operator`. Their enabled-state expressions also reflect that role. The configured native action permission is the equipment-action gate; the visual enabled state supports the interface.

Alarm acknowledgement, shelving and unshelving controls use `isAuthorized(false, "Authenticated/Roles/Operator")` bindings. Sign Out requires Viewer, which keeps it available to both account types. The six views share the session-access requirement.

This authorization is scoped to the configured Perspective actions. Trusted Gateway/Designer scripts and a direct localhost Modbus client are separate execution paths. Future callable endpoints require their own authorization. The current command helper records the supplied Perspective identity; its library function is not a universal authorization boundary.

## Independent health signals

| Signal | Meaning |
|---|---|
| Tag quality / value validation | Required reads have Good quality, non-null values and finite numeric data |
| Heartbeat freshness | The heartbeat value has changed between good samples within the five-second timeout |
| Communications healthy | Quality/value checks and heartbeat freshness are satisfied |
| Model validity | The current electrical model reports a usable solution; independent of communication availability |
| Monitor alive | A separate expression watchdog sees a recent successful monitor publication |
| Telemetry usable | Health and model conditions permit process use |

`phase8_monitor` observes 41 OPC points and 18 derived engineering tags. Its first sample establishes a baseline, and a later observed change establishes freshness. Counter rollover from 65535 to 0 is accepted. Freshness uses monotonic elapsed time rather than the OPC tag timestamp or a successful read alone.

Bad/uncertain quality, missing values, non-finite numbers and invalid heartbeat values invalidate use. Quality recovery establishes a new heartbeat baseline. A stalled heartbeat can occur while TCP communication and OPC quality remain Good; the dedicated freeze fixture verifies this distinction.

The Gateway timer runs once per second with fixed delay and dedicated threading. It writes the Health folder and leaves the equipment-command and telemetry points to their owning paths.

## Alarm enable behavior

Eleven included alarm definitions cover monitor failure, data quality, heartbeat stall, model invalidity, transformer loading, bus voltage, and fault/trip latches. Transformer alarms require usable data. Voltage alarms additionally require an energized bus. Fault/trip alarms use healthy communications, allowing those conditions to be reported while a normal solved model is unavailable.

The normal voltage thresholds are 13.11/14.49 kV with 0.138 kV deadband. T1 warning/overload thresholds are 90/100% with 3-percentage-point deadband. Activation delays and priority are stored in the tag export. These are illustrative SCADA alarm settings, separate from the engine's training protection.

## Operator display behavior

The live Overview checks quality/non-null inputs, communication health and model validity before showing operational states. Unusable data produces amber conductors, UNKNOWN device/bus/model states and unavailable analog readings. Known valid isolation produces gray downstream conductors with valid zero measurements.

Controls and Trends have health banners. The chart's `breakLine` settings can show returned bad/missing historian values, while an unchanged heartbeat with Good OPC quality is explained by the health indication. A historical numeric value alone does not establish present freshness.

The [communication outage and recovery images](../evidence/live-overview/README.md) demonstrate the Overview's native runtime handling. The offline health tests cover additional edge conditions using explicit API substitutes.
