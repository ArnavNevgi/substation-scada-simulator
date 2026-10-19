"""Optional actual-TCP checks of the heartbeat-freeze demo on an isolated port.

Requires the installed Phase 3/4/5/7 project files and existing Python packages.
Never connects to Ignition's configured port or modifies project configuration.
"""

import asyncio
import copy
import json
from pathlib import Path
import socket
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.server import ModbusTcpServer

from modbus_points import load_points
from phase4_powerflow import load_config
from phase7_fault import calculate_fault_study
from phase7_modbus_server import control_loop, model_loop
from phase8_freeze_demo import HeartbeatFreezeBridge

CHECKS = []


def check(condition, label):
    CHECKS.append({"check": label, "passed": bool(condition)})
    print(("PASS: " if condition else "FAIL: ") + label, flush=True)
    if not condition:
        raise AssertionError(label)


async def run():
    config = copy.deepcopy(load_config(ROOT / "config/substation.json"))
    config["simulation"]["power_flow_interval_s"] = 0.2
    points = load_points(ROOT / "config/points.json")
    study = await asyncio.to_thread(calculate_fault_study, config)
    HeartbeatFreezeBridge.freeze_after_s = 5.0
    bridge = HeartbeatFreezeBridge(config, points, study)
    await bridge.update()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    server = ModbusTcpServer(context=bridge.device(), address=("127.0.0.1", port))
    client = AsyncModbusTcpClient("127.0.0.1", port=port, timeout=3, retries=0)
    tasks = []

    async def read_words():
        result = await client.read_holding_registers(0, count=23, device_id=1)
        if result.isError():
            raise AssertionError(str(result))
        return result.registers

    async def pair():
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            before = await read_words()
            result = await client.read_discrete_inputs(0, count=16, device_id=1)
            if result.isError():
                raise AssertionError(str(result))
            after = await read_words()
            if before == after:
                return after, result.bits
        raise AssertionError("Stable status read timed out")

    async def until(predicate):
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            words, bits = await pair()
            if predicate(words, bits):
                return words, bits
            await asyncio.sleep(0.05)
        raise AssertionError("Expected model state timed out")

    try:
        await server.serve_forever(background=True)
        tasks = [asyncio.create_task(control_loop(bridge)),
                 asyncio.create_task(model_loop(bridge, verbose=False))]
        check(await client.connect(), "TCP connects to the isolated freeze-demo server")
        initial, bits = await pair()
        check(initial[13] == 2000 and bits[0] and bits[4],
              "Normal telemetry and valid closed-breaker status remain compatible")
        moved, _ = await until(lambda w, b: w[1] != initial[1])
        check(moved[1] != initial[1], "Heartbeat advances before the optional freeze")
        frozen, _ = await until(lambda w, b: bridge.freeze_announced)
        frozen_hb = frozen[1]
        await asyncio.sleep(0.7)
        again, _ = await pair()
        check(again[1] == frozen_hb, "Heartbeat stays frozen across later model updates")
        result = await client.write_register(112, 300, device_id=1)
        check(not result.isError(), "Modbus setpoint writes still respond while heartbeat is frozen")
        cloudy, bits = await until(lambda w, b: w[13] == 617 and b[4])
        check(cloudy[1] == frozen_hb and cloudy[4] == 9433 and bits[0],
              "Other model telemetry changes to the cloudy scenario without heartbeat movement")
        protected = await client.write_register(0, 123, device_id=1)
        check(protected.isError(), "The freeze demo preserves read-only telemetry protection")
        result = await client.write_registers(100, [1, 1], device_id=1)
        check(not result.isError(), "The original command commit protocol remains available")
        opened, bits = await until(lambda w, b: w[19] == 2 and not b[0] and b[4])
        check(opened[1] == frozen_hb and opened[2] == 0 and opened[13] == 0 and opened[18] == 2,
              "Breaker opening still publishes valid isolated zeros with the intentionally frozen heartbeat")
    finally:
        client.close()
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await server.shutdown()


def main():
    error = None
    try:
        asyncio.run(run())
    except Exception as exc:
        error = repr(exc)
        print("VERIFICATION ERROR:", error)
    passed = sum(check["passed"] for check in CHECKS)
    success = error is None and passed == len(CHECKS)
    path = ROOT / "artifacts/phase8_tcp_verification.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"phase": 8, "scope": "Actual Modbus TCP freeze-demo checks; no live Ignition",
                               "passed": success, "passed_checks": passed, "total_checks": len(CHECKS),
                               "checks": CHECKS, "error": error}, indent=2) + "\n", encoding="utf-8")
    print("PHASE 8 TCP VERIFICATION: %s (%d/%d checks)" % ("PASS" if success else "FAIL", passed, len(CHECKS)))
    print("Report:", path)
    return 0 if success else 1


if __name__ == "__main__":
    raise SystemExit(main())
