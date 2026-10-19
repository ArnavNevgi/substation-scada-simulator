r"""Optional demo: keep Modbus and model updates alive but freeze the heartbeat.

Stop the normal server first, then:
  .\.venv\Scripts\python.exe .\phase8_freeze_demo.py --freeze-after 20
Stop this demo and restart phase7_modbus_server.py to restore normal behavior.
Uses the unchanged Phase 7 server and the same Modbus map.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import time

import phase7_modbus_server as phase7


class HeartbeatFreezeBridge(phase7.ControlBridge):
    freeze_after_s = 20.0

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.freeze_started_at = time.monotonic()
        self.freeze_announced = False

    async def update(self):
        old_heartbeat = self.heartbeat
        freeze = time.monotonic() - self.freeze_started_at >= self.freeze_after_s
        result = await super().update()
        if freeze:
            # No await between normal publication and this overwrite: a Modbus
            # request on the same event loop cannot read the intermediate word.
            self.heartbeat = old_heartbeat
            self.hr[1] = old_heartbeat
            if not self.freeze_announced:
                self.freeze_announced = True
                print("DEMO: heartbeat FROZEN; Modbus and other model updates continue.", flush=True)
        return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze-after", type=float, default=20.0,
                        help="Seconds after bridge creation before heartbeat freezes (default 20).")
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    if not math.isfinite(args.freeze_after) or args.freeze_after < 0:
        parser.error("--freeze-after must be finite and nonnegative")
    HeartbeatFreezeBridge.freeze_after_s = args.freeze_after
    phase7.ControlBridge = HeartbeatFreezeBridge
    try:
        asyncio.run(phase7.serve(port=args.port))
    except KeyboardInterrupt:
        print("Heartbeat-freeze demo stopped. Restart the normal Phase 7 server.")


if __name__ == "__main__":
    main()
