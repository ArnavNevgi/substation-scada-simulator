"""Offline verification: real TCP feedback + original Phase 7 control engine.

Ignition tag/audit APIs and Java clock/lock classes are test doubles. This does
not claim an Ignition/Jython runtime integration test. No external service is
contacted; the TCP fixture binds to 127.0.0.1 on an ephemeral port.
"""
import datetime as dt
import importlib.util
import json
from pathlib import Path
import socket
import socketserver
import struct
import sys
import threading
import types
import unittest
import uuid

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


engine_module = load_file("phase11_reference_control", ROOT / "phase7_control.py")


class JavaFailure(BaseException):
    pass


class Quality:
    def __init__(self, good=True):
        self.good = good

    def isGood(self):
        return self.good

    def __repr__(self):
        return "Good" if self.good else "Bad"


class Qualified:
    def __init__(self, value, good=True):
        self.value = value
        self.quality = Quality(good)


class Lock:
    def __init__(self):
        self.value = threading.RLock()

    def tryLock(self):
        return self.value.acquire(False)

    def lock(self):
        self.value.acquire()

    def unlock(self):
        self.value.release()


class Dataset:
    # Exact column labels observed from the user's Ignition 8.3.10 Local profile.
    columns = ["Timestamp", "Actor", "Actor Host", "Action", "Action Target",
               "Action Value", "Result Code", "Result", "System", "Context Code",
               "Context"]

    def __init__(self, rows):
        self.rows = rows

    def getColumnNames(self):
        return self.columns

    def getRowCount(self):
        return len(self.rows)

    def getValueAt(self, row, column):
        return self.rows[row][column]


class Fixture:
    def __init__(self):
        config = json.loads((ROOT / "config/substation.json").read_text())
        self.engine = engine_module.ControlEngine(
            config, {"bus_ikss_ka": 7.445, "cb1_hv_current_a": 893.4})
        self.engine.model_valid = True
        self.engine.bus_energized = True
        self.engine.pv_running = True
        self.engine.normal_current_a = 42.3
        self.now = 0.0
        self.code = 0
        self.writes = []
        self.records = []
        self.errors = []
        self.audit_fail_phase = None
        self.java_audit_failure = False
        self.bad_quality = False
        self.payload_failure = False
        self.commit_failure = False
        self.deliver_bad_commit = False
        self.fail_reads_after_commit = False
        self.feedback_override = None
        self.malformed = None
        self.fragment = False
        self.sleep_callback = None
        self.requests = []
        self.mutex = threading.RLock()

    def nanoTime(self):
        return int(self.now * 1e9)

    def sleep(self, milliseconds):
        self.now += milliseconds / 1000.0
        self.engine.tick(self.now)
        if self.sleep_callback is not None:
            callback, self.sleep_callback = self.sleep_callback, None
            callback()

    def date_now(self):
        return dt.datetime(2026, 10, 19, 0, 0, tzinfo=dt.timezone.utc) + dt.timedelta(seconds=self.now)

    def read_tags(self, paths, timeout):
        # Deliberately stale tag-cache values. These must not confirm an outcome
        # or supply the next sequence; the feedback TCP response is authoritative.
        return [Qualified(0, not self.bad_quality) for _ in paths]

    def write_tags(self, paths, values, timeout):
        path, value = paths[0], values[0]
        self.writes.append((path, value))
        if path.endswith("Commands/Command_Code"):
            self.code = value
            if self.payload_failure:
                return [Quality(False)]
        elif path.endswith("Commands/Command_Sequence"):
            if not self.commit_failure or self.deliver_bad_commit:
                self.engine.submit(self.code, value, self.now)
                self.engine.tick(self.now)
            if self.commit_failure:
                raise JavaFailure("simulated uncertain commit write")
        elif path.endswith("Inputs/Remote_Mode_Set"):
            self.engine.environment(remote=bool(value))
        elif path.endswith("Inputs/Fault_Enable"):
            self.engine.environment(fault=bool(value))
        return [Quality()]

    def audit(self, **args):
        phase = args["action"].removeprefix("COMMAND_")
        if phase == self.audit_fail_phase:
            if self.java_audit_failure:
                raise JavaFailure("simulated Java audit failure")
            raise RuntimeError("simulated audit failure")
        self.records.append(dict(args))

    def query_audit(self, **kwargs):
        rows = [[r["eventTimestamp"], r.get("actor", "test-automatic-actor"),
                 r.get("actorHost", ""), r["action"], r["actionTarget"],
                 r["actionValue"], 0, "Good", "test-system", 2, "Gateway"]
                for r in self.records]
        rows.append([self.date_now(), "operator", "host", "tag write", "some/tag",
                     "1", 0, "Good", "test-system", 2, "Gateway"])
        return Dataset(rows)

    def install_java(self):
        java = types.ModuleType("java")
        lang = types.ModuleType("java.lang")
        lang.Exception = JavaFailure
        lang.System = types.SimpleNamespace(nanoTime=self.nanoTime)
        lang.Thread = types.SimpleNamespace(sleep=self.sleep)
        util = types.ModuleType("java.util")
        util.UUID = types.SimpleNamespace(randomUUID=uuid.uuid4)
        concurrent = types.ModuleType("java.util.concurrent")
        locks = types.ModuleType("java.util.concurrent.locks")
        locks.ReentrantLock = Lock
        for module in (java, lang, util, concurrent, locks):
            sys.modules[module.__name__] = module

    def system_api(self):
        date = types.SimpleNamespace(
            now=self.date_now, addHours=lambda value, hours: value + dt.timedelta(hours=hours),
            format=lambda value, pattern: value.strftime("%Y-%m-%d %H:%M:%S %z"),
            toMillis=lambda value: int(value.timestamp() * 1000))
        util = types.SimpleNamespace(
            audit=self.audit, jsonEncode=lambda value: json.dumps(value, separators=(",", ":")),
            jsonDecode=json.loads, queryAuditLog=self.query_audit,
            getLogger=lambda name: types.SimpleNamespace(error=self.errors.append))
        tag = types.SimpleNamespace(readBlocking=self.read_tags, writeBlocking=self.write_tags)
        return types.SimpleNamespace(date=date, util=util, tag=tag)


def receive(sock, count):
    result = b""
    while len(result) < count:
        try:
            chunk = sock.recv(count - len(result))
        except ConnectionResetError:
            return None
        if not chunk:
            return None
        result += chunk
    return result


class Handler(socketserver.BaseRequestHandler):
    def handle(self):
        f = self.server.fixture
        while True:
            request = receive(self.request, 12)
            if request is None:
                return
            transaction, protocol, length, unit, function, address, count = struct.unpack(
                ">HHHBBHH", request)
            f.requests.append((function, address, count))
            if f.fail_reads_after_commit and any("Command_Sequence" in p for p, v in f.writes):
                return
            with f.mutex:
                values = f.feedback_override or (
                    f.engine.command_status, f.engine.command_reason, f.engine.ack_sequence)
                pdu = struct.pack(">BBHHH", 3, 6, *values)
                response_unit, response_protocol = unit, protocol
                if f.malformed == "transaction":
                    transaction = (transaction + 1) % 65536
                elif f.malformed == "protocol":
                    response_protocol = 1
                elif f.malformed == "unit":
                    response_unit = (unit + 1) % 256
                elif f.malformed == "function":
                    pdu = bytes([4]) + pdu[1:]
                elif f.malformed == "bytecount":
                    pdu = pdu[:1] + bytes([4]) + pdu[2:]
                elif f.malformed == "exception":
                    pdu = bytes([0x83, 2])
                response_length = len(pdu) + 1
                if f.malformed == "length":
                    response_length = 0
                response = struct.pack(
                    ">HHHB", transaction, response_protocol, response_length, response_unit) + pdu
            try:
                if f.fragment:
                    for byte in response:
                        self.request.sendall(bytes([byte]))
                else:
                    self.request.sendall(response)
            except (BrokenPipeError, ConnectionResetError):
                return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.f = Fixture()
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.fixture = self.f
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.f.install_java()
        self.m = load_file("phase11_under_test", ROOT.parent / "ignition/scripts/phase7_controls.py")
        self.m.system = self.f.system_api()
        self.m.FEEDBACK_PORT = self.server.server_address[1]
        self.m.SOCKET_TIMEOUT_S = 0.1
        self.m.RESULT_TIMEOUT_S = 2.0
        self.source = types.SimpleNamespace(session=types.SimpleNamespace(props=types.SimpleNamespace(
            id="session-123", host="127.0.0.1", auth=types.SimpleNamespace(
                authenticated=True, user=types.SimpleNamespace(userName="arnav")))))

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def phases(self):
        return [r["action"].removeprefix("COMMAND_") for r in self.f.records]

    def test_open_waits_for_real_completion_and_links_audit_records(self):
        message = self.m.send_command(1, self.source)
        self.assertIn("APPLIED #1", message)
        self.assertFalse(self.f.engine.breaker_closed)
        self.assertGreaterEqual(self.f.now, 0.3)
        self.assertEqual(self.phases(), ["REQUESTED", "APPLIED"])
        a, b = [json.loads(r["actionValue"]) for r in self.f.records]
        self.assertEqual(a["rid"], b["rid"])
        self.assertEqual((a["seq"], b["seq"]), (1, 1))
        self.assertEqual([p.rsplit("/", 1)[1] for p, v in self.f.writes], ["Command_Code", "Command_Sequence"])
        self.assertTrue(all(request == (3, 19, 3) for request in self.f.requests))
        self.assertEqual(self.f.records[0]["actor"], "arnav")

    def test_close_rejected_for_fault_records_reason_without_moving_breaker(self):
        self.f.engine.breaker_closed = False
        self.f.engine.fault_active = True
        self.assertIn("REJECTED", self.m.send_command(2))
        self.assertFalse(self.f.engine.breaker_closed)
        self.assertEqual(json.loads(self.f.records[-1]["actionValue"])["reason"], 2)

    def test_local_mode_rejects_pv_stop_and_preserves_enabled_state(self):
        self.f.engine.remote_mode = False
        self.assertIn("Local mode", self.m.send_command(5))
        self.assertTrue(self.f.engine.pv_enabled)
        self.assertEqual(self.phases(), ["REQUESTED", "REJECTED"])

    def test_pv_start_applied_does_not_claim_running_power(self):
        self.f.engine.pv_running = False
        self.f.engine.pv_enabled = False
        self.assertIn("APPLIED", self.m.send_command(4))
        self.assertTrue(self.f.engine.pv_enabled)
        self.assertFalse(self.f.engine.pv_running)

    def test_reset_rejected_while_fault_active(self):
        self.f.engine.fault_active = True
        self.assertIn("Fault active", self.m.send_command(3))
        self.assertEqual(self.phases(), ["REQUESTED", "REJECTED"])

    def test_unknown_command_never_writes(self):
        self.assertIn("Not sent", self.m.send_command(99))
        self.assertEqual(self.f.writes, [])
        self.assertEqual(self.phases(), ["NOT_SENT"])

    def test_bad_tag_quality_never_writes(self):
        self.f.bad_quality = True
        self.assertIn("bad quality", self.m.send_command(1))
        self.assertEqual(self.f.writes, [])

    def test_request_audit_failure_blocks_command(self):
        self.f.audit_fail_phase = "REQUESTED"
        self.assertIn("request audit failed", self.m.send_command(1))
        self.assertEqual(self.f.writes, [])
        self.assertTrue(self.f.errors)

    def test_java_audit_exception_is_caught_and_blocks_command(self):
        self.f.audit_fail_phase = "REQUESTED"
        self.f.java_audit_failure = True
        self.assertIn("request audit failed", self.m.send_command(1))
        self.assertEqual(self.f.writes, [])

    def test_failed_payload_never_commits_sequence(self):
        self.f.payload_failure = True
        self.assertIn("Not sent", self.m.send_command(1))
        self.assertEqual(len(self.f.writes), 1)
        self.assertEqual(self.f.engine.ack_sequence, 0)
        self.assertEqual(self.phases(), ["REQUESTED", "NOT_SENT"])

    def test_uncertain_commit_not_delivered_is_unknown_and_blocks_new_request(self):
        self.f.commit_failure = True
        self.assertIn("UNKNOWN #1", self.m.send_command(1))
        writes = len(self.f.writes)
        self.assertIn("requires reconciliation", self.m.send_command(2))
        self.assertEqual(len(self.f.writes), writes)
        self.assertEqual(self.phases()[:3], ["REQUESTED", "WRITE_UNCERTAIN", "UNKNOWN"])

    def test_uncertain_commit_delivered_uses_device_outcome_without_retry(self):
        self.f.commit_failure = self.f.deliver_bad_commit = True
        self.assertIn("APPLIED #1", self.m.send_command(1))
        self.assertEqual(len(self.f.writes), 2)
        self.assertEqual(self.phases(), ["REQUESTED", "WRITE_UNCERTAIN", "APPLIED"])

    def test_feedback_loss_is_unknown_and_read_only_reconciliation_recovers(self):
        self.f.fail_reads_after_commit = True
        self.assertIn("UNKNOWN", self.m.send_command(1))
        self.assertEqual(len(self.f.writes), 2)
        self.f.fail_reads_after_commit = False
        self.assertIn("RECONCILED_APPLIED", self.m.reconcile_pending(self.source))
        self.assertEqual(len(self.f.writes), 2)
        self.assertIsNone(self.m._unresolved)

    def test_reconciliation_cannot_clear_an_unmatched_unknown(self):
        self.f.commit_failure = True
        self.m.send_command(1)
        self.assertIn("Still unresolved", self.m.reconcile_pending())
        self.assertIsNotNone(self.m._unresolved)
        self.assertEqual(len(self.f.writes), 2)

    def test_superseded_ack_is_not_reported_as_success(self):
        self.f.sleep_callback = lambda: setattr(self.f, "feedback_override", (2, 0, 44))
        self.assertIn("superseded", self.m.send_command(1))
        self.assertEqual(self.phases(), ["REQUESTED", "UNKNOWN"])

    def test_pending_backend_blocks_new_writes(self):
        self.f.engine.command_status = 1
        self.assertIn("pending command", self.m.send_command(1))
        self.assertEqual(self.f.writes, [])

    def test_sequence_rollover_accepts_zero(self):
        self.f.engine.last_sequence = self.f.engine.ack_sequence = 65535
        self.f.engine.command_status = 2
        self.assertIn("APPLIED #0", self.m.send_command(5))
        self.assertEqual(self.f.engine.ack_sequence, 0)

    def test_two_commands_do_not_reuse_a_sequence_despite_stale_tag_cache(self):
        self.assertIn("APPLIED #1", self.m.send_command(5))
        # The real power-flow worker supplies this next valid snapshot after
        # STOP changes the electrical operating point. Respect its interlock.
        self.f.engine.model_valid = True
        self.f.engine.bus_energized = True
        self.assertIn("APPLIED #2", self.m.send_command(4))
        ids = [json.loads(r["actionValue"])["rid"] for r in self.f.records if r["action"] == "COMMAND_REQUESTED"]
        self.assertEqual(len(set(ids)), 2)

    def test_final_audit_failure_preserves_actual_applied_outcome(self):
        self.f.audit_fail_phase = "APPLIED"
        message = self.m.send_command(1)
        self.assertIn("APPLIED #1", message)
        self.assertIn("AUDIT WRITE FAILED", message)
        self.assertFalse(self.f.engine.breaker_closed)
        self.assertIsNone(self.m._active)

    def test_second_button_is_blocked_while_outcome_is_pending(self):
        peer_results = []
        self.f.sleep_callback = lambda: peer_results.append(self.m.send_command(2))
        self.assertIn("APPLIED #1", self.m.send_command(1))
        self.assertIn("awaiting its outcome", peer_results[0])
        self.assertEqual(len(self.f.writes), 2)

    def test_mode_change_during_pending_close_produces_failed_outcome(self):
        self.f.engine.breaker_closed = False
        self.f.sleep_callback = lambda: self.m.set_simulation("Remote_Mode_Set", 0)
        self.assertIn("FAILED #1", self.m.send_command(2))
        self.assertFalse(self.f.engine.breaker_closed)
        self.assertEqual(json.loads(self.f.records[-1]["actionValue"])["reason"], 1)

    def test_explicit_anonymous_identity_is_recorded(self):
        self.source.session.props.auth.authenticated = False
        self.m.send_command(5, self.source)
        self.assertEqual(self.f.records[0]["actor"], "anonymous")

    def test_fragmented_tcp_response_is_reassembled(self):
        self.f.fragment = True
        self.assertIn("APPLIED", self.m.send_command(5))

    def test_malformed_modbus_frames_never_permit_submission(self):
        for kind in ("transaction", "protocol", "unit", "function", "bytecount", "exception", "length"):
            with self.subTest(kind=kind):
                self.f.malformed = kind
                self.assertIn("Not sent", self.m.send_command(5))
                self.assertEqual(self.f.writes, [])

    def test_invalid_terminal_status_or_reason_never_permits_submission(self):
        for feedback in ((7, 0, 0), (2, 55, 0), (2, 1, 0)):
            with self.subTest(feedback=feedback):
                self.f.feedback_override = feedback
                self.assertIn("Not sent", self.m.send_command(5))
                self.assertEqual(self.f.writes, [])

    def test_connection_failure_before_submission_never_writes(self):
        self.m.FEEDBACK_HOST = "invalid.invalid"
        # Avoid DNS/network dependence: an already-closed local ephemeral port.
        self.m.FEEDBACK_HOST = "127.0.0.1"
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.m.FEEDBACK_PORT = closed.getsockname()[1]
        closed.close()
        self.assertIn("Not sent", self.m.send_command(5))
        self.assertEqual(self.f.writes, [])

    def test_history_extracts_actor_sequence_id_and_excludes_automatic_tag_writes(self):
        self.m.send_command(1, self.source)
        writes_before = list(self.f.writes)
        records_before = list(self.f.records)
        rows = self.m.history_rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["Event"], "APPLIED")
        self.assertEqual(rows[0]["Actor"], "arnav")
        self.assertEqual(rows[0]["Sequence"], 1)
        self.assertEqual(rows[0]["RequestID"], rows[1]["RequestID"])
        self.assertEqual(rows[0]["Command"], "OPEN CB1")
        self.assertIn("2026-10-19", rows[0]["Time"])
        self.assertEqual(self.f.writes, writes_before)
        self.assertEqual(self.f.records, records_before)

    def test_history_accepts_event_timestamp_label_and_reordered_columns(self):
        self.m.send_command(5, self.source)
        rows = [[r["actionValue"], r["actionTarget"], r["action"],
                 r["eventTimestamp"], r["actor"]] for r in self.f.records]
        data = Dataset(rows)
        data.columns = ["ACTION_VALUE", "ACTION_TARGET", "ACTION",
                        "EVENT_TIMESTAMP", "ACTOR"]
        self.m.system.util.queryAuditLog = lambda **kwargs: data
        history = self.m.history_rows()
        self.assertEqual(len(history), 2)
        self.assertTrue(all(row["Command"] == "STOP PV1" for row in history))
        self.assertEqual({row["Event"] for row in history}, {"REQUESTED", "APPLIED"})
        self.assertEqual(history[0]["RequestID"], history[1]["RequestID"])

    def test_history_rejects_schema_with_missing_timestamp(self):
        data = Dataset([])
        data.columns = Dataset.columns[1:]
        self.m.system.util.queryAuditLog = lambda **kwargs: data
        with self.assertRaises(ValueError):
            self.m.history_rows()

    def test_history_requires_expected_column_schema(self):
        data = Dataset([])
        data.columns = ["unexpected"]
        self.m.system.util.queryAuditLog = lambda **kwargs: data
        with self.assertRaises(ValueError):
            self.m.history_rows()

    def test_audit_value_stays_under_255_characters_even_with_escaped_details(self):
        ctx = self.m._context(1)
        self.m._audit(ctx, "NOT_SENT", detail="\u4e2d" * 96)
        self.assertLessEqual(len(self.f.records[-1]["actionValue"]), 240)

    def test_reconciliation_audit_failure_keeps_unknown_guard(self):
        self.f.fail_reads_after_commit = True
        self.m.send_command(1)
        self.f.fail_reads_after_commit = False
        self.f.audit_fail_phase = "RECONCILED_APPLIED"
        self.assertIn("audit failed", self.m.reconcile_pending())
        self.assertIsNotNone(self.m._unresolved)

    def test_empty_history_is_an_empty_table(self):
        self.assertEqual(self.m.history_rows(), [])

    def test_installation_check_queries_feedback_and_audit_without_writing(self):
        result = self.m.check_installation()
        self.assertEqual(result["module_version"], "11.1")
        self.assertEqual(result["ack_sequence"], 0)
        self.assertEqual(result["audit_rows"], 1)
        self.assertEqual(self.f.writes, [])
        self.assertEqual(self.f.records, [])


if __name__ == "__main__":
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(AuditTests)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    report = {"suite": "Phase 11 offline command audit verification",
              "tests_run": result.testsRun, "failures": len(result.failures),
              "errors": len(result.errors), "passed": result.wasSuccessful(),
              "scope": "Real local TCP Modbus feedback frames and original Phase 7 ControlEngine; mocked Ignition and Java APIs",
              "not_verified": ["Ignition/Jython runtime", "actual OPC tag writes", "actual audit-profile persistence", "Perspective UI"]}
    artifacts = ROOT / "artifacts"
    artifacts.mkdir(exist_ok=True)
    (artifacts / "phase11_offline_verification.json").write_text(json.dumps(report, indent=2) + "\n")
    raise SystemExit(0 if result.wasSuccessful() else 1)
