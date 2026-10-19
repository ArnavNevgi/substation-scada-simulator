# Local setup

The supplied snapshot uses Ignition 8.3.10 (build b2026100610), Python 3.14.3 on Windows, pandapower 3.5.6 and PyModbus 3.16.1. The Python source also passes the packaged checks on Python 3.12.14/Linux. The instructions below describe a new local installation; an already working installation can keep its current settings.

## 1. Start Python

Extract the repository and open PowerShell in its root directory. Create the virtual environment and install `requirements.txt` as shown in the main README. Run `simulator/phase7_modbus_server.py` using that environment.

The server reads `simulator/config/substation.json` and `simulator/config/points.json` relative to its own location. It starts at `127.0.0.1:1502`, unit 1. Start one server on that endpoint. Its normal initial state is CB1 closed, remote mode, fault removed and PV enabled.

A normal startup calculates the fault reference, publishes a solved operating point, and begins its model and control loops. A process restart initializes the simulated inputs, breaker state, sequence counters and trip latches from the configuration.

## 2. Configure Gateway resources

Install/activate the local Ignition Maker environment with the OPC/device, Perspective and historian capabilities used by this project. Configure these dependencies before expecting a complete imported application:

| Resource | Required configuration |
|---|---|
| Modbus TCP device | Name `SubstationSim`; host `127.0.0.1`; port `1502`; unit 1; **zero-based addressing enabled** |
| Reconnect/polling | Confirm a changing `Diagnostics/Heartbeat` and fresh Good-quality reads; the captured tag polling was approximately once per second |
| Tag provider | `default` |
| Historian provider | `SubstationHistory`; the original project uses a Core Historian provider |
| Alarm journal | Database journal named `Sample_Alarm_Journal_Profile`; connect it to an available local SQL/SQLite database |
| Default datasource | The original project references `Sample_SQLite_Database`; create that connection or select your local default datasource in project settings |
| Audit profile | Local profile named `SubstationAudit` |
| User source | Internal source named `SubstationUsers`, with roles exactly `Viewer` and `Operator` |
| Identity provider | Ignition IdP named `SubstationIdP`, using that user source; map the roles to `Authenticated/Roles/Viewer` and `Authenticated/Roles/Operator` |
| Accounts | A Viewer account with Viewer role; an Operator account with both Operator and Viewer roles; choose local passwords |

The observed accounts are `viewer1` and `operator1`; their credentials are configured locally. An Operator also needs Viewer because the project's session permission requires Viewer.

The working AlarmHistory view intentionally references the database profile `Sample_Alarm_Journal_Profile`. The unused Internal journal name `SubstationAlarmJournal` is not required by this snapshot. Its query behavior was not resolved or validated as an alternative.

## 3. Import project and tags

1. Open the Gateway's **Platform → System → Projects** page and import `ignition/SubstationSCADA_project.zip` as **SubstationSCADA**.
2. Open that project in Designer.
3. In Tag Browser, select the `default` provider root. Import `ignition/Substation_tags.json` there. The JSON contains the `Substation` folder; selecting that folder as the destination would nest it incorrectly.
4. For a new installation, the tag names should have no collisions. For an existing installation, review collisions against the saved tag configuration before choosing an overwrite policy.
5. Confirm `Project Properties` uses IdP `SubstationIdP` and audit profile `SubstationAudit`, with project auditing enabled.
6. Save the project.

The native project ZIP includes all six views, seven page routes, both Project Library modules, session permissions, and the `Phase8Health` Gateway timer. Separate script pasting or additional timers are unnecessary when importing this complete export.

The unpacked `ignition/project/` folder and readable `ignition/scripts/` copies mirror the native export. Use Designer changes followed by a fresh export to update this snapshot; changes to one readable copy do not automatically update the ZIP.

## 4. Check names and historical sources

The imported Power Chart pens reference:

```text
histprov:SubstationHistory:/sys:Ignition-Nevgi:/prov:default:/tag:Substation/Measurements/Grid_P_kW
histprov:SubstationHistory:/sys:Ignition-Nevgi:/prov:default:/tag:Substation/Measurements/PV_AC_Power_kW
```

If the Gateway name differs from `Ignition-Nevgi`, browse and reselect those two local historical tags in the Power Chart. The chart has a ten-minute range and 2,000 ms refresh. `config.mode` is omitted in this native export; confirm **realtime** in Designer, setting it explicitly if needed on another installation.

Confirm the imported timer is enabled, dedicated, fixed-delay and set to 1,000 ms. Its handler calls `phase8_monitor.tick()`.

## 5. Read-only readiness checks

Wait for Good OPC quality, an observed heartbeat change, and a valid model. The first heartbeat sample is a baseline; the health monitor requires a later change before reporting freshness.

In Designer's Script Console:

```python
print(phase7_controls.VERSION)
print(phase7_controls.check_installation())
```

Expect version `11.1` and valid feedback/audit-query fields. This check issues no equipment command. Under `[default]Substation/Health`, confirm `Monitor_Alive`, `Communications_Healthy` and `Telemetry_Usable` are true in normal operation.

Launch the project from the Gateway or open:

```text
http://localhost:8088/data/perspective/client/SubstationSCADA/
```

The `/` route opens Overview. Sign in as the local Viewer/Operator accounts and confirm their configured permissions. Historical data, journal rows and audit rows accumulate in the new Gateway; the export contains configuration rather than the original stored records.

## Checks to repeat after a new installation

Use the [recorded acceptance results](../evidence/live-overview/README.md) as expected behavior. Confirm normal readings, PV stop/start, CB1 opening/restoration, and communication loss/recovery. Wait for each command outcome and fresh valid model data before the next equipment command.

The automated checks in [verification](verification.md) use isolated test instances and test doubles where identified. They do not install or configure a Gateway.

## References

- [Ignition project export/import](https://www.docs.inductiveautomation.com/docs/8.3/platform/projects/project-export-and-import)
- [Perspective Power Chart](https://www.docs.inductiveautomation.com/docs/8.3/appendix/components/perspective-components/perspective-chart-palette/perspective-power-chart)
- [Connecting a Modbus device in Ignition 8.3](https://www.docs.inductiveautomation.com/docs/8.3/ignition-modules/opc-ua/opc-ua-drivers/modbus/connecting-to-modbus-device)
