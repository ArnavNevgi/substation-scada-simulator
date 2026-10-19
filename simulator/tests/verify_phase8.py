"""Offline checks of the ACTUAL monitor module using small Ignition/Java fakes.

No live Gateway, Modbus connection, tag edits, or equipment commands are used.
Ignition's expression parser and alarm engine still require the live HMI tests.
"""

from __future__ import annotations

import ast
import asyncio
from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
import re
import sys
import types
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
CHECKS = []


def check(condition, label):
    CHECKS.append({"check": label, "passed": bool(condition)})
    print(("PASS: " if condition else "FAIL: ") + label)
    if not condition:
        raise AssertionError(label)


class Quality:
    def __init__(self, good=True):
        self.good = good

    def isGood(self):
        return self.good


class Qualified:
    def __init__(self, value, good=True):
        self.value = value
        self.quality = Quality(good)
        self.timestamp = 0


class ConfigEnum:
    """Java-style config value: canonical name differs from display text."""
    def __init__(self, name, display):
        self.canonical_name = name
        self.display = display

    def name(self):
        return self.canonical_name

    def __str__(self):
        return self.display


class Clock:
    def __init__(self):
        self.ns = 0
        self.wall_ms = 1792339200000

    def at(self, seconds):
        delta = int(seconds * 1e9) - self.ns
        self.ns += delta
        self.wall_ms += delta / 1e6


class Lock:
    def __init__(self):
        self.busy = False

    def tryLock(self):
        if self.busy:
            return False
        self.busy = True
        return True

    def unlock(self):
        self.busy = False


class FakeTags:
    def __init__(self, clock):
        self.clock = clock
        self.configs = {}
        self.values = {}
        self.bad_paths = set()
        self.read_error = False
        self.write_fail = False
        self.configure_fail = False
        self.enum_style = None
        self.force_mode = None
        self.force_binding = False
        self.writes = []
        self.edits = []

    def exists(self, path):
        return path in self.configs

    def getConfiguration(self, path, recursive=False):
        config = copy.deepcopy(self.configs[path])
        displays = {"WhenFalse": "When False", "WhenTrue": "When True",
                    "AboveValue": "Above Setpoint", "BelowValue": "Below Setpoint",
                    "Float8": "Double", "DateTime": "Date Time"}

        def render(value):
            canonical = value.name() if hasattr(value, "name") else str(value)
            display = displays.get(canonical, canonical)
            if self.enum_style == "objects":
                return ConfigEnum(canonical, display)
            return display

        if self.enum_style:
            for key in ("tagType", "dataType"):
                if key in config:
                    config[key] = render(config[key])
            for alarm in config.get("alarms", []):
                for key in ("mode", "priority", "ackMode"):
                    if key in alarm:
                        alarm[key] = render(alarm[key])
        for alarm in config.get("alarms", []):
            if alarm.get("name") == "P8_MonitorStopped" and self.force_mode:
                alarm["mode"] = ConfigEnum(self.force_mode, displays[self.force_mode])
            if alarm.get("name") == "P8_T1Overload" and self.force_binding:
                alarm["enabled"]["value"] = "false"
        return [config]

    def configure(self, parent, configs, policy):
        results = []
        for definition in configs:
            path = parent + ("" if parent.endswith("]") else "/") + definition["name"]
            self.edits.append((path, copy.deepcopy(definition), policy))
            if self.configure_fail or (policy == "a" and path in self.configs):
                results.append(Quality(False))
                continue
            old = self.configs.get(path, {})
            self.configs[path] = dict(old, **copy.deepcopy(definition))
            if "value" in definition:
                self.values[path] = definition["value"]
            results.append(Quality())
        return results

    def evaluate(self, path):
        if self.configs.get(path, {}).get("valueSource") != "expr":
            return self.values.get(path)
        expr = self.configs[path]["expression"]
        expr = re.sub(r"\{([^{}]+)\}", lambda match: repr(self.evaluate(match[1])), expr)
        expr = expr.replace("&&", " and ").replace("||", " or ")
        expr = re.sub(r"!(?!=)", " not ", expr)
        expr = expr.replace("if(", "iif(")
        return eval(expr.strip(), {"__builtins__": {},
                                   "toMillis": lambda value: value,
                                   "now": lambda interval: self.clock.wall_ms,
                                   "iif": lambda condition, yes, no: yes if condition else no})

    def readBlocking(self, paths, timeout):
        if self.read_error:
            raise IOError("simulated read timeout")
        return [Qualified(self.evaluate(path), path not in self.bad_paths) for path in paths]

    def writeBlocking(self, paths, values, timeout):
        self.writes.append((list(paths), list(values)))
        if self.write_fail:
            return [Quality(False) for _ in paths]
        for path, value in zip(paths, values):
            self.values[path] = value
        return [Quality() for _ in paths]


class Harness:
    def __init__(self):
        self.clock = Clock()
        self.tags = FakeTags(self.clock)
        self.logs = []
        fake_system = types.SimpleNamespace(
            tag=self.tags,
            date=types.SimpleNamespace(now=lambda: self.clock.wall_ms, fromMillis=lambda n: n),
            util=types.SimpleNamespace(getLogger=lambda name: types.SimpleNamespace(error=self.logs.append)))
        modules = {name: types.ModuleType(name) for name in
                   ("java", "java.lang", "java.util", "java.util.concurrent", "java.util.concurrent.locks")}
        modules["java.lang"].System = types.SimpleNamespace(nanoTime=lambda: self.clock.ns)
        modules["java.util.concurrent.locks"].ReentrantLock = Lock
        namespace = {"system": fake_system, "__name__": "phase8_monitor"}
        with patch.dict(sys.modules, modules):
            exec(compile((ROOT.parent / "ignition/scripts/phase8_monitor.py").read_text(),
                         "phase8_monitor.py", "exec"), namespace)
        self.module = namespace
        for path in namespace["READ_PATHS"]:
            folder, name = path.rsplit("/", 1)
            self.tags.configs[folder] = {"name": folder.rsplit("/", 1)[-1], "tagType": "Folder"}
            self.tags.configs[path] = {"name": name, "tagType": "AtomicTag",
                                       "valueSource": "opc", "readOnly": True,
                                       "opcItemPath": "preserve:" + path}
            self.tags.values[path] = False if "/Status/" in path else 0
        self.tags.configs[namespace["BASE"].rstrip("/")] = {"name": "Substation", "tagType": "Folder"}
        self.tags.values[namespace["BASE"] + "Status/Model_Valid"] = True
        self.tags.values[namespace["BASE"] + "Status/Bus_Energized"] = True
        self.tags.values[namespace["BASE"] + "Status/CB1_Closed"] = True
        for name in ("Commands/Command_Code", "Commands/Command_Sequence", "Inputs/Fault_Enable"):
            self.tags.configs[namespace["BASE"] + name]["readOnly"] = False

    def install(self):
        with redirect_stdout(io.StringIO()):
            self.module["install"]()

    def tick(self, seconds, hb=None):
        self.clock.at(seconds)
        if hb is not None:
            self.tags.values[self.module["BASE"] + "Diagnostics/Heartbeat"] = hb
        self.module["tick"]()

    def value(self, name):
        return self.tags.evaluate(self.module["HEALTH"] + name)

    def ready(self):
        self.install()
        self.tick(0, 10)
        self.tick(1, 11)


def monitor_checks():
    h = Harness()
    h.install()
    check(len(h.module["READ_PATHS"]) == 59 and len(set(h.module["READ_PATHS"])) == 59,
          "Monitor covers all 41 OPC points and 18 engineering tags without duplicates")
    check(not h.value("Monitor_Alive") and not h.value("Telemetry_Usable"),
          "An installed but unstarted monitor cannot report usable telemetry")
    h.tick(0, 65535)
    check(h.value("Monitor_Alive") and not h.value("Heartbeat_Fresh") and not h.value("Heartbeat_Stalled"),
          "First heartbeat sample establishes a baseline without claiming freshness or alarming during startup grace")
    h.tick(1, 0)
    check(h.value("Heartbeat_Fresh") and h.value("Telemetry_Usable"),
          "Heartbeat rollover from 65535 to 0 is a valid observed update")
    h.tick(5.9)
    check(h.value("Heartbeat_Fresh"), "Heartbeat remains fresh below its five-second timeout")
    h.tick(6)
    check(not h.value("Heartbeat_Fresh") and h.value("Heartbeat_Stalled") and not h.value("Telemetry_Usable"),
          "An unchanged heartbeat expires exactly at five seconds despite good tag quality")
    check(h.value("Monitor_Alive") and h.value("Tag_Quality_Good"),
          "A stale model heartbeat is distinguished from a stopped monitor or bad OPC quality")
    h.tick(7, 1)
    check(h.value("Telemetry_Usable") and not h.value("Heartbeat_Stalled"),
          "A new heartbeat restores freshness")
    bad = h.module["BASE"] + "Measurements/Grid_P_kW"
    h.tags.bad_paths.add(bad)
    h.tick(8, 2)
    check(h.value("Bad_Tag_Quality") and not h.value("Heartbeat_Stalled") and not h.value("Telemetry_Usable"),
          "Bad or uncertain engineering-tag quality suppresses process use without a duplicate stale alarm")
    h.tags.bad_paths.clear()
    h.tick(9, 3)
    check(h.value("Telemetry_Usable"), "Good quality recovery restores telemetry use with a changing heartbeat")
    hb_path = h.module["BASE"] + "Diagnostics/Heartbeat"
    h.tags.bad_paths.add(hb_path)
    h.tick(10)
    check(h.value("Heartbeat_Age_s") == -1 and not h.value("Heartbeat_Fresh"),
          "Bad heartbeat quality invalidates the previous freshness baseline")
    h.tags.bad_paths.clear()
    h.tick(11, 100)
    check(not h.value("Heartbeat_Fresh"), "Recovery requires a change between good heartbeat samples")
    h.tick(12, 101)
    check(h.value("Telemetry_Usable"), "The next good heartbeat change completes recovery")
    valid = h.module["BASE"] + "Status/Model_Valid"
    h.tags.values[valid] = False
    h.tick(13, 102)
    check(h.value("Communications_Healthy") and h.value("Model_Invalid") and not h.value("Telemetry_Usable"),
          "Model validity is independent of healthy communication and a changing heartbeat")
    h.tags.values[valid] = True
    h.tags.values[h.module["BASE"] + "Status/Bus_Energized"] = False
    h.tick(14, 103)
    check(h.value("Telemetry_Usable") and not h.value("Electrical_Alarms_Enabled"),
          "A valid isolated bus remains usable status and suppresses voltage alarms on its legitimate zeros")
    check("isolated" in h.value("Summary"), "Health summary explains the valid isolated state")
    h.clock.at(20)
    check(not h.value("Monitor_Alive") and not h.value("Telemetry_Usable"),
          "The independent watchdog expires when the Gateway timer stops running")
    check("MONITOR NOT RUNNING" in h.value("Summary"), "Summary does not display a frozen healthy message after timer failure")

    for invalid, label in ((None, "missing"), (float("nan"), "NaN"), (float("inf"), "infinite")):
        x = Harness()
        x.ready()
        x.tags.values[x.module["BASE"] + "Raw/PV_AC_Power_kW"] = invalid
        x.tick(2, 12)
        check(x.value("Bad_Tag_Quality") and not x.value("Telemetry_Usable"),
              "A %s numeric value is rejected even when its OPC quality says Good" % label)
    for invalid in (-1, 65536, 1.5):
        x = Harness()
        x.ready()
        x.tick(2, invalid)
        check(x.value("Bad_Tag_Quality") and not x.value("Heartbeat_Fresh"),
              "Out-of-range or fractional heartbeat %s is rejected" % invalid)

    x = Harness()
    x.ready()
    x.tags.read_error = True
    x.tick(2)
    check(x.value("Monitor_Alive") and x.value("Bad_Tag_Quality") and not x.value("Telemetry_Usable"),
          "A read exception publishes failed health while keeping the monitor watchdog honest")
    check("READ ERROR" in x.value("Summary"), "Read error details remain visible")
    x.tags.read_error = False
    x.tick(3, 50)
    check(not x.value("Heartbeat_Fresh"), "Read-exception recovery starts with a new heartbeat baseline")
    x.tick(4, 51)
    check(x.value("Telemetry_Usable"), "Read-exception recovery completes on the next observed change")
    previous = x.value("Last_Monitor_Tick")
    x.tags.write_fail = True
    x.tick(5, 52)
    check(x.value("Last_Monitor_Tick") == previous and bool(x.logs),
          "Failed health writes are logged and never commit a successful watchdog time")
    x.clock.at(10)
    check(not x.value("Monitor_Alive"), "A failed publisher is detected by the independent watchdog")
    x.tags.write_fail = False
    old = len(x.tags.writes)
    x.module["_lock"].busy = True
    x.tick(11, 53)
    check(len(x.tags.writes) == old, "Concurrent timer entry cannot publish a second health sample")
    x.module["_lock"].busy = False
    check(all(path.startswith(x.module["HEALTH"]) for paths, _ in x.tags.writes for path in paths),
          "The monitor writes only its Health folder, never commands, inputs, or telemetry")
    x = Harness()
    x.ready()
    x.clock.wall_ms -= 86400000
    x.module["tick"]()
    check(x.value("Heartbeat_Fresh"), "Heartbeat elapsed time is independent of a backward wall-clock change")


def installer_checks():
    h = Harness()
    path = h.module["BASE"] + "Measurements/T1_Loading_pct"
    h.tags.configs[path]["alarms"] = [{"name": "ExistingAlarm", "mode": "AboveValue", "setpointA": 150}]
    before = copy.deepcopy(h.tags.configs)
    h.install()
    definitions = h.module["_health_definitions"]()
    check(len(definitions) == 15, "Installer creates 15 named health tags")
    plan = h.module["_alarm_plan"]()
    check(sum(len(alarms) for _, alarms in plan) == 11, "Installer configures 11 named alarms")
    check(any(a["name"] == "ExistingAlarm" for a in h.tags.configs[path]["alarms"]),
          "Unrelated existing alarms are preserved")
    check(all(all(h.tags.configs[p].get(k) == v for k, v in cfg.items() if k != "alarms")
              for p, cfg in before.items()),
          "Existing OPC addresses, permissions, folders, and tag properties are preserved")
    h.tick(0, 1)
    h.tick(1, 2)
    saved_values = copy.deepcopy(h.tags.values)
    h.install()
    check(h.tags.values == saved_values, "Re-running setup does not reset live memory values")
    check(sum(1 for a in h.tags.configs[path]["alarms"] if a["name"] == "P8_T1Overload") == 1,
          "Re-running setup does not duplicate managed alarms")
    flattened = {a["name"]: a for _, alarms in plan for a in alarms}
    check(all(a["ackMode"] == "Manual" for a in flattened.values()),
          "All alarms require operator acknowledgement")
    check(flattened["P8_T1High"]["setpointA"] == 90 and
          flattened["P8_T1Overload"]["setpointA"] == 100 and
          flattened["P8_T1High"]["deadband"] == flattened["P8_T1Overload"]["deadband"] == 3,
          "Transformer warning/overload thresholds include three percentage points of hysteresis")
    check(abs(flattened["P8_BusLow"]["setpointA"] - 13.8 * 0.95) < 1e-9 and
          abs(flattened["P8_BusHigh"]["setpointA"] - 13.8 * 1.05) < 1e-9,
          "Bus thresholds implement 0.95 and 1.05 pu on the 13.8 kV base")
    check("Electrical_Alarms_Enabled" in flattened["P8_BusLow"]["enabled"]["value"] and
          "Communications_Healthy" in flattened["P8_CB1Trip"]["enabled"]["value"],
          "Voltage alarms require a usable supplied bus; trip alarms require healthy communication")
    check(all("timeOnDelaySeconds" in a and "activeDelay" not in a for a in flattened.values()),
          "Alarm delays use Ignition's documented scripting property names")
    x = Harness()
    del x.tags.configs[x.module["READ_PATHS"][-1]]
    try:
        x.install()
    except Exception:
        pass
    check(not x.tags.edits, "Missing required tags abort setup before any configuration writes")
    x = Harness()
    x.tags.configs[x.module["BASE"] + "Health"] = {"name": "Health", "tagType": "AtomicTag"}
    try:
        x.install()
    except Exception:
        pass
    check(not x.tags.edits, "A conflicting Health namespace aborts setup before any configuration writes")
    x = Harness()
    x.tags.configure_fail = True
    failed = False
    try:
        x.install()
    except Exception:
        failed = True
    check(failed, "Bad configuration quality prevents a successful installation report")
    source = (ROOT.parent / "ignition/scripts/phase8_monitor.py").read_text()
    source.encode("ascii")
    tree = ast.parse(source)
    incompatible = (ast.JoinedStr, ast.AnnAssign, ast.AsyncFunctionDef, ast.Await, ast.NamedExpr)
    check(not any(isinstance(node, incompatible) for node in ast.walk(tree)),
          "Ignition module avoids Python 3-only syntax and non-ASCII source")


def freeze_demo_checks():
    clock = Clock()

    class BaseBridge:
        def __init__(self):
            self.heartbeat = 0
            self.hr = [0] * 128

        async def update(self):
            self.heartbeat = (self.heartbeat + 1) % 65536
            self.hr[1] = self.heartbeat
            self.hr[0] += 1
            return True

    fake_phase7 = types.ModuleType("phase7_modbus_server")
    fake_phase7.ControlBridge = BaseBridge
    namespace = {"__name__": "phase8_freeze_demo"}
    with patch.dict(sys.modules, {"phase7_modbus_server": fake_phase7}):
        exec(compile((ROOT / "phase8_freeze_demo.py").read_text(), "phase8_freeze_demo.py", "exec"), namespace)
    with patch.object(namespace["time"], "monotonic", lambda: clock.ns / 1e9):
        bridge = namespace["HeartbeatFreezeBridge"]()
        asyncio.run(bridge.update())
        check(bridge.heartbeat == bridge.hr[1] == 1, "Optional freeze demo starts with a normal moving heartbeat")
        clock.at(19.9)
        asyncio.run(bridge.update())
        check(bridge.heartbeat == bridge.hr[1] == 2, "Heartbeat continues before the configured freeze time")
        clock.at(20)
        with redirect_stdout(io.StringIO()):
            asyncio.run(bridge.update())
        check(bridge.heartbeat == bridge.hr[1] == 2 and bridge.hr[0] == 3,
              "At the freeze boundary only heartbeat publication stops; other updates continue")
        clock.at(25)
        asyncio.run(bridge.update())
        check(bridge.heartbeat == bridge.hr[1] == 2 and bridge.hr[0] == 4,
              "The demo retains its frozen heartbeat across subsequent model publications")


def enum_readback_checks():
    for style, label in (("labels", "display labels including When False and Above Setpoint"),
                         ("objects", "Java-style enums with canonical names and different display text")):
        h = Harness()
        h.tags.enum_style = style
        error = None
        try:
            h.install()
            h.tick(0, 1)
            h.tick(1, 2)
            prior_values = copy.deepcopy(h.tags.values)
            h.install()  # Includes existing Float8/Double and DateTime/Date Time checks.
        except Exception as exc:
            error = str(exc)
        check(error is None and h.tags.values == prior_values,
              "Installer accepts %s and preserves live values on reinstallation" % label)

    h = Harness()
    h.tags.enum_style = "objects"
    h.tags.force_mode = "WhenTrue"
    error = None
    try:
        h.install()
    except Exception as exc:
        error = str(exc)
    check(error is not None and "Alarm mode mismatch" in error,
          "Enum normalization still rejects a genuinely different alarm mode")

    h = Harness()
    h.tags.enum_style = "objects"
    h.tags.force_binding = True
    error = None
    try:
        h.install()
    except Exception as exc:
        error = str(exc)
    check(error is not None and "enabled binding mismatch" in error,
          "Native enum support retains validation of process-alarm enable bindings")


def main():
    error = None
    try:
        monitor_checks()
        installer_checks()
        enum_readback_checks()
        freeze_demo_checks()
    except Exception as exc:
        error = repr(exc)
        print("VERIFICATION ERROR:", error)
    passed = sum(c["passed"] for c in CHECKS)
    success = error is None and passed == len(CHECKS)
    report = {"phase": 8, "scope": "Offline CPython with Ignition/Java API fakes; not a live Ignition verification",
              "passed": success, "passed_checks": passed, "total_checks": len(CHECKS),
              "checks": CHECKS, "error": error}
    path = ROOT / "artifacts/phase8_verification.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print("\nPHASE 8 OFFLINE VERIFICATION: %s (%d/%d checks)" % ("PASS" if success else "FAIL", passed, len(CHECKS)))
    print("Report:", path)
    raise SystemExit(0 if success else 1)


if __name__ == "__main__":
    main()
