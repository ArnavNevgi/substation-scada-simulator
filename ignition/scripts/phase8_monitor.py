# Ignition Project Library module: phase8_monitor
# Paste this whole file into Ignition, then save the project.
# Jython 2.7 compatible. Windows Python is only used by the offline verifier.

import math
from java.lang import System
from java.util.concurrent.locks import ReentrantLock

BASE = "[default]Substation/"
HEALTH = BASE + "Health/"
HEARTBEAT_TIMEOUT_S = 5.0

ANALOG_NAMES = (
    "LineSide_Voltage_kV", "Bus_Voltage_kV", "CB1_Current_A", "Grid_P_kW",
    "Grid_Q_kVAr", "Load_P_kW", "Load_Q_kVAr", "T1_Loading_pct",
    "PV_Available_DC_kW", "PV_DC_Power_kW", "PV_DC_Voltage_V",
    "PV_DC_Current_A", "PV_AC_Power_kW", "PV_Q_kVAr", "PV_Efficiency_pct",
    "PV_Temperature_C", "Grid_Frequency_Hz", "LastFault_Ikss_kA"
)
STATUS_NAMES = (
    "CB1_Closed", "Trip_Latched", "PV_Running", "Grid_Available", "Model_Valid",
    "Bus_Energized", "Remote_Mode", "Fault_Active", "PV_Tripped"
)
DIAGNOSTIC_NAMES = (
    "Heartbeat", "Model_State", "Command_Status", "Command_Reason",
    "Command_Ack_Sequence"
)
INPUT_NAMES = (
    "Load_P_Set_kW", "Load_Q_Set_kVAr", "Irradiance_Set_W_m2",
    "PV_P_Limit_Set_kW", "PV_Q_Set_kVAr", "Fault_Enable", "Remote_Mode_Set"
)
READ_PATHS = tuple(
    BASE + folder + "/" + name
    for folder, names in (
        ("Raw", ANALOG_NAMES), ("Measurements", ANALOG_NAMES),
        ("Status", STATUS_NAMES), ("Diagnostics", DIAGNOSTIC_NAMES),
        ("Inputs", INPUT_NAMES),
        ("Commands", ("Command_Code", "Command_Sequence"))
    )
    for name in names
)

_lock = ReentrantLock()
_last_hb = None
_last_change_ns = None
_have_change = False


def _config_key(value):
    """Compare native Java enums, JSON names and English display labels."""
    raw = value
    try:
        enum_name = getattr(value, "name", None)
        if callable(enum_name):
            raw = enum_name()
    except Exception:
        pass  # Some config objects expose only their display text.
    key = "".join(c for c in str(raw).lower() if c.isalnum())
    aliases = {
        "equal": "equality", "notequal": "inequality",
        "abovesetpoint": "abovevalue", "belowsetpoint": "belowvalue",
        "betweensetpoints": "betweenvalues", "outsidesetpoints": "outsidevalues",
        "outofrange": "outofengrange", "bitstate": "bit",
        "double": "float8", "float": "float4", "integer": "int4"
    }
    return aliases.get(key, key)


def _number(value):
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        raise ValueError("non-finite value")
    return number


def _sample(values, now_ns):
    """Compare heartbeat VALUES with elapsed monotonic time, not tag timestamps."""
    global _last_hb, _last_change_ns, _have_change
    bad = []
    observed = {}
    if len(values) != len(READ_PATHS):
        raise ValueError("Incomplete tag read")
    for path, qualified in zip(READ_PATHS, values):
        try:
            if not qualified.quality.isGood() or qualified.value is None:
                raise ValueError("bad or uncertain quality / no value")
            if path.startswith(BASE + "Status/"):
                if qualified.value not in (True, False):
                    raise ValueError("invalid Boolean status")
            else:
                _number(qualified.value)
            observed[path] = qualified.value
        except Exception:
            bad.append(path)

    hb_path = BASE + "Diagnostics/Heartbeat"
    hb = None
    if hb_path in observed:
        try:
            number = _number(observed[hb_path])
            if number != int(number) or not 0 <= number <= 65535:
                raise ValueError("invalid heartbeat")
            hb = int(number)
        except Exception:
            bad.append(hb_path)

    if hb is None:
        # On recovery require an observed change between good heartbeat samples.
        _last_hb, _last_change_ns, _have_change = None, None, False
        age, fresh = -1.0, False
    elif _last_hb is None:
        _last_hb, _last_change_ns, _have_change = hb, now_ns, False
        age, fresh = 0.0, False
    else:
        if hb != _last_hb:  # Includes the normal 65535 -> 0 rollover.
            _last_hb, _last_change_ns, _have_change = hb, now_ns, True
        age = max(0.0, (now_ns - _last_change_ns) / 1000000000.0)
        fresh = _have_change and age < HEARTBEAT_TIMEOUT_S

    quality_good = not bad
    model_valid = bool(observed.get(BASE + "Status/Model_Valid", False))
    bus_energized = bool(observed.get(BASE + "Status/Bus_Energized", False))
    if bad:
        detail = "BAD TAG QUALITY/DATA: " + bad[0]
        if len(bad) > 1:
            detail += " (+%d others)" % (len(bad) - 1)
    elif not fresh:
        if not _have_change and age < HEARTBEAT_TIMEOUT_S:
            detail = "WAITING FOR HEARTBEAT CHANGE"
        else:
            detail = "STALE HEARTBEAT: unchanged for %.1f s" % age
    elif not model_valid:
        detail = "MODEL INVALID/UPDATING: do not use electrical measurements"
    elif not bus_energized:
        detail = "HEALTHY: valid isolated-bus state"
    else:
        detail = "HEALTHY: quality good, heartbeat changing, model valid"
    return [quality_good, fresh, age, model_valid, bus_energized, detail]


def _write_health(payload):
    names = ("Tag_Quality_Good", "Heartbeat_Fresh", "Heartbeat_Age_s",
             "Model_Valid_Observed", "Bus_Energized_Observed", "Detail")
    results = system.tag.writeBlocking([HEALTH + n for n in names], payload, 1500)
    if len(results) != len(names) or not all(q.isGood() for q in results):
        raise Exception("Health publication failed: " + str(results))
    # Commit the watchdog time only after ALL health fields were written well.
    results = system.tag.writeBlocking(
        [HEALTH + "Last_Monitor_Tick"], [system.date.now()], 1500)
    if len(results) != 1 or not results[0].isGood():
        raise Exception("Monitor watchdog write failed: " + str(results))


def tick():
    """Call ONLY from one Gateway Timer, every 1000 ms, Fixed Delay, Dedicated."""
    global _last_hb, _last_change_ns, _have_change
    if not _lock.tryLock():
        return
    try:
        try:
            values = system.tag.readBlocking(list(READ_PATHS), 1500)
            payload = _sample(values, System.nanoTime())
        except Exception as error:
            # Never publish a successful watchdog tick with old 'healthy' flags.
            _last_hb, _last_change_ns, _have_change = None, None, False
            payload = [False, False, -1.0, False, False,
                       "TAG READ ERROR: " + str(error)]
        _write_health(payload)
    except Exception as error:
        system.util.getLogger("Substation.Phase8").error(str(error))
    finally:
        _lock.unlock()


def _checked_configure(parent, configs, policy="m"):
    results = system.tag.configure(parent, configs, policy)
    if len(results) != len(configs) or not all(q.isGood() for q in results):
        raise Exception("Tag configuration failed at " + parent + ": " + str(results))


def _alarm(name, label, priority, mode, delay=0.0, enabled=None, **extra):
    alarm = {"name": "P8_" + name, "label": label,
             "displayPath": "Substation/" + label,
             "priority": priority, "mode": mode, "ackMode": "Manual",
             "timestampSource": "System", "timeOnDelaySeconds": float(delay),
             "timeOffDelaySeconds": 0.0}
    if enabled is not None:
        alarm["enabled"] = {"bindType": "Expression", "value": enabled}
    alarm.update(extra)
    return alarm


def _alarm_plan():
    comms = "{" + HEALTH + "Communications_Healthy}"
    usable = "{" + HEALTH + "Telemetry_Usable}"
    supplied = "{" + HEALTH + "Electrical_Alarms_Enabled}"
    return [
        (HEALTH + "Monitor_Alive", [
            _alarm("MonitorStopped", "Health monitor stopped", "High", "WhenFalse", 3)]),
        (HEALTH + "Bad_Tag_Quality", [
            _alarm("BadQuality", "Bad tag quality or invalid data", "High", "WhenTrue", 2)]),
        (HEALTH + "Heartbeat_Stalled", [
            _alarm("HeartbeatStalled", "Simulator heartbeat stalled", "High", "WhenTrue", 2)]),
        (HEALTH + "Model_Invalid", [
            _alarm("ModelInvalid", "Electrical model invalid", "High", "WhenTrue", 3)]),
        (BASE + "Measurements/T1_Loading_pct", [
            _alarm("T1High", "T1 loading above 90 percent", "Medium", "AboveValue", 3,
                   usable, setpointA=90.0, inclusiveA=False, deadband=3.0,
                   deadbandMode="Absolute"),
            _alarm("T1Overload", "T1 overload above 100 percent", "High", "AboveValue", 3,
                   usable, setpointA=100.0, inclusiveA=False, deadband=3.0,
                   deadbandMode="Absolute")]),
        (BASE + "Measurements/Bus_Voltage_kV", [
            _alarm("BusLow", "13.8 kV bus low voltage", "Medium", "BelowValue", 3,
                   supplied, setpointA=13.11, inclusiveA=False, deadband=0.138,
                   deadbandMode="Absolute"),
            _alarm("BusHigh", "13.8 kV bus high voltage", "Medium", "AboveValue", 3,
                   supplied, setpointA=14.49, inclusiveA=False, deadband=0.138,
                   deadbandMode="Absolute")]),
        (BASE + "Status/Trip_Latched", [
            _alarm("CB1Trip", "CB1 protection trip latched", "High", "WhenTrue", enabled=comms)]),
        (BASE + "Status/PV_Tripped", [
            _alarm("PVTrip", "PV trip latched", "Medium", "WhenTrue", enabled=comms)]),
        (BASE + "Status/Fault_Active", [
            _alarm("FaultActive", "Simulated bus fault present", "High", "WhenTrue", enabled=comms)])
    ]


def _health_definitions():
    def ref(name):
        return "{" + HEALTH + name + "}"
    age_ms = "(toMillis(now(1000)) - toMillis(" + ref("Last_Monitor_Tick") + "))"
    memory = [
        ("Last_Monitor_Tick", "DateTime", system.date.fromMillis(0)),
        ("Tag_Quality_Good", "Boolean", False),
        ("Heartbeat_Fresh", "Boolean", False),
        ("Heartbeat_Age_s", "Float8", -1.0),
        ("Model_Valid_Observed", "Boolean", False),
        ("Bus_Energized_Observed", "Boolean", False),
        ("Detail", "String", "MONITOR NOT STARTED")
    ]
    expressions = [
        ("Monitor_Alive", "Boolean", age_ms + " >= 0 && " + age_ms + " <= 5000"),
        ("Communications_Healthy", "Boolean", ref("Monitor_Alive") + " && " +
         ref("Tag_Quality_Good") + " && " + ref("Heartbeat_Fresh")),
        ("Telemetry_Usable", "Boolean", ref("Communications_Healthy") + " && " +
         ref("Model_Valid_Observed")),
        ("Electrical_Alarms_Enabled", "Boolean", ref("Telemetry_Usable") + " && " +
         ref("Bus_Energized_Observed")),
        ("Bad_Tag_Quality", "Boolean", ref("Monitor_Alive") + " && !" + ref("Tag_Quality_Good")),
        ("Heartbeat_Stalled", "Boolean", ref("Monitor_Alive") + " && " +
         ref("Tag_Quality_Good") + " && !" + ref("Heartbeat_Fresh") +
         " && " + ref("Heartbeat_Age_s") + " >= 5"),
        ("Model_Invalid", "Boolean", ref("Communications_Healthy") + " && !" +
         ref("Model_Valid_Observed")),
        ("Summary", "String", "if(" + ref("Monitor_Alive") + ", " + ref("Detail") +
         ', "MONITOR NOT RUNNING: check Gateway Timer")')
    ]
    configs = []
    for name, datatype, value in memory:
        configs.append({"name": name, "tagType": "AtomicTag", "valueSource": "memory",
                        "dataType": datatype, "value": value,
                        "documentation": "Phase 8 Gateway monitor output; do not operate equipment with this tag."})
    for name, datatype, expression in expressions:
        configs.append({"name": name, "tagType": "AtomicTag", "valueSource": "expr",
                        "dataType": datatype, "expression": expression,
                        "documentation": "Phase 8 derived telemetry-health status."})
    return configs


def install():
    """Run once from Designer Script Console: phase8_monitor.install()"""
    missing = [path for path in READ_PATHS if not system.tag.exists(path)]
    if missing:
        raise Exception("Phase 5/7 tags missing; nothing changed: " + ", ".join(missing))
    definitions = _health_definitions()
    # Preflight the complete namespace before creating or editing anything.
    if system.tag.exists(BASE + "Health"):
        config = system.tag.getConfiguration(BASE + "Health", False)[0]
        if _config_key(config.get("tagType")) != _config_key("Folder"):
            raise Exception("Health already exists and is not a folder; nothing changed")
    for definition in definitions:
        path = HEALTH + definition["name"]
        if system.tag.exists(path):
            config = system.tag.getConfiguration(path, False)[0]
            if _config_key(config.get("tagType", "AtomicTag")) != _config_key("AtomicTag"):
                raise Exception("Unexpected existing tag type: " + path)
            for key in ("valueSource", "dataType"):
                if key in config and _config_key(config[key]) != _config_key(definition[key]):
                    raise Exception("Unexpected existing " + key + ": " + path)
    if not system.tag.exists(BASE + "Health"):
        _checked_configure(BASE.rstrip("/"), [{"name": "Health", "tagType": "Folder"}], "a")
    for definition in definitions:
        path = HEALTH + definition["name"]
        if definition["valueSource"] == "memory" and system.tag.exists(path):
            continue  # Re-running install never resets live monitor values.
        _checked_configure(HEALTH.rstrip("/"), [definition])

    count = 0
    for path, managed in _alarm_plan():
        config = system.tag.getConfiguration(path, False)[0]
        # Preserve unrelated alarms and all OPC paths, scaling and permissions.
        old = [dict(a) for a in config.get("alarms", [])
               if not str(a.get("name", "")).startswith("P8_")]
        parent, name = path.rsplit("/", 1)
        _checked_configure(parent, [{"name": name, "alarmEvalEnabled": True,
                                     "alarms": old + managed}])
        count += len(managed)

    # Read back the configured alarm properties, including binding definitions.
    for path, expected in _alarm_plan():
        actual = dict((str(a["name"]), a) for a in
                      system.tag.getConfiguration(path, False)[0].get("alarms", []))
        for alarm in expected:
            if alarm["name"] not in actual:
                raise Exception("Alarm missing after configuration: " + alarm["name"])
            installed = actual[alarm["name"]]
            # getConfiguration omits defaults. Equality is the default mode;
            # compare other enum properties when they are explicitly returned.
            returned_mode = installed.get("mode", "Equality")
            if _config_key(returned_mode) != _config_key(alarm["mode"]):
                raise Exception("Alarm mode mismatch: %s (expected %s, returned %s)" %
                                (alarm["name"], alarm["mode"], returned_mode))
            for key in ("priority", "ackMode"):
                if key in installed and _config_key(installed[key]) != _config_key(alarm[key]):
                    raise Exception("Alarm property mismatch: " + alarm["name"] + "." + key)
            # Default zero delays may be omitted by getConfiguration.
            for key in ("timeOnDelaySeconds", "timeOffDelaySeconds", "setpointA", "deadband"):
                if key in alarm and abs(float(installed.get(key, 0)) - float(alarm[key])) > 0.000001:
                    raise Exception("Alarm numeric property mismatch: " + alarm["name"] + "." + key)
            if "enabled" in alarm:
                binding = installed.get("enabled", {})
                if not hasattr(binding, "get") or _config_key(binding.get("bindType")) != _config_key("Expression") or \
                        str(binding.get("value")) != alarm["enabled"]["value"]:
                    raise Exception("Alarm enabled binding mismatch: " + alarm["name"])
    print("PHASE 8 TAG/ALARM SETUP: PASS (%d health tags, %d alarms)" % (len(definitions), count))
    print("Next: enable the one Gateway Timer. Initial monitor alarms are expected until it runs.")

