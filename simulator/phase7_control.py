"""Deterministic educational command, breaker and protection state machine.

No networking or electrical solver lives here. All times are monotonic seconds
supplied by the caller, so the operating sequence can be checked without sleeps.
"""

from dataclasses import dataclass
import math


COMMANDS = {"open": 1, "close": 2, "reset": 3, "pv-start": 4, "pv-stop": 5}
COMMAND_NAMES = {1: "Open CB1", 2: "Close CB1", 3: "Reset latches",
                 4: "Enable PV1", 5: "Stop PV1"}
STATUSES = {0: "IDLE", 1: "PENDING", 2: "APPLIED", 3: "REJECTED", 4: "FAILED"}
REASONS = {0: "None", 1: "Local mode", 2: "Fault active", 3: "Trip latched",
           4: "Grid/bus unavailable", 5: "Model invalid", 6: "Busy",
           7: "Invalid command", 8: "Out of range", 9: "Other interlock"}


@dataclass
class Movement:
    target_closed: bool
    due: float


class ControlEngine:
    """Single-writer state, advanced by the asyncio server's control task."""

    def __init__(self, config, fault_study):
        self.breaker_closed = bool(config["breaker"]["initial_closed"])
        self.operation_delay_s = float(config["breaker"]["operation_delay_s"])
        self.trip_delay_s = float(config["protection"]["trip_delay_s"])
        self.pickup_a = float(config["protection"]["overcurrent_pickup_hv_a"])
        if any(not math.isfinite(x) or x <= 0 for x in (
            self.operation_delay_s, self.trip_delay_s, self.pickup_a
        )):
            raise ValueError("Breaker delay, trip delay and pickup must be finite and positive.")
        self.fault_study = fault_study
        self.remote_mode = True
        self.grid_available = True
        self.fault_active = False
        self.trip_latched = False
        self.pv_tripped = not self.breaker_closed
        self.pv_enabled = self.breaker_closed  # Initial normal operation, as in Phase 5.
        self.model_valid = False
        self.bus_energized = False
        self.pv_running = False
        self.normal_current_a = None
        self.revision = 0
        self.last_sequence = 0
        self.ack_sequence = 0
        self.command_status = 0
        self.command_reason = 0
        self.pending_code = None
        self.movement = None
        self.overcurrent_since = None
        self.last_fault_ikss_ka = 0.0
        self.last_trip_at = None

    @property
    def fault_fed(self):
        return self.fault_active and self.breaker_closed and self.grid_available

    def invalidate(self):
        self.model_valid = False
        self.bus_energized = False
        self.pv_running = False
        self.normal_current_a = None

    def changed(self):
        """Invalidate a solved snapshot when its electrical inputs change."""
        self.revision += 1
        self.invalidate()

    def environment(self, *, fault=None, remote=None):
        if remote is not None:
            self.remote_mode = bool(remote)
        if fault is not None and bool(fault) != self.fault_active:
            self.fault_active = bool(fault)
            self.changed()
            if self.fault_fed:
                self.last_fault_ikss_ka = self.fault_study["bus_ikss_ka"]
                self._trip_pv()

    def accept_snapshot(self, snapshot, revision):
        if revision != self.revision or self.fault_fed:
            return False
        self.model_valid = True
        self.bus_energized = snapshot["state"] == "normal"
        self.pv_running = bool(snapshot["pv"]["running"])
        self.normal_current_a = snapshot["cb1_current_a"]
        return True

    def _trip_pv(self):
        if self.pv_enabled or not self.pv_tripped:
            self.pv_enabled = False
            self.pv_tripped = True
            self.changed()

    def _finish(self, status, reason=0):
        self.command_status, self.command_reason = status, reason
        self.pending_code = None

    def _close_reason(self):
        if not self.remote_mode:
            return 1
        if self.fault_active:
            return 2
        if self.trip_latched:
            return 3
        if not self.grid_available:
            return 4
        if not self.model_valid:
            return 5
        return 0

    def submit(self, code, sequence, now):
        """Acknowledge each new sequence once; False means transport-level busy.

        A duplicate most-recent sequence is an idempotent retry, even if the
        staged payload differs. A new sequence while pending is never queued.
        """
        if sequence == self.last_sequence:
            return True
        if self.command_status == 1:
            return False
        self.last_sequence = self.ack_sequence = sequence
        self.command_status, self.command_reason = 1, 0
        self.pending_code = code
        if not self.remote_mode:
            self._finish(3, 1)
        elif code not in COMMAND_NAMES:
            self._finish(3, 7)
        elif self.movement is not None:
            self._finish(3, 6)
        elif code == 1:
            if self.breaker_closed:
                self.movement = Movement(False, now + self.operation_delay_s)
            else:
                self._finish(2)
        elif code == 2:
            reason = self._close_reason()
            if reason:
                self._finish(3, reason)
            elif self.breaker_closed:
                self._finish(2)
            else:
                self.movement = Movement(True, now + self.operation_delay_s)
        elif code == 3:
            if self.fault_active:
                self._finish(3, 2)
            elif not self.model_valid:
                self._finish(3, 5)
            else:
                # Reset grants permission; it does not move CB1 or enable PV1.
                if self.trip_latched or self.pv_tripped:
                    self.trip_latched = self.pv_tripped = False
                    self.changed()
                self._finish(2)
        elif code == 4:
            reason = self._close_reason()
            if not reason and self.pv_tripped:
                reason = 3
            if not reason and (not self.breaker_closed or not self.bus_energized):
                reason = 4
            if reason:
                self._finish(3, reason)
            else:
                if not self.pv_enabled:
                    self.pv_enabled = True
                    self.changed()
                # Enable is applied; actual running also depends on sunlight/PQ.
                self._finish(2)
        elif code == 5:
            if self.pv_enabled:
                self.pv_enabled = False
                self.changed()
            self._finish(2)
        return True

    def _operate_overcurrent(self, now):
        self.trip_latched = True
        self.last_trip_at = now
        self._trip_pv()
        self.changed()
        if self.pending_code == 2:
            self._finish(4, 3)
            self.movement = None
        due = now + self.operation_delay_s
        if self.movement is not None and not self.movement.target_closed:
            due = min(due, self.movement.due)
        self.movement = Movement(False, due)

    def tick(self, now):
        if self.fault_fed:
            self.last_fault_ikss_ka = self.fault_study["bus_ikss_ka"]
            self._trip_pv()
        # A pending close loses permission if local mode, a fault or a trip
        # appears. The open operation, once accepted, is allowed to finish.
        if self.movement is not None and self.movement.target_closed:
            reason = self._close_reason()
            if reason:
                self.movement = None
                self._finish(4, reason)
        if self.movement is not None and now >= self.movement.due:
            target = self.movement.target_closed
            self.movement = None
            if target != self.breaker_closed:
                self.breaker_closed = target
                if not target and self.pv_enabled:
                    self._trip_pv()  # Grid-following PV cannot sustain the island.
                self.changed()
            if self.command_status == 1 and self.pending_code in (1, 2):
                self._finish(2)

        current = None
        if self.breaker_closed and self.grid_available:
            if self.fault_fed:
                current = self.fault_study["cb1_hv_current_a"]
            elif self.model_valid:
                current = self.normal_current_a
        if current is not None and current >= self.pickup_a:
            if self.overcurrent_since is None:
                self.overcurrent_since = now
            if not self.trip_latched and now - self.overcurrent_since >= self.trip_delay_s:
                self._operate_overcurrent(now)
        else:
            self.overcurrent_since = None

