"""Durable lesson progress and optimistic concurrency; no model credentials here."""

from __future__ import annotations

import json
import sqlite3
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import Field, JsonValue

from llm_test_lab.contracts import Contract, LearningPolicy


class LessonError(ValueError):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


class Prediction(Contract):
    own_closed: bool = Field(alias="ownClosed")
    foreign_disclosed: bool = Field(alias="foreignDisclosed")


class Job(Contract):
    running: bool = False
    id: str | None = None
    kind: Literal["baseline", "verify", "transfer"] | None = None
    error: str | None = None
    cancel_requested: bool = Field(default=False, alias="cancelRequested")


class LessonState(Contract):
    schema_version: Literal[1] = Field(default=1, alias="schemaVersion")
    id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    revision: int = 0
    stage: Literal[
        "prediction", "evidence", "policy", "verification", "transfer", "reflection", "complete"
    ] = "prediction"
    mode: Literal["fixture"] = "fixture"
    prediction: Prediction | None = None
    diagnosis: dict[str, JsonValue] | None = None
    policy: LearningPolicy | None = None
    baseline: dict[str, JsonValue] | None = None
    verification: dict[str, JsonValue] | None = None
    transfer: dict[str, JsonValue] | None = None
    reflection: dict[str, JsonValue] | None = None
    job: Job = Field(default_factory=Job)
    message: str | None = None
    history: list[dict[str, JsonValue]] = Field(default_factory=list)
    created_at: str = Field(
        default_factory=lambda: datetime.now(UTC).isoformat(), alias="createdAt"
    )


class SessionStore:
    def __init__(self, root: Path):
        self.path = root / ".lab-state/learning/progress.sqlite"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(
                "CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, state TEXT NOT NULL); CREATE TABLE IF NOT EXISTS active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), session_id TEXT NOT NULL);"
            )
            if db.execute("SELECT session_id FROM active WHERE singleton=1").fetchone() is None:
                initial = LessonState()
                db.execute("INSERT INTO sessions VALUES (?,?)", (initial.id, self._encode(initial)))
                db.execute("INSERT INTO active VALUES (1,?)", (initial.id,))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=3, isolation_level=None)
        try:
            db.execute("PRAGMA busy_timeout=3000")
            yield db
        finally:
            db.close()

    @staticmethod
    def _encode(state: LessonState) -> str:
        return json.dumps(state.to_dict(), ensure_ascii=False, allow_nan=False)

    @staticmethod
    def _read(db: sqlite3.Connection) -> LessonState:
        row = db.execute(
            "SELECT s.state FROM sessions s JOIN active a ON s.id=a.session_id WHERE a.singleton=1"
        ).fetchone()
        if row is None:
            raise LessonError(
                "STATE_MISSING", "Сессия не найдена. Перезапустите локальную лабораторию.", 500
            )
        return LessonState.model_validate_json(row[0])

    def get(self) -> LessonState:
        with self._connect() as db:
            return self._read(db)

    def update(
        self,
        revision: int | None,
        change: Callable[[LessonState], None],
        *,
        session_id: str | None = None,
    ) -> LessonState:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                state = self._read(db)
                if session_id is not None and state.id != session_id:
                    raise LessonError(
                        "STALE_SESSION", "Сессия уже сменилась. Обновите страницу.", 409
                    )
                if revision is not None and state.revision != revision:
                    raise LessonError(
                        "STALE_REVISION",
                        "Результат уже изменился. Страница обновит состояние; повторный запуск не создан.",
                        409,
                    )
                change(state)
                state.revision += 1
                # Validate again before the durable write; malformed state is never accepted.
                state = LessonState.model_validate(state.to_dict())
                db.execute(
                    "UPDATE sessions SET state=? WHERE id=?", (self._encode(state), state.id)
                )
                db.execute("COMMIT")
                return state
            except BaseException:
                db.execute("ROLLBACK")
                raise

    def reset(self, revision: int) -> LessonState:
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                old = self._read(db)
                if old.revision != revision or old.job.running:
                    raise LessonError(
                        "BUSY_OR_STALE", "Сначала дождитесь завершения или отмены опыта.", 409
                    )
                # Revisions are monotonic across resets, so an old tab cannot
                # accidentally address a new session with the same revision.
                state = LessonState(revision=old.revision + 1)
                db.execute("INSERT INTO sessions VALUES (?,?)", (state.id, self._encode(state)))
                db.execute("UPDATE active SET session_id=? WHERE singleton=1", (state.id,))
                db.execute("COMMIT")
                return state
            except BaseException:
                db.execute("ROLLBACK")
                raise
