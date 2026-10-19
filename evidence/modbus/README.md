# Modbus command-cycle packet evidence

**Result: STOP PV1 and START PV1 both pass the packet-level checks.**

The user captured the local Ignition/Python simulator connection with Wireshark. Analysis is read-only and reproducible from the included capture.

## Capture

| Item | Observed |
|---|---|
| File | `modbus_control_cycle.pcapng` |
| SHA-256 | `3153d5955603a819351ba8edacfe9f16b205de33d92956b0941722e4ddbd3854` |
| Size / frames | 36,268 bytes / 335 frames |
| First packet UTC | 2026-10-19T20:00:46.210254+00:00 |
| First packet India time | 20 October 2026, 01:30:46.210254 IST |
| First-to-last packet span | 36.0000147 s |
| Endpoint / unit | 127.0.0.1:1502 / unit 1 |
| Capture interface | Windows Npcap loopback, LINKTYPE_NULL |
| Timestamp units | Nanoseconds, read from the interface timestamp-resolution option |
| Modbus messages | 161: 81 requests, 80 responses |
| Complete request/response pairs | 80 |
| Modbus exceptions / TCP resets | 0 / 0 observed |
| Duplicate TCP payload segments | 0 observed |
| Interface drop counter | 0 reported by the capture statistics |

Three TCP client ports are present: the continuing tag-poll/write connection (64779), and two separate read-only command-feedback connections (60145, 60153). TCP ports distinguish the flows even though both endpoints use localhost.

## Command proof

Addresses below are zero-based wire addresses. FC06 writes one holding register; FC03 reads holding registers. The command and status meanings come from this project’s map.

| Command | Payload write and server echo | Sequence commit and server echo | Coherent feedback | Result |
|---|---|---|---|---|
| STOP PV1 | HR100 = 5; frames 176/178 | HR101 = 1; frames 180/182 | FC03 reads HR19–21; frames 184/186; `[2, 0, 1]` | APPLIED, reason None, matching sequence 1 |
| START PV1 | HR100 = 4; frames 247/249 | HR101 = 2; frames 251/253 | FC03 reads HR19–21; frames 255/257; `[2, 0, 2]` | APPLIED, reason None, matching sequence 2 |

For each command, the payload write is acknowledged before the sequence write is sent. The sequence is the project’s commit trigger. Each direct feedback response supplies status, reason, and acknowledged sequence together in one response. Both completed commands have `[status=2, reason=0, sequence=expected]`.

The FC06 echo establishes that the register write was acknowledged. The separate project feedback establishes the command result. No extra command-write requests appear in this capture.

## Actual operating feedback

The analog measurements and digital operating status confirm the requested change. These are separate polling responses, with their own frame timestamps.

| Phase | Holding-register response | PV power | Grid import | Bus voltage | T1 loading | PV-running bit |
|---|---|---|---|---|---|---|
| Before STOP | frame 3 | 2,000 kW | 8,056 kW | 13.61 kV | 42.1% | 1, frame 7 |
| After STOP | frame 194 | 0 kW | 10,053 kW | 13.60 kV | 51.9% | 0, frame 198 |
| After START / fresh valid solve | frame 273 | 2,000 kW | 8,056 kW | 13.61 kV | 42.1% | 1, frame 269 |

CB1 remains closed and the bus remains energized in all 36 captured digital-status samples. The heartbeat progresses from 7022 to 7051 over the holding-register samples.

After START, frame 265 reports model-state 0 with the previous analog values, followed by frame 273 reporting model-state 1 and restored PV power. APPLIED command feedback can therefore precede the fresh solved analog values. The diagram’s quality/model guards remain relevant during that interval.

## Observed timing

| Command | Sequence-commit request → first matching APPLIED response |
|---|---|
| STOP PV1 | 92.609 ms |
| START PV1 | 120.273 ms |

These are two timestamp differences from this local capture. They include the feedback polling/processing interval. They do not measure HMI click-to-display latency, electrical actuation time, or a performance bound. The capture timestamps have nanosecond units; that does not establish nanosecond clock accuracy.

## Capture boundary and scope

Frame 335, the last packet, is a routine FC03 request for HR0–116. Its response is outside the recorded interval. This is the only unmatched request; all four command writes and all four direct command-feedback reads have captured responses. The missing final response does not establish a failed transaction.

There are no captured Modbus exceptions, TCP resets, or repeated payload segments. This is a statement about this 36-second file; it is not a general packet-loss, reliability, or field-network claim.

Modbus payloads do not contain the Perspective actor or the audit RequestID. Those require the separate application audit. Sequence numbers here are 1 and 2 and should not be used as globally unique identifiers across simulator runs.

## Reproduce

Python 3.10 or newer, with no extra packages:

```text
python analyze_capture.py modbus_control_cycle.pcapng --output reproduced
python test_analyzer.py
```

The analyzer checks pcapng block lengths and timestamp resolution, parses localhost IPv4/TCP, reassembles contiguous TCP payloads, and pairs Modbus requests/responses by connection, transaction ID and unit. It checks function/byte counts and write echoes. Unsupported link/IP formats, TCP gaps, out-of-order or conflicting overlapping data fail explicitly. It does not validate transport checksums.

The included test suite passed 15 tests, covering known wire bytes, split/coalesced messages, retransmission deduplication, malformed records, matching rules, telemetry changes, and the supplied capture. It is a scoped analyzer for this project, not a general network dissector.

## Files

- `modbus_control_cycle.pcapng`: original user capture, unchanged.
- `results/analysis.json`: complete decoded observations and packet/frame references.
- `results/commands.csv`: command ordering and result evidence.
- `results/holding_telemetry.csv`, `results/discrete_telemetry.csv`: separately timestamped polling data.
- `results/modbus_messages.csv`: all decoded Modbus messages.
- `results/verification.json`: analyzer test result.
- `register_map.json`: relevant point addresses, scaling and project enum meanings.
- `analyze_capture.py`, `test_analyzer.py`: reproducible read-only analysis.
- `manifest.json`: source identities and hashes.

## Primary references

- Modbus Organization, application protocol and TCP/IP implementation specifications: https://www.modbus.org/modbus-specifications
- IETF pcapng format, September 2026 Internet-Draft (work in progress): https://datatracker.ietf.org/doc/html/draft-ietf-opsawg-pcapng-06

This evidence concerns the educational local simulator and its modeled states.
