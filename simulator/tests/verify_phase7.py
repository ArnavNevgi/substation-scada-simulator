"""Phase 7 checks: electrical reference, deterministic timing, actual TCP and races.

Uses its own localhost port, never the configured Ignition port.
The seven legacy pre-audit helper checks were superseded by verify_phase11.py,
which tests the current exported command helper in 34 separate cases.
Writes artifacts/phase7_verification.json. Requires only the existing packages.
"""

import asyncio
from dataclasses import replace
from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import platform
import socket
import sys
import threading
import time
import types

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pandapower
import pymodbus
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.server import ModbusTcpServer

from modbus_points import load_points
from phase4_powerflow import load_config, operating_point, run_powerflow
from phase7_control import ControlEngine
from phase7_fault import calculate_fault_study
from phase7_modbus_server import ControlBridge, control_loop, model_loop

CHECKS = []


def check(name, passed, details=None):
    item = {"name": name, "passed": bool(passed)}
    if not passed and details is not None:
        item["details"] = details
    CHECKS.append(item)
    print(("PASS: " if passed else "FAIL: ") + name, flush=True)
    if not passed:
        if details is not None:
            print("Observed:", json.dumps(details), flush=True)
        raise AssertionError(name)


async def read_pair(read_words, read_bits, timeout=10):
    """Bracket DI with complete HR reads; discard a crossed publication.

    HR and DI are separate Modbus requests. A model publication changes the
    heartbeat, and a breaker/command transition changes the diagnostics even
    before the next solve. Comparing the whole HR block catches both cases.
    """
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        before = await read_words()
        status = await read_bits()
        after = await read_words()
        if before == after:
            return after, status
        await asyncio.sleep(0)
    raise AssertionError("Could not read stable HR/DI feedback before timeout.")


async def reader_regression_checks():
    async def crossed_read(old, new, status):
        responses = iter((old, new, new, new))
        reads = 0

        async def read_words():
            nonlocal reads
            reads += 1
            return next(responses)

        async def read_bits():
            return status

        words, bits = await read_pair(read_words, read_bits)
        return words, bits, reads

    before = [0] * 23
    before[1], before[2], before[18], before[19] = 1, 1361, 1, 1
    after = before.copy()
    after[1], after[2], after[18], after[19] = 2, 0, 2, 2
    opened = [False] * 16
    opened[4] = True
    w, b, reads = await crossed_read(before, after, opened)
    check("Reader retries HR/DI responses that straddle breaker opening and a solve",
          reads == 4 and w[1] == 2 and w[2] == 0 and w[19] == 2 and not b[0] and b[4])

    # A control transition can occur before a new model heartbeat is published.
    after = before.copy()
    after[18], after[19] = 0, 2
    opened[4] = False
    w, b, reads = await crossed_read(before, after, opened)
    check("Reader also retries changed diagnostics when heartbeat has not advanced",
          reads == 4 and w[1] == before[1] and w[18] == 0 and w[19] == 2
          and not b[0] and not b[4])


def engine_checks(config, study, normal, opened, stopped):
    def fresh():
        e = ControlEngine(config, study)
        e.accept_snapshot(normal, e.revision)
        return e

    d = config["breaker"]["operation_delay_s"]
    delay = config["protection"]["trip_delay_s"]
    e = fresh()
    e.submit(1, 1, 0)
    e.tick(d - 0.001)
    check("Open remains pending until its breaker movement delay", e.command_status == 1 and e.breaker_closed)
    e.tick(d + 0.001)
    check("Manual open completes on position feedback and trips grid-following PV",
          not e.breaker_closed and e.command_status == 2 and e.pv_tripped and not e.trip_latched)
    revision = e.revision
    e.submit(2, 1, 2)
    e.tick(3)
    check("Duplicate sequence does not execute a different staged command",
          not e.breaker_closed and e.revision == revision and e.command_status == 2)

    e = fresh()
    e.environment(fault=True)
    e.tick(0)
    e.tick(delay - 0.001)
    check("Fault immediately trips PV but does not bypass the OC delay",
          e.pv_tripped and not e.pv_enabled and not e.trip_latched and e.breaker_closed)
    e.tick(delay + 0.001)
    check("OC delay operates the relay before the breaker physically opens",
          e.trip_latched and e.breaker_closed and e.movement is not None)
    e.tick(delay + d + 0.002)
    check("Breaker opens after the additional movement delay", not e.breaker_closed)
    e.submit(3, 1, 3)
    check("Reset is rejected while fault remains", (e.command_status, e.command_reason) == (3, 2))
    e.submit(2, 2, 3)
    check("Closing onto the enabled fault is rejected", (e.command_status, e.command_reason) == (3, 2))
    e.environment(fault=False)
    e.accept_snapshot(opened, e.revision)
    check("Removing the fault retains both trip latches and last-event current",
          e.trip_latched and e.pv_tripped and e.last_fault_ikss_ka == study["bus_ikss_ka"])
    e.submit(2, 3, 4)
    check("Trip latch blocks close after fault removal", (e.command_status, e.command_reason) == (3, 3))
    e.submit(3, 4, 4)
    check("Reset clears latches without closing CB1 or enabling PV",
          e.command_status == 2 and not e.trip_latched and not e.pv_tripped
          and not e.breaker_closed and not e.pv_enabled)
    e.accept_snapshot(opened, e.revision)
    e.submit(2, 5, 5)
    e.tick(5 + d + 0.001)
    check("Close restores the breaker position without automatic PV restart",
          e.breaker_closed and not e.pv_enabled and e.command_status == 2)
    e.accept_snapshot(stopped, e.revision)
    e.submit(4, 6, 6)
    check("Separate PV enable succeeds after grid restoration", e.pv_enabled and e.command_status == 2)

    e = fresh()
    e.environment(remote=False)
    e.submit(1, 1, 0)
    check("Local mode rejects an operator open command", (e.command_status, e.command_reason) == (3, 1) and e.breaker_closed)
    e.environment(fault=True)
    e.tick(0)
    e.tick(delay + 0.001)
    e.tick(delay + d + 0.002)
    check("Automatic protection still operates in local mode", e.trip_latched and not e.breaker_closed)

    e = fresh()
    e.submit(1, 1, 0)
    e.tick(d + 0.001)
    e.accept_snapshot(opened, e.revision)
    e.submit(2, 2, 1)
    e.environment(fault=True)
    e.tick(1.1)
    check("A fault cancels a pending close before movement completes",
          not e.breaker_closed and e.movement is None
          and (e.command_status, e.command_reason) == (4, 2))

    e = fresh()
    e.environment(fault=True)
    e.tick(0)
    e.tick(0.5)
    e.environment(fault=False)
    e.tick(0.6)
    e.accept_snapshot(stopped, e.revision)
    e.environment(fault=True)
    e.tick(0.7)
    e.tick(0.7 + delay - 0.001)
    check("Clearing a short fault resets the definite-time pickup timer", not e.trip_latched)
    e.tick(0.7 + delay + 0.001)
    check("A subsequent sustained fault must satisfy its own full delay", e.trip_latched)

    e = fresh()
    e.invalidate()
    e.submit(2, 1, 0)
    check("Invalid electrical model blocks close", (e.command_status, e.command_reason) == (3, 5))
    e.submit(4, 2, 0)
    check("Invalid electrical model blocks PV start", (e.command_status, e.command_reason) == (3, 5))
    e.submit(5, 3, 0)
    e.submit(1, 4, 0)
    e.tick(d + 0.001)
    check("Remote stop and open remain available with invalid model", not e.pv_enabled and not e.breaker_closed)
    e = fresh()
    e.submit(99, 1, 0)
    check("Unknown command receives an acknowledged rejection", (e.command_status, e.command_reason, e.ack_sequence) == (3, 7, 1))
    e.last_sequence = 65535
    e.submit(5, 0, 0)
    check("Sequence rollover from 65535 to 0 works", e.ack_sequence == 0 and e.command_status == 2 and not e.pv_enabled)


async def tcp_checks(config, points, study):
    bridge = ControlBridge(config, points, study)
    unit = config["modbus"]["device_id"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ModbusTcpServer(context=bridge.device(), address=("127.0.0.1", port))
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=3, retries=0)
    tasks = []
    release = threading.Event()

    def ok(response):
        if response.isError():
            raise AssertionError(str(response))
        return response

    async def words(address=0, count=23):
        return ok(await client.read_holding_registers(address, count=count, device_id=unit)).registers

    async def bits():
        return ok(await client.read_discrete_inputs(0, count=16, device_id=unit)).bits[:16]

    async def write(address, value):
        ok(await client.write_register(address, value, device_id=unit))

    async def pair():
        return await read_pair(words, bits)

    async def until(predicate, timeout=10):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            w, b = await read_pair(words, bits, timeout=max(0.01, end - time.monotonic()))
            if predicate(w, b):
                return w, b
            await asyncio.sleep(0.03)
        raise AssertionError("State timed out: " + str((await words())[18:23]) + " " + str(await bits()))

    async def send(code, expected=2, reason=0):
        seq = ((await words(101, 1))[0] + 1) % 65536
        ok(await client.write_registers(100, [code, seq], device_id=unit))
        w, b = await until(lambda w, b: w[21] == seq and w[19] != 1)
        if (w[19], w[20]) != (expected, reason):
            raise AssertionError(f"Unexpected result for command {code}: {w[19:22]}")
        if expected == 2:
            return await until(lambda w, b: b[4])
        return w, b

    try:
        check("Initial Phase 7 electrical publication is valid", await bridge.update())
        await server.serve_forever(background=True)
        check("TCP connects to an isolated test server", await client.connect())
        tasks = [asyncio.create_task(control_loop(bridge)), asyncio.create_task(model_loop(bridge, verbose=False))]
        w, b = await pair()
        check("Existing normal telemetry and all status bits remain compatible",
              w[4] == 8056 and w[13] == 2000 and w[2] == 1361
              and b[:9] == [True, False, True, True, True, True, True, False, False])
        grouped, tail = await words(0, 125), await words(125, 3)
        check("Grouped reads retain the complete allocation and reserved zeros",
              len(grouped + tail) == 128 and grouped[23:100] == [0] * 77
              and tail == [0] * 3 and b[9:] == [False] * 7)
        response = await client.write_register(0, 0, device_id=unit)
        response2 = await client.write_register(50, 1, device_id=unit)
        check("Telemetry and reserved addresses remain protected", response.isError() and response2.isError())
        before = await words(110, 7)
        response = await client.write_registers(110, [12000, 2000, 1000, 2000, 0, 2, 1], device_id=unit)
        check("Invalid fault input rejects the entire multi-register write",
              response.isError() and await words(110, 7) == before)

        ok(await client.write_registers(100, [1, 1], device_id=unit))
        w, b = await pair()
        check("Atomic command commit exposes pending acknowledgement before movement",
              w[19] == 1 and w[21] == 1 and b[0])
        response = await client.write_registers(100, [2, 2], device_id=unit)
        check("A second pending command is rejected without overwriting its feedback",
              response.isError() and (await words())[19:22] == [1, 0, 1])
        ok(await client.write_registers(100, [1, 1], device_id=unit))
        w, b = await until(lambda w, b: not b[0] and b[4])
        check("Duplicate pending retry is harmless; open publishes valid downstream zeros",
              w[19] == 2 and w[0] > 0 and w[2] == w[3] == w[6] == w[13] == 0
              and b[8] and not b[1] and not b[5], {"holding_registers": w, "discrete_inputs": b})
        await send(3)
        w, b = await send(2)
        check("Reset then close restores the bus with PV still stopped", b[0] and b[5] and not b[2] and w[13] == 0)
        w, b = await send(4)
        check("PV start restores the original operating point", b[2] and w[13] == 2000 and w[4] == 8056)

        await write(100, 5)
        check("Writing only the payload does not operate equipment", (await bits())[2])
        sequence = ((await words(101, 1))[0] + 1) % 65536
        await write(101, sequence)
        w, b = await until(lambda w, b: b[4] and not b[2] and w[21] == sequence)
        check("Separate FC6 code then sequence writes operate correctly", w[19] == 2 and w[13] == 0 and w[4] == 10053)
        await write(100, 4)
        await write(101, sequence)
        check("Repeated completed sequence does not restart PV", not (await bits())[2])
        await write(116, 0)
        w, b = await send(4, 3, 1)
        check("Local-mode rejection is visible through Modbus diagnostics", not b[6] and not b[2])
        await write(116, 1)
        await send(4)

        await write(112, 300)
        w, b = await until(lambda w, b: b[4] and w[13] == 617)
        check("Existing cloudy scenario still changes HMI telemetry", w[4] == 9433 and w[8] == 488)
        await write(112, 1000)
        await write(114, 64536)  # signed -1000 kVAr
        w, b = await until(lambda w, b: b[4] and w[14] == 64536)
        check("Signed reactive setpoint/telemetry remains intact", w[5] > 2200)
        await write(114, 0)
        await until(lambda w, b: b[4] and w[14] == 0)
        # Stop scheduled publications while establishing the exact last-valid
        # baseline, then explicitly attempt the fault-suspended update. This
        # checks analog holding without racing an ordinary thermal update.
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        before = await words()
        await write(115, 1)
        suspended = await bridge.update()
        during, status = await pair()
        analogs = [i for i in range(18) if i != 1]
        check("Energized fault holds ordinary analogs and marks normal power flow invalid",
              not status[4] and status[7] and during[18] == 3
              and all(during[i] == before[i] for i in analogs))
        check("Fault-suspended attempt advances heartbeat without publishing normal power flow",
              not suspended and during[1] != before[1])
        tasks[1] = asyncio.create_task(model_loop(bridge, verbose=False))
        w, b = await until(lambda w, b: b[1] and not b[0] and b[4])
        check("Sustained fault trips CB1 and PV, retaining the fault while isolated",
              b[8] and b[7] and not b[2] and not b[5] and w[18] == 3)
        check("Post-trip analogs represent known isolation; last fault is retained separately",
              w[2] == w[3] == w[6] == w[13] == 0 and w[0] > 0 and w[22] == 7445)
        await send(2, 3, 2)
        await send(3, 3, 2)
        check("Fault-active close and reset interlocks work over TCP", True)
        await write(115, 0)
        await until(lambda w, b: b[4] and not b[7])
        w, b = await send(2, 3, 3)
        check("Fault removal does not bypass the breaker trip latch", b[1] and b[8] and not b[0])
        w, b = await send(3)
        check("Reset clears latches without restoring supply", not b[1] and not b[8] and not b[0] and not b[2])
        w, b = await send(2)
        check("Breaker close does not automatically restart PV", b[0] and b[5] and not b[2])
        w, b = await send(4)
        check("Separate PV start completes full fault recovery", w[13] == 2000 and w[4] == 8056 and b[2])
        heartbeat = w[1]
        await asyncio.sleep(2 * bridge.interval_s + 0.2)
        w = await words()
        check("Heartbeat advances without polling and fault-event current remains retained",
              w[1] != heartbeat and w[22] == 7445)

        # Pause only the scheduled electrical worker; keep the real control task.
        tasks[1].cancel()
        await asyncio.gather(tasks[1], return_exceptions=True)
        good = await words()

        def failing_solver(_config, _point):
            raise ValueError("Deliberate Phase 7 test failure")

        bridge.solver = failing_solver
        check("A solver failure is handled", not await bridge.update())
        w, b = await pair()
        check("Failed solve holds analogs, advances heartbeat and clears validity",
              not b[4] and w[18] == 4 and w[1] != good[1]
              and all(w[i] == good[i] for i in analogs))
        await send(2, 3, 5)
        await send(4, 3, 5)
        check("Invalid-model close/start interlocks work over TCP", True)
        bridge.solver = run_powerflow
        check("A fresh successful solve recovers validity", await bridge.update())

        # A slow worker must not block control, nor later overwrite an open
        # breaker with a stale closed-network electrical snapshot.
        started = threading.Event()

        def delayed_solver(c, point):
            started.set()
            if not release.wait(6):
                raise ValueError("Test synchronization timed out")
            return run_powerflow(c, point)

        bridge.solver = delayed_solver
        worker = asyncio.create_task(bridge.update())
        check("Slow electrical worker started", await asyncio.to_thread(started.wait, 3))
        seq = ((await words(101, 1))[0] + 1) % 65536
        ok(await client.write_registers(100, [1, seq], device_id=unit))
        w, b = await until(lambda w, b: not b[0] and w[19] == 2, timeout=3)
        check("Breaker control and Modbus remain responsive while solver is blocked", not b[4] and not release.is_set())
        held = await words()
        release.set()
        check("Obsolete electrical worker result is discarded", not await worker)
        w, b = await pair()
        check("Stale solve cannot mark old energized measurements valid after opening",
              not b[0] and not b[4] and all(w[i] == held[i] for i in analogs))
        bridge.solver = run_powerflow
        check("A new solve for current topology publishes valid isolation",
              await bridge.update() and (await words())[2] == 0 and (await bits())[4])
    finally:
        release.set()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        client.close()
        await server.shutdown()


def main():
    # Failure injection is intentional; the named PASS checks verify its effect.
    logging.getLogger("substation.phase7").setLevel(logging.CRITICAL)
    error = None
    study = None
    try:
        config = load_config(ROOT / "config/substation.json")
        points = load_points(ROOT / "config/points.json")
        study = calculate_fault_study(config)
        check("IEC 60909 result agrees with independent impedance calculation",
              study["relative_error"] < 1e-6 and math.isclose(study["bus_ikss_ka"], 7.4451769647, rel_tol=1e-6))
        check("HV-referred fault current exceeds the configured 200 A pickup",
              math.isclose(study["cb1_hv_current_a"], 893.4212358, rel_tol=1e-6)
              and study["cb1_hv_current_a"] > config["protection"]["overcurrent_pickup_hv_a"])
        point = operating_point(config, "normal")
        normal = run_powerflow(config, point)
        opened = run_powerflow(config, replace(point, breaker_closed=False, pv_enabled=False))
        stopped = run_powerflow(config, replace(point, pv_enabled=False))
        engine_checks(config, study, normal, opened, stopped)
        asyncio.run(reader_regression_checks())
        asyncio.run(tcp_checks(config, points, study))
    except Exception as exc:
        error = str(exc)
        print("VERIFICATION ERROR:", error, flush=True)
    finally:
        report = {
            "phase": 7, "passed": error is None,
            "time_utc": datetime.now(timezone.utc).isoformat(),
            "python": platform.python_version(), "pandapower": pandapower.__version__,
            "pymodbus": pymodbus.__version__, "fault_study": study,
            "passed_checks": sum(x["passed"] for x in CHECKS),
            "total_checks": len(CHECKS), "checks": CHECKS, "error": error,
            "scope": "Local Python/Modbus checks; actual Ignition UI still requires the Windows walkthrough.",
        }
        path = ROOT / "artifacts/phase7_verification.json"
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"\nPHASE 7 VERIFICATION: {'PASS' if error is None else 'FAIL'} "
              f"({report['passed_checks']}/{report['total_checks']} checks)", flush=True)
        print("Report:", path, flush=True)
    return 0 if error is None else 1


if __name__ == "__main__":
    raise SystemExit(main())
