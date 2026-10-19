"""Local TCP integration checks. The simulator under test uses an isolated port."""

import asyncio
from pathlib import Path
import socket
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from pymodbus.client import AsyncModbusTcpClient
from pymodbus.constants import ExcCodes
from pymodbus.server import ModbusTcpServer

from modbus_points import decode_word, encode_word, load_points
from phase4_powerflow import load_config, run_powerflow
from phase5_modbus_server import TelemetryBridge


async def main():
    config = load_config(ROOT / "config/substation.json")
    points = load_points(ROOT / "config/points.json")
    bridge = TelemetryBridge(config, points)
    unit = config["modbus"]["device_id"]
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = ModbusTcpServer(context=bridge.device(), address=("127.0.0.1", port))
    client = AsyncModbusTcpClient("127.0.0.1", port=port, retries=0, timeout=3)
    checks = []

    def check(name, condition):
        if not condition:
            raise AssertionError(name)
        checks.append(name)
        print("PASS:", name, flush=True)

    def ok(response):
        if response.isError():
            raise AssertionError(str(response))
        return response

    async def words(address=0, count=23):
        return ok(await client.read_holding_registers(address, count=count, device_id=unit)).registers

    async def bits():
        return ok(await client.read_discrete_inputs(0, count=16, device_id=unit)).bits[:16]

    async def write(name, value):
        p = points[name]
        ok(await client.write_register(p["offset_zero_based"], encode_word(p, value), device_id=unit))

    async def model_loop():
        while True:
            await asyncio.sleep(0.3)
            await bridge.update()

    task = None
    try:
        check("First electrical snapshot is valid", await bridge.update())
        await server.serve_forever(background=True)
        check("TCP connection", await client.connect())
        normal = await words()
        status = await bits()
        check("Voltage scaling and known normal electrical values",
              normal[0] == 11493 and normal[2] == 1361
              and normal[4] == 8056 and normal[13] == 2000)
        check("Discrete inputs use their own bit area",
              status[:9] == [True, False, True, True, True, True, True, False, False])
        check("Reserved DI values are false", status[9:] == [False] * 7)
        grouped = await words(0, 125)
        tail = await words(125, 3)
        check("Entire HR allocation has no grouped-read holes", len(grouped + tail) == 128)
        check("Reserved HR values read zero", grouped[23:100] == [0] * 77 and tail == [0] * 3)
        response = await client.read_holding_registers(128, count=1, device_id=unit)
        check("Out-of-range HR read rejected", response.isError())

        before = normal[1]
        task = asyncio.create_task(model_loop())
        # No polling during this wait; progress is independent of client reads.
        await asyncio.sleep(1.3)
        after = (await words())[1]
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        task = None
        check("Heartbeat advances without polling", after != before)

        response = await client.write_register(0, 123, device_id=unit)
        check("Telemetry writes rejected", response.isError() and response.exception_code == ExcCodes.ILLEGAL_ADDRESS)
        response = await client.write_register(50, 123, device_id=unit)
        check("Reserved HR writes rejected", response.isError())
        response = await client.write_register(100, 1, device_id=unit)
        check("Future command writes rejected", response.isError())
        response = await client.write_register(115, 1, device_id=unit)
        check("Future fault-enable writes rejected", response.isError())
        response = await client.write_register(112, 1001, device_id=unit)
        check("Out-of-range setpoint rejected and previous value retained",
              response.isError() and (await words(112, 1))[0] == 1000)
        before_inputs = await words(110, 2)
        response = await client.write_registers(110, [12000, 6000], device_id=unit)
        check("Invalid multi-register write makes no partial changes",
              response.isError() and await words(110, 2) == before_inputs)
        ok(await client.write_registers(110, [10000, 2000, 1000, 2000, 0], device_id=unit))
        check("Valid multi-register setpoint write accepted",
              await words(110, 5) == [10000, 2000, 1000, 2000, 0])

        await write("Irradiance_Set_W_m2", 300)
        await bridge.update()
        cloudy = await words()
        check("Cloud input changes solved telemetry", cloudy[13] == 617 and cloudy[4] == 9433)
        await write("Irradiance_Set_W_m2", 1000)
        await write("PV_Q_Set_kVAr", -1000)
        await bridge.update()
        absorption = await words()
        check("Signed reactive telemetry decodes correctly",
              absorption[14] == 64536 and decode_word(points["PV_Q_kVAr"], absorption[14]) == -1000)
        check("Reactive absorption increases grid Q import", absorption[5] > normal[5])
        await write("PV_Q_Set_kVAr", 2500)
        await bridge.update()
        limited = await words()
        check("Explicit P/Q capability limit", limited[13] == 2000 and limited[14] == 1500)
        await write("PV_Q_Set_kVAr", 0)
        await write("Load_P_Set_kW", 500)
        await write("Load_Q_Set_kVAr", 0)
        await bridge.update()
        exported = await words()
        check("Negative grid power survives unsigned wire encoding",
              exported[4] > 32767 and decode_word(points["Grid_P_kW"], exported[4]) == -1463)

        await write("Remote_Mode_Set", 0)
        check("Local/remote input has separate status feedback", (await bits())[6] is False)
        await write("Remote_Mode_Set", 1)
        bridge.breaker_closed = False  # Test known isolation, not a public command.
        await bridge.update()
        opened = await words()
        opened_bits = await bits()
        check("Known isolation publishes valid zeros and preserves upstream voltage",
              opened[0] > 0 and opened[2] == opened[3] == opened[6] == opened[13] == 0
              and opened_bits[4] and not opened_bits[5] and not opened_bits[0])
        check("DC voltage remains present in sunlight while isolated",
              opened[11] == 1000 and opened[12] == 0)
        bridge.breaker_closed = True
        await bridge.update()
        good = await words()

        def failing_solver(_config, _point):
            raise ValueError("Deliberate integration-test solver failure")

        bridge.solver = failing_solver
        check("Handled electrical solve failure", not await bridge.update())
        failed = await words()
        failed_bits = await bits()
        analog_indices = [i for i in range(18) if i != 1]
        check("Failed solve retains all last-valid analog measurements",
              all(failed[i] == good[i] for i in analog_indices))
        check("Communications remain responsive; validity clears and heartbeat advances",
              failed[18] == 4 and not failed_bits[4] and failed[1] != good[1])
        bridge.solver = run_powerflow
        check("Valid state recovers after solver recovery",
              await bridge.update() and (await bits())[4])
        bridge.heartbeat = 65535
        await bridge.update()
        check("Heartbeat wraps as unsigned 16-bit", (await words())[1] == 0)
        try:
            encode_word(points["Grid_P_kW"], 40000)
        except ValueError:
            check("Analog overflow is rejected instead of wrapping", True)
        else:
            raise AssertionError("Overflow was not rejected")
        print(f"\nPHASE 5 TCP VERIFICATION: PASS ({len(checks)} checks)", flush=True)
    finally:
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        client.close()
        await server.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
