# Project Library module: phase7_controls (replace its contents, not its name).
# Ignition 8.3 / Jython 2.7. Do not run this file with Windows Python.
# Writes still use the existing Ignition tags, code first and sequence last.
# Feedback uses one read-only FC03 response for HR19..21 of the Phase 7 server.

import socket
import struct
from java.lang import Exception as JavaException
from java.lang import System, Thread
from java.util import UUID
from java.util.concurrent.locks import ReentrantLock

AUDIT_PROFILE = "SubstationAudit"
FEEDBACK_HOST = "127.0.0.1"
FEEDBACK_PORT = 1502
FEEDBACK_UNIT = 1
SOCKET_TIMEOUT_S = 1.0
RESULT_TIMEOUT_S = 12.0
POLL_MS = 200
BASE = "[default]Substation/"
VERSION = "11.1"

_ERRORS = (Exception, JavaException)
_lock = ReentrantLock()
_last_sequence = None
_active = None
_unresolved = None
_names = {1: "OPEN CB1", 2: "CLOSE CB1", 3: "RESET TRIPS",
          4: "START PV1", 5: "STOP PV1"}
_targets = {1: "Substation/CB1", 2: "Substation/CB1",
            3: "Substation/Protection", 4: "Substation/PV1",
            5: "Substation/PV1"}
_reasons = {0: "None", 1: "Local mode", 2: "Fault active",
            3: "Trip latched", 4: "Grid/bus unavailable", 5: "Model invalid",
            6: "Busy", 7: "Invalid command", 8: "Out of range",
            9: "Other interlock"}
_terminal = {2: "APPLIED", 3: "REJECTED", 4: "FAILED"}


def _context(code, source=None):
    ctx = {"rid": str(UUID.randomUUID()), "cmd": code, "seq": None,
           "target": _targets.get(code, "Substation/Controls"),
           "actor": None, "host": None, "session": None}
    if source is not None:
        # These are built-in Perspective session properties, not custom fields.
        props = source.session.props
        ctx["session"] = str(props.id)
        ctx["host"] = str(props.host)
        ctx["actor"] = "anonymous"
        if props.auth.authenticated and props.auth.user.userName:
            ctx["actor"] = str(props.auth.user.userName)
    return ctx


def _audit(ctx, phase, reason=None, detail=None):
    """False means the audit API failed. It never changes a device outcome."""
    payload = {"rid": ctx["rid"], "seq": ctx["seq"], "cmd": ctx["cmd"]}
    if reason is not None:
        payload["reason"] = int(reason)
    if detail:
        payload["detail"] = str(detail)[:96]
    try:
        value = system.util.jsonEncode(payload)
        # Also fit database profiles with a conventional VARCHAR(255) value.
        while len(value) > 240 and payload.get("detail"):
            payload["detail"] = payload["detail"][:-8]
            value = system.util.jsonEncode(payload)
        origin = ["feature", "phase11", "request", ctx["rid"]]
        if ctx.get("session"):
            origin += ["session", ctx["session"]]
        args = {"action": "COMMAND_" + phase,
                "actionTarget": ctx["target"], "actionValue": value,
                "auditProfile": AUDIT_PROFILE, "originatingSystem": origin,
                "eventTimestamp": system.date.now()}
        if ctx.get("actor") is not None:
            args["actor"] = ctx["actor"]
        if ctx.get("host") is not None:
            args["actorHost"] = ctx["host"]
        system.util.audit(**args)
        return True
    except _ERRORS as error:
        try:
            system.util.getLogger("Substation.Phase11").error(
                "Audit write failed for %s / %s: %s" %
                (ctx["rid"], phase, error))
        except _ERRORS:
            pass
        return False


class _Feedback(object):
    """Strict Modbus TCP reader; has no write operation."""
    def __init__(self):
        self.sock = None
        self.transaction = 0

    def close(self):
        if self.sock is not None:
            try:
                self.sock.close()
            finally:
                self.sock = None

    def _receive(self, count):
        chunks = []
        remaining = count
        while remaining:
            chunk = self.sock.recv(remaining)
            if not chunk:
                raise IOError("Feedback connection closed before frame completed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    def read(self):
        try:
            if self.sock is None:
                self.sock = socket.create_connection(
                    (FEEDBACK_HOST, FEEDBACK_PORT), SOCKET_TIMEOUT_S)
                self.sock.settimeout(SOCKET_TIMEOUT_S)
            self.transaction = (self.transaction + 1) % 65536
            # MBAP: transaction, protocol=0, length=6, unit; FC03, start, count.
            request = struct.pack(
                ">HHHBBHH", self.transaction, 0, 6, FEEDBACK_UNIT, 3, 19, 3)
            self.sock.sendall(request)
            transaction, protocol, length, unit = struct.unpack(
                ">HHHB", self._receive(7))
            if (transaction != self.transaction or protocol != 0 or
                    unit != FEEDBACK_UNIT or not 2 <= length <= 254):
                raise IOError("Invalid or mismatched Modbus feedback header")
            pdu = self._receive(length - 1)
            function = ord(pdu[0:1])
            if function == 0x83 and len(pdu) == 2:
                raise IOError("Modbus feedback exception %d" % ord(pdu[1:2]))
            if function != 3 or len(pdu) != 8 or ord(pdu[1:2]) != 6:
                raise IOError("Invalid Modbus feedback function/length/byte count")
            status, reason, sequence = struct.unpack(">HHH", pdu[2:])
            if status not in (0, 1, 2, 3, 4) or reason not in _reasons:
                raise IOError("Invalid command feedback status or reason")
            if status == 2 and reason != 0:
                raise IOError("APPLIED feedback has a nonzero reason")
            return status, reason, sequence
        except _ERRORS:
            self.close()
            raise


def _write(path, value):
    qualities = system.tag.writeBlocking([BASE + path], [value], 5000)
    if len(qualities) != 1 or not qualities[0].isGood():
        raise IOError("Tag write failed: " + path + " / " + str(qualities))


def _not_sent(ctx, detail):
    recorded = _audit(ctx, "NOT_SENT", detail=detail)
    return "Not sent: %s.%s" % (
        detail, "" if recorded else " Audit write failed; check Gateway logs.")


def _finish(ctx, phase, reason=None, detail=None):
    global _active, _unresolved
    recorded = _audit(ctx, phase, reason, detail)
    _lock.lock()
    try:
        if _active is not None and _active["rid"] == ctx["rid"]:
            _active = None
        if phase == "UNKNOWN":
            _unresolved = dict(ctx)
    finally:
        _lock.unlock()
    suffix = "" if recorded else " AUDIT WRITE FAILED; check Gateway logs."
    if phase == "UNKNOWN":
        return ("UNKNOWN #%d: %s. No automatic retry. Reconcile feedback before "
                "another equipment command.%s") % (ctx["seq"], detail, suffix)
    return "%s #%d: %s / %s. Check equipment feedback.%s" % (
        phase, ctx["seq"], _names[ctx["cmd"]],
        _reasons.get(reason, "Unknown reason"), suffix)


def _wait_result(ctx, reader, previous_ack):
    deadline = System.nanoTime() + int(RESULT_TIMEOUT_S * 1000000000)
    last_error = "Matching terminal feedback not observed"
    while System.nanoTime() < deadline:
        try:
            status, reason, ack = reader.read()
            if ack == ctx["seq"] and status in _terminal:
                return _finish(ctx, _terminal[status], reason)
            if ack not in (previous_ack, ctx["seq"]):
                return _finish(ctx, "UNKNOWN", detail=
                               "Feedback superseded by another command writer")
        except _ERRORS as error:
            last_error = str(error)
        Thread.sleep(POLL_MS)
    return _finish(ctx, "UNKNOWN", detail=last_error)


def send_command(code, source=None):
    """Called by existing buttons; pass self as source for explicit identity.

    One writer only. CLI submissions must not run alongside browser controls.
    The function waits at most RESULT_TIMEOUT_S plus individual I/O timeouts.
    """
    global _last_sequence, _active
    if code not in _names:
        return _not_sent(_context(-1, source), "Unknown command")
    ctx = _context(code, source)
    if not _lock.tryLock():
        return _not_sent(ctx, "Another submission is being staged")
    reader = _Feedback()
    staged = False
    commit_attempted = False
    previous_ack = None
    try:
        if _active is not None:
            return _not_sent(ctx, "Another command is awaiting its outcome")
        if _unresolved is not None:
            return _not_sent(ctx, "An UNKNOWN command requires reconciliation")
        values = system.tag.readBlocking([
            BASE + "Commands/Command_Code",
            BASE + "Commands/Command_Sequence"
        ], 1500)
        if len(values) != 2 or not all(v.quality.isGood() for v in values):
            return _not_sent(ctx, "Command tags have bad quality")
        status, reason, previous_ack = reader.read()
        if status == 1:
            return _not_sent(ctx, "The simulator reports a pending command")
        sequence = (previous_ack + 1) % 65536
        if sequence == _last_sequence:
            sequence = (sequence + 1) % 65536
        ctx["seq"] = sequence
        if not _audit(ctx, "REQUESTED"):
            return "Not sent: request audit failed. Check Gateway logs."
        _active = dict(ctx)
        staged = True
        _write("Commands/Command_Code", int(code))
        # Keep the required payload-then-commit ordering; never retry a write.
        _last_sequence = sequence
        commit_attempted = True
        _write("Commands/Command_Sequence", sequence)
    except _ERRORS as error:
        if not commit_attempted:
            if staged:
                _active = None
            return _not_sent(ctx, str(error))
        # A timed-out/bad commit write may still have reached the simulator.
        # Read its actual feedback; do not assume failure or resend the write.
        _audit(ctx, "WRITE_UNCERTAIN", detail=str(error))
    finally:
        _lock.unlock()
        if not commit_attempted:
            reader.close()
    try:
        return _wait_result(ctx, reader, previous_ack)
    except _ERRORS as error:
        return _finish(ctx, "UNKNOWN", detail=str(error))
    finally:
        reader.close()


def reconcile_pending(source=None):
    """Read-only reconciliation. Never sends/resends an equipment command."""
    global _unresolved
    if not _lock.tryLock():
        return "Busy: another submission is being staged."
    reader = _Feedback()
    try:
        if _unresolved is None:
            return "No UNKNOWN command is awaiting reconciliation."
        status, reason, ack = reader.read()
        if ack != _unresolved["seq"] or status not in _terminal:
            return "Still unresolved: matching terminal feedback is unavailable."
        ctx = dict(_unresolved)
        # The actor here is the person performing reconciliation, when supplied.
        resolver = _context(ctx["cmd"], source)
        for key in ("actor", "host", "session"):
            ctx[key] = resolver[key]
        phase = "RECONCILED_" + _terminal[status]
        if not _audit(ctx, phase, reason):
            return "Feedback found, but reconciliation audit failed."
        _unresolved = None
        return "%s #%d / %s. No command was resent." % (
            phase, ack, _reasons[reason])
    finally:
        reader.close()
        _lock.unlock()


def set_simulation(name, value):
    """Preserve the existing fault/local/remote simulation-button interface.

    Automatic project auditing records these supported tag writes. Their write
    completion is not presented as an equipment-command outcome.
    """
    if name not in ("Fault_Enable", "Remote_Mode_Set") or value not in (0, 1):
        return "Not sent: invalid simulation input."
    if not _lock.tryLock():
        return "Another submission is being staged."
    try:
        _write("Inputs/" + name, int(value))
        return "Simulation input accepted: %s = %d. Check status feedback." % (
            name, value)
    except _ERRORS as error:
        return "Simulation input error: %s" % error
    finally:
        _lock.unlock()


def history_rows():
    """Perspective Table data: explicit command records from the last 24 hours."""
    now = system.date.now()
    data = system.util.queryAuditLog(
        auditProfileName=AUDIT_PROFILE,
        startDate=system.date.addHours(now, -24), endDate=now)
    names = [str(n) for n in data.getColumnNames()]
    columns = dict(("".join(c for c in n.lower() if c.isalnum()), i)
                   for i, n in enumerate(names))
    if "eventtimestamp" not in columns and "timestamp" in columns:
        columns["eventtimestamp"] = columns["timestamp"]
    required = ("eventtimestamp", "actor", "action", "actiontarget", "actionvalue")
    if not all(n in columns for n in required):
        raise ValueError("Unexpected audit columns: " + str(names))
    rows = []
    for r in range(data.getRowCount()):
        def get(name):
            return data.getValueAt(r, columns[name])
        action = str(get("action"))
        if not action.startswith("COMMAND_"):
            continue
        try:
            payload = system.util.jsonDecode(str(get("actionvalue")))
        except _ERRORS:
            payload = {}
        stamp = get("eventtimestamp")
        reason_code = payload.get("reason")
        reason = _reasons.get(reason_code, "")
        if payload.get("detail"):
            reason = str(payload["detail"])
        rows.append({"Time": system.date.format(stamp, "yyyy-MM-dd HH:mm:ss.SSS Z"),
                     "Actor": str(get("actor") or ""),
                     "Command": _names.get(payload.get("cmd"), "Unknown command"),
                     "Sequence": payload.get("seq"),
                     "Event": action[len("COMMAND_"):],
                     "Reason": reason, "RequestID": payload.get("rid", ""),
                     "Target": str(get("actiontarget") or ""),
                     "_epoch": system.date.toMillis(stamp)})
    rows.sort(key=lambda row: row["_epoch"], reverse=True)
    result = rows[:200]
    for row in result:
        del row["_epoch"]
    return result


def check_installation():
    """Read-only check of the module, feedback endpoint and audit profile."""
    reader = _Feedback()
    try:
        status, reason, ack = reader.read()
        now = system.date.now()
        data = system.util.queryAuditLog(
            auditProfileName=AUDIT_PROFILE,
            startDate=system.date.addHours(now, -24), endDate=now)
        return {"module_version": VERSION, "audit_rows": data.getRowCount(),
                "feedback_endpoint": "%s:%d/unit%d" %
                (FEEDBACK_HOST, FEEDBACK_PORT, FEEDBACK_UNIT),
                "ack_sequence": ack, "command_status": status,
                "command_reason": _reasons[reason]}
    finally:
        reader.close()