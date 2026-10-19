# Verification and evidence

The packaged source was checked on Linux with Python 3.12.14, pandapower 3.5.6 and PyModbus 3.16.1. The Windows Ignition 8.3.10 runtime evidence was supplied by the user and reviewed separately. These scopes are kept distinct.

## Fresh packaged-source checks

| Suite | Result | What it checks |
|---|---|---|
| Balanced AC power flow | 57/57 | Eleven operating scenarios, voltage/loading behavior, P/Q balances, limits and reference conditions |
| Control engine and actual Modbus TCP | 63/63 | Timing/interlocks, separate code/sequence writes, duplicates, pending state, model failures, stale-worker rejection and HR/DI read consistency |
| Gateway health monitor with API substitutes | 56/56 | Quality/value validation, heartbeat freshness/rollover/recovery, watchdog, alarm configuration and preservation of existing tags |
| Heartbeat-freeze actual TCP fixture | 9/9 | Heartbeat freeze while communication, setpoints, model changes, command protocol and telemetry write protection remain active |
| Current command audit helper | 34/34 | Real local TCP feedback frames with substituted Ignition/Java APIs; identity, audit failures, sequence handling, uncertain outcomes, reconciliation and history schema |
| Packet analyzer | 15/15 | Known wire bytes, TCP reassembly/matching validation and the supplied STOP/START trace |
| Native export/tag consistency | PASS | Resource-byte equality, routes, tag references, point map, timer settings, script syntax and native action permissions |

The older 70-check control verifier included seven checks for the initial pre-audit helper. Those checks are superseded by the current 34-test audit suite. The packaged control/TCP verifier retains its 63 applicable engine/network checks and uses the current simulator source. Only test-harness layout paths and the superseded legacy-helper block were adjusted; application source and the native export were preserved.

Fresh reports are under `validation/`. The simulator scripts also write their normal generated reports to `simulator/artifacts/` when rerun.

The included [GitHub Actions workflow](../.github/workflows/verify.yml) runs these commands on Python 3.12 with read-only repository permissions. The equivalent commands passed during local packaging; the hosted workflow has not yet run because this package has not been published.

## Rerun from the repository root

After installing `requirements.txt` into the local virtual environment:

```powershell
.\.venv\Scripts\python.exe .\simulator\phase4_powerflow.py --verify
.\.venv\Scripts\python.exe .\simulator\tests\verify_phase7.py
.\.venv\Scripts\python.exe .\simulator\tests\verify_phase8.py
.\.venv\Scripts\python.exe .\simulator\tests\verify_phase8_tcp.py
.\.venv\Scripts\python.exe .\simulator\tests\verify_phase11.py
.\.venv\Scripts\python.exe .\evidence\modbus\test_analyzer.py
.\.venv\Scripts\python.exe .\tools\check_project.py
```

The TCP verifiers create isolated localhost instances on temporary ports. The health/audit API substitutes do not connect to a Gateway or configure tags. The tests validate their declared source/fixture scope rather than a live Ignition installation.

`verify_phase5.py` is retained as an optional test of the underlying earlier telemetry bridge. The normal application entry point is `phase7_modbus_server.py`.

## Native runtime evidence

Eight supplied screenshots cover native import/rendering, normal operation, PV stop/restart, CB1 opening and restoration, operator audit entries, communication loss and recovery. [The acceptance record](../evidence/live-overview/README.md) contains the images, values and limits. The latest exported Overview JSON exactly matches the reviewed live Overview.

The current export also contains the `/` homepage, all six views, the latest timer/modules, Operator permissions on nine write actions, and the ten-minute/two-second-refresh Trends configuration. Browser-session observations and screenshot review do not establish every authorization-bypass path or a clean-room Gateway restore.

## Packet evidence

The [36-second trace](../evidence/modbus/README.md) contains 335 packets and 80 complete Modbus request/response pairs. Both payload/sequence write pairs receive echoes and matching APPLIED feedback. Separate power/status polling confirms PV operation changes.

The final routine telemetry request occurs at the recording boundary without its response. There are no captured Modbus exceptions or TCP resets. These are observations of this file, not general reliability claims.

## Reproduction limits

The exact native project export and supplied tag JSON are preserved. Gateway-level resources and existing historian/journal/audit rows require separate local setup. The Python package combines the previously delivered source modules; it is not a new Windows source-directory archive. See [scope](scope-and-limitations.md) and [provenance](provenance.json).
