"""Independent lease monitor, including while the CLI/worker is stuck in HTTP."""

from __future__ import annotations

import os
import signal
import sys
import time
from pathlib import Path

from . import CLEANUP_GRACE_SECONDS, LEASE_SECONDS, RunControl, process_alive


def kill_group(pgid: int, sig: int) -> None:
    if pgid <= 1 or pgid == os.getpgrp():
        return
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def monitor(path: Path) -> None:
    control = RunControl(path)
    while True:
        snapshot = control.snapshot()
        if snapshot["status"] not in {"running", "cancelled"}:
            return
        lost = (
            not process_alive(snapshot["owner_pid"])
            or time.monotonic() - snapshot["heartbeat"] > LEASE_SECONDS
        )
        if lost:
            control.cancel("owner lease lost")
            pgid = snapshot["pgid"]
            if pgid:
                # Allow cancellation watcher in Python to save evidence first.
                time.sleep(CLEANUP_GRACE_SECONDS)
                kill_group(pgid, signal.SIGTERM)
                time.sleep(0.5)
                kill_group(pgid, signal.SIGKILL)
            control.finalize_run("partial")
            return
        time.sleep(0.2)


if __name__ == "__main__":
    monitor(Path(sys.argv[1]))
