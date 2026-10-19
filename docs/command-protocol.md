# Command protocol and audit

The project uses a staged payload and sequence-commit protocol over zero-based holding-register addresses. The numeric command/status meanings are application-specific.

| Address | Meaning |
|---|---|
| HR100 | Staged command code |
| HR101 | New sequence value commits the staged command |
| HR19 | Command status: 0 IDLE, 1 PENDING, 2 APPLIED, 3 REJECTED, 4 FAILED |
| HR20 | Reason: 0 None, 1 Local mode, 2 Fault active, 3 Trip latched, 4 Grid/bus unavailable, 5 Model invalid, 6 Busy, 7 Invalid command, 8 Out of range, 9 Other interlock |
| HR21 | Acknowledged command sequence |

| Code | Command |
|---:|---|
| 1 | OPEN CB1 |
| 2 | CLOSE CB1 |
| 3 | RESET TRIPS |
| 4 | START PV1 / apply PV enable |
| 5 | STOP PV1 |

## Submission and outcome

The current Project Library helper is `phase7_controls`, version 11.1. Each equipment button passes its Perspective component to `send_command(code, self)` so the helper can record session identity.

1. Acquire the submission lock and reject another active/unresolved command.
2. Check command-tag quality and perform a direct read-only FC03 preflight of HR19–21.
3. Allocate a new 16-bit sequence and UUID RequestID; record REQUESTED before sending.
4. Write the command code through the existing Ignition tag, then write the sequence tag last.
5. Poll one FC03 response containing HR19–21 together. Accept a terminal result only when the acknowledged sequence matches this request.
6. Record the outcome with the same RequestID and sequence, then release the active request.

The request timeout is 12 s, the feedback socket timeout is 1 s, and the poll interval is 200 ms. These are application settings. A write echo indicates a register operation was acknowledged; the later project status indicates the command result.

The helper validates MBAP transaction/protocol/length/unit and response function/byte count. It handles fragmented TCP responses. A matching APPLIED result also requires reason zero.

## Uncertain outcomes

A commit may reach the simulator even if its write call reports uncertainty. The helper observes feedback rather than resending. Missing/mismatched terminal feedback produces UNKNOWN and retains an unresolved-command guard. Read-only `reconcile_pending()` can later record a matching terminal result; it never resends the equipment command.

The engine treats a duplicate most-recent sequence idempotently. A different sequence while an operation is pending is not queued. The intended operating model is one command writer at a time. Sequences wrap and can reset across process runs; the UUID links application audit records across those boundaries.

APPLIED may represent an already-satisfied request. For START PV1 it means enable was applied; actual running and power depend on bus availability, irradiance, limits and a fresh model solution. Position and power feedback remain separate observations.

## Audit records

The helper uses the Local `SubstationAudit` profile. It records REQUESTED and APPLIED/REJECTED/FAILED, along with NOT_SENT, WRITE_UNCERTAIN, UNKNOWN and reconciliation outcomes where relevant. Fields include actor, target, code, sequence, UUID and reason.

The `CommandHistory` table queries the newest 200 explicit command events within the last 24 hours, refreshed through a two-second expression/transform binding. Automatic project tag-write records remain in the underlying audit profile and are excluded from this command-only table. The Ignition 8.3.10 Local profile's observed `Timestamp` column is supported alongside normalized timestamp aliases.

The four simulation-input buttons use `set_simulation()` and automatic project auditing. They are not included as equipment COMMAND events by `history_rows()`.

## Packet evidence

The [recorded command cycle](../evidence/modbus/README.md) contains four FC06 write requests and their echoes: payload then sequence for STOP, followed by payload then sequence for START. Direct FC03 feedback returns `[2, 0, 1]` and `[2, 0, 2]`. Independent polling shows the matching PV-running and power changes.

The capture's 92.609 ms and 120.273 ms commit-to-observed-feedback intervals are two local samples, not HMI latency or a performance guarantee.

Primary protocol definitions: [Modbus Organization specifications](https://www.modbus.org/modbus-specifications).
