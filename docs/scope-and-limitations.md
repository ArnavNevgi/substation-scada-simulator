# Scope and limitations

This is a local educational model and SCADA implementation using assumed network/equipment parameters. Its evidence is tied to the supplied code, export, screenshots and packet file.

- The electrical calculations are balanced steady-state snapshots. Switching waveforms, harmonics, electromagnetic transients, frequency dynamics, inverter PLL behavior and semiconductor switching are outside the implemented model.
- The PV plant is a static grid-following P/Q model with assumed efficiency and capability limits. Its temperature calculation is illustrative.
- The fault study uses external-grid contribution for a fixed radial configuration. Its independent check shares the stated equipment assumptions. It does not establish a validated manufacturer or utility model.
- The overcurrent pickup, relay delay and breaker delay are training settings executed by Python scheduling. They do not establish deterministic or protection-grade timing.
- CB1 is the implemented controllable breaker. Earlier exploratory CB2 concepts are not part of this model.
- The communications implementation demonstrates Modbus TCP and Ignition OPC tags. DNP3 and IEC 61850 are not implemented here.
- Simulator state, inputs, latches and sequence counters initialize on restart. The historian, alarm journal and audit persistence are separate Gateway resources.
- The native project ZIP excludes Gateway users, passwords, device/database connections, historian storage and stored event rows. A new Gateway needs the named setup dependencies. A complete historian database restore has not been verified.
- Historical pen paths contain the original Gateway name `Ignition-Nevgi`; reselect local historical sources when that name differs. The chart's mode uses an omitted native default in this export; verify realtime during a new installation.
- The imported tag JSON is the supplied 19 October snapshot; the project ZIP is the newer 20 October snapshot. Their static references are checked together. A project export itself contains no tag-provider export.
- The packaged Python files come from the previously delivered source packages. They have been rerun with the current exported Ignition helpers, but were not obtained as a fresh byte-for-byte archive of the Windows working directory.
- Some preserved configuration/tag descriptions retain earlier design wording. In particular, the current engine retains `LastFault_Ikss_kA` across RESET TRIPS; it initializes to zero on process restart. Its older point description mentions reset. The implemented engine and its checks define the current behavior.
- Screenshot review proves displayed states. It does not measure control timing, UI latency, every security bypass path, or all historian persistence cases.
- The packet capture proves the included local STOP/START cycle and observed polling values. Its final routine poll ends at the recording boundary. Its two timing samples are not a throughput/reliability benchmark.
- The offline audit/health suites use substituted Ignition and Java APIs. Native runtime acceptance is documented separately rather than inferred from those substitutes.

The working database alarm journal is retained under its configured name `Sample_Alarm_Journal_Profile`. The unused Internal journal named `SubstationAlarmJournal` had unresolved query lookup behavior and is not claimed as an alternate working configuration.

Source origins and unchanged export identifiers are recorded in [provenance](provenance.json). The [verification report](verification.md) separates fresh local checks from the user-provided runtime observations.
