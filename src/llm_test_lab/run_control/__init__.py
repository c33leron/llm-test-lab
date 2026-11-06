"""Durable, process-independent admission and mutation cancellation boundary."""

from __future__ import annotations

import contextlib
import json
import os
import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

LEASE_SECONDS = 10.0
CLEANUP_GRACE_SECONDS = 5.0


class ControlError(RuntimeError):
    code = "RUN_CONTEXT_INVALID"


class RunCancelled(ControlError):
    code = "RUN_CANCELLED"


class OwnerLost(ControlError):
    code = "RUN_INTERRUPTED"


class BudgetExceeded(ControlError):
    code = "BUDGET_EXCEEDED"


def process_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


class RunControl:
    """One connection per operation; all authority is in the durable approved plan.

    A control transaction is held across each short domain mutation. Thus cancel
    and commit have an actual serial order, rather than a check-then-write race.
    No transaction is held during inference.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()

    @classmethod
    def create(cls, path: Path, plan: dict[str, Any], owner_pid: int) -> RunControl:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Exclusive creation prevents a second controller from resetting claims.
        with path.open("xb"):
            pass
        control = cls(path)
        with control._connection() as conn:
            conn.executescript("""
                CREATE TABLE run (id TEXT PRIMARY KEY, plan_hash TEXT NOT NULL,
                  status TEXT NOT NULL, owner_pid INTEGER NOT NULL, heartbeat REAL NOT NULL,
                  budget INTEGER NOT NULL, reserved INTEGER NOT NULL DEFAULT 0,
                  reason TEXT, pgid INTEGER);
                CREATE TABLE cells (id TEXT PRIMARY KEY, cell TEXT NOT NULL,
                  state TEXT NOT NULL DEFAULT 'not_run', envelope TEXT);
                CREATE TABLE reservations (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  attempt_id TEXT NOT NULL REFERENCES cells(id), time REAL NOT NULL);
            """)
            conn.execute(
                "INSERT INTO run(id,plan_hash,status,owner_pid,heartbeat,budget) VALUES(?,?,?,?,?,?)",
                (
                    plan["runId"],
                    plan["planHash"],
                    "running",
                    owner_pid,
                    time.monotonic(),
                    plan["maxInferenceCalls"],
                ),
            )
            conn.executemany(
                "INSERT INTO cells(id,cell) VALUES(?,?)",
                [(c["attemptId"], json.dumps(c, ensure_ascii=False)) for c in plan["cells"]],
            )
        return control

    @contextlib.contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        if not self.path.is_file():
            raise ControlError("managed run control is missing")
        conn = sqlite3.connect(self.path, isolation_level=None, timeout=1.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=1000")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
        finally:
            conn.close()

    @contextlib.contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    def _assert_active(self, conn: sqlite3.Connection, attempt_id: str | None = None) -> None:
        row = conn.execute("SELECT * FROM run").fetchone()
        if row is None:
            raise ControlError("run does not exist")
        if row["status"] == "cancelled":
            if row["reason"] == "owner lease lost":
                raise OwnerLost("CLI owner lease was lost")
            raise RunCancelled("managed run was cancelled")
        if row["status"] != "running":
            raise ControlError("managed run is not active; viewer cannot rerun it")
        if (
            not process_alive(row["owner_pid"])
            or time.monotonic() - row["heartbeat"] > LEASE_SECONDS
        ):
            raise OwnerLost("CLI owner is lost or its lease expired")
        if attempt_id is not None:
            cell = conn.execute("SELECT state FROM cells WHERE id=?", (attempt_id,)).fetchone()
            if cell is None or cell["state"] != "running":
                raise ControlError("attempt has not been admitted or is already finalized")

    def check(self, attempt_id: str) -> None:
        with self._connection() as conn:
            self._assert_active(conn, attempt_id)

    def claim(self, attempt_id: str) -> bool:
        with self._transaction() as conn:
            self._assert_active(conn)
            row = conn.execute("SELECT state FROM cells WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ControlError("attempt is absent from the approved plan")
            if row["state"] != "not_run":
                return False
            if conn.execute("SELECT 1 FROM cells WHERE state='running'").fetchone():
                raise ControlError("only one attempt may run at a time")
            conn.execute("UPDATE cells SET state='running' WHERE id=?", (attempt_id,))
            return True

    def reserve(self, attempt_id: str) -> None:
        with self._transaction() as conn:
            self._assert_active(conn, attempt_id)
            row = conn.execute("SELECT reserved,budget FROM run").fetchone()
            assert row is not None
            if row["reserved"] >= row["budget"]:
                raise BudgetExceeded("run-wide inference-call budget exhausted")
            conn.execute("UPDATE run SET reserved=reserved+1")
            conn.execute(
                "INSERT INTO reservations(attempt_id,time) VALUES(?,?)",
                (attempt_id, time.monotonic()),
            )

    @contextlib.contextmanager
    def mutation_guard(self, attempt_id: str) -> Iterator[None]:
        with self._transaction() as conn:
            self._assert_active(conn, attempt_id)
            yield

    def heartbeat(self) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE run SET heartbeat=? WHERE owner_pid=? AND status='running'",
                (time.monotonic(), os.getpid()),
            )

    def set_process_group(self, pgid: int) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE run SET pgid=?", (pgid,))

    def cancel(self, reason: str = "user cancellation") -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE run SET status='cancelled',reason=? WHERE status='running'", (reason,)
            )

    def finish(self, attempt_id: str, envelope: dict[str, Any]) -> None:
        with self._transaction() as conn:
            conn.execute(
                "UPDATE cells SET state=?,envelope=? WHERE id=? AND state='running'",
                (envelope["terminalState"], json.dumps(envelope, ensure_ascii=False), attempt_id),
            )

    def finalize_run(self, status: str) -> None:
        with self._transaction() as conn:
            conn.execute("UPDATE cells SET state='interrupted' WHERE state='running'")
            conn.execute("UPDATE run SET status=?", (status,))

    def record_recovery(self, attempt_id: str, envelope: dict[str, Any]) -> None:
        if (
            envelope.get("terminalState") != "interrupted"
            or envelope.get("attemptId") != attempt_id
        ):
            raise ControlError("recovery must preserve interrupted cell identity")
        with self._transaction() as conn:
            conn.execute(
                "UPDATE cells SET envelope=? WHERE id=? AND state='interrupted'",
                (json.dumps(envelope, ensure_ascii=False), attempt_id),
            )

    def snapshot(self) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM run").fetchone()
            if row is None:
                raise ControlError("run does not exist")
            result = dict(row)
            result["cells"] = [dict(c) for c in conn.execute("SELECT * FROM cells ORDER BY rowid")]
            return result

    def cell(self, attempt_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT cell FROM cells WHERE id=?", (attempt_id,)).fetchone()
            if row is None:
                raise ControlError("attempt is absent from approved plan")
            return json.loads(row["cell"])  # type: ignore[no-any-return]
