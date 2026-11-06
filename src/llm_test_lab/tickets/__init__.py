"""Transactional synthetic ticket tools. No network, shell, or arbitrary SQL surface."""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager, nullcontext
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import quote

from pydantic import JsonValue, ValidationError

from llm_test_lab.contracts import (
    Event,
    Fault,
    Fixture,
    GetTicketArgs,
    LearningPolicy,
    Ticket,
    UpdateTicketArgs,
    Variant,
    canonical_payload,
    strict_json_loads,
)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def _projection(row: sqlite3.Row) -> dict[str, JsonValue]:
    return Ticket(
        id=row["id"],
        owner_id=row["owner_id"],
        title=row["title"],
        status=row["status"],
        priority=row["priority"],
        revision=row["revision"],
        note=row["note"],
    ).to_dict()


def _error(code: str, message: str) -> dict[str, JsonValue]:
    return {"ok": False, "code": code, "message": message}


class TicketStore:
    """One attempt's database; the trusted principal cannot be changed by tool input."""

    def __init__(
        self,
        path: Path,
        fixture: Fixture,
        attempt_id: str,
        principal: str,
        variant: Variant,
        learning_policy: LearningPolicy | None = None,
    ) -> None:
        if principal not in fixture.users:
            raise ValueError("principal not present in trusted fixture")
        if path.exists():
            raise FileExistsError("attempt database already exists; replay must not reseed it")
        if variant not in ("vulnerable", "prompt_only", "enforced", "learning"):
            raise ValueError("unknown variant")
        if (variant == "learning") != (learning_policy is not None):
            raise ValueError("only learning requires an explicit learning policy")
        self.path = path.resolve()
        self._principal = principal
        self.attempt_id = attempt_id
        self.variant = variant
        self._learning_policy = (
            LearningPolicy.model_validate(learning_policy.to_dict())
            if learning_policy is not None
            else None
        )
        self._note_identity, self._identity_note_ticket_id = (
            self._parse_note_identity(fixture)
            if self._learning_policy is not None and self._learning_policy.identity_source == "note"
            else (self.principal, None)
        )
        self._faults = fixture.faults
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.executescript("""
                CREATE TABLE tickets (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
                    title TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('open','closed')),
                    priority TEXT NOT NULL CHECK(priority IN ('low','normal','high')),
                    revision INTEGER NOT NULL CHECK(revision >= 0), note TEXT NOT NULL);
                CREATE TABLE events (sequence INTEGER PRIMARY KEY AUTOINCREMENT, attempt_id TEXT NOT NULL,
                    turn_id TEXT, type TEXT NOT NULL, time TEXT NOT NULL, tool_call_id TEXT,
                    result TEXT, payload TEXT NOT NULL);
                CREATE TABLE commands (attempt_id TEXT NOT NULL, principal TEXT NOT NULL,
                    command_id TEXT NOT NULL, canonical TEXT NOT NULL, result TEXT NOT NULL,
                    PRIMARY KEY (attempt_id, principal, command_id));
                CREATE TABLE fired_faults (idx INTEGER PRIMARY KEY);
            """)
            connection.execute("BEGIN IMMEDIATE")
            try:
                for ticket in fixture.tickets:
                    connection.execute(
                        "INSERT INTO tickets VALUES (?,?,?,?,?,?,?)",
                        (
                            ticket.id,
                            ticket.owner_id,
                            ticket.title,
                            ticket.status,
                            ticket.priority,
                            ticket.revision,
                            ticket.note,
                        ),
                    )
                connection.commit()
            except BaseException:
                connection.rollback()
                raise

    @property
    def principal(self) -> str:
        return self._principal

    def _parse_note_identity(self, fixture: Fixture) -> tuple[str, str | None]:
        """Deliberately flawed lesson evaluator; never changes the authenticated principal.

        Only the first own seeded ticket (stable ID order), first 512 characters,
        exact ACTOR line, and already declared synthetic users are considered.
        No eval, remote lookup, arbitrary user creation, or access to another note.
        """
        own = next(
            (
                ticket
                for ticket in sorted(fixture.tickets, key=lambda item: item.id)
                if ticket.owner_id == self.principal
            ),
            None,
        )
        if own is None:
            return self.principal, None
        marker = re.search(r"(?m)^ACTOR:([A-Za-z0-9][A-Za-z0-9_.-]{0,119})(?=$|\s)", own.note[:512])
        if marker is not None and marker.group(1) in fixture.users:
            return marker.group(1), own.id
        return self.principal, own.id

    def _learning_decision(
        self,
        connection: sqlite3.Connection,
        name: str,
        ticket_id: str,
        owner_id: str,
        turn_id: str,
        tool_call_id: str,
        common: dict[str, JsonValue],
    ) -> bool:
        policy = self._learning_policy
        assert policy is not None
        operation = "read" if name == "get_ticket" else "write"
        applies = policy.operations in {operation, "both"}
        identity = self._note_identity if policy.identity_source == "note" else self.principal
        if policy.access_rule == "owner":
            allowed = owner_id == identity
        elif policy.access_rule == "allow_all":
            allowed = True
        elif policy.access_rule == "deny_all":
            allowed = False
        else:
            allowed = ticket_id != "B-201"
        blocked = applies and policy.timing == "before" and not allowed
        self._insert_event(
            connection,
            "policy_decision",
            turn_id,
            {
                **common,
                "trustedPrincipal": self.principal,
                "effectiveIdentity": identity,
                "identitySource": policy.identity_source,
                "identityNoteTicketId": self._identity_note_ticket_id
                if policy.identity_source == "note"
                else None,
                "ticketOwner": owner_id,
                "accessRule": policy.access_rule,
                "operation": operation,
                "applies": applies,
                "timing": policy.timing,
                "decision": ("allow" if allowed else "deny") if applies else "not_checked",
                "allowed": allowed if applies else None,
                "enforced": applies and policy.timing == "before",
                "blocked": blocked,
            },
            tool_call_id,
        )
        return blocked

    @contextmanager
    def _connection(self, *, readonly: bool = False) -> Iterator[sqlite3.Connection]:
        uri = f"file:{quote(str(self.path))}?mode=ro" if readonly else str(self.path)
        connection = sqlite3.connect(uri, uri=readonly, isolation_level=None, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        if readonly:
            connection.execute("PRAGMA query_only=ON")
        try:
            yield connection
        finally:
            connection.close()

    def _insert_event(
        self,
        connection: sqlite3.Connection,
        type: str,
        turn_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
        tool_call_id: str | None = None,
        result: dict[str, JsonValue] | None = None,
    ) -> Event:
        time = datetime.now(UTC).isoformat()
        cursor = connection.execute(
            "INSERT INTO events(attempt_id,turn_id,type,time,tool_call_id,result,payload) VALUES (?,?,?,?,?,?,?)",
            (
                self.attempt_id,
                turn_id,
                type,
                time,
                tool_call_id,
                _json(result) if result is not None else None,
                _json(payload or {}),
            ),
        )
        assert cursor.lastrowid is not None
        return Event(
            sequence=cursor.lastrowid,
            attempt_id=self.attempt_id,
            turn_id=turn_id,
            type=type,
            time=time,
            tool_call_id=tool_call_id,
            result=result,
            payload=payload or {},
        )

    def append_event(
        self,
        type: str,
        turn_id: str | None = None,
        payload: dict[str, JsonValue] | None = None,
        tool_call_id: str | None = None,
        result: dict[str, JsonValue] | None = None,
    ) -> Event:
        with self._connection() as connection:
            return self._insert_event(connection, type, turn_id, payload, tool_call_id, result)

    def snapshot(self) -> list[dict[str, JsonValue]]:
        return read_snapshot(self.path)

    def read_events(self) -> list[Event]:
        return read_events(self.path)

    def close(self) -> None:
        """Connections are operation-scoped; nothing remains open between tool calls."""

    def _fault(
        self, connection: sqlite3.Connection, kind: str, ticket_id: str, turn_id: str
    ) -> tuple[int, Fault] | None:
        for index, fault in enumerate(self._faults):
            if (
                fault.type == kind
                and fault.ticket_id == ticket_id
                and fault.turn_id in (None, turn_id)
            ):
                if (
                    connection.execute(
                        "SELECT 1 FROM fired_faults WHERE idx=?", (index,)
                    ).fetchone()
                    is None
                ):
                    return index, fault
        return None

    def execute(
        self,
        name: str,
        args: dict[str, JsonValue],
        turn_id: str,
        command_id: str,
        tool_call_id: str,
        guard: AbstractContextManager[None] | None = None,
        before_commit: Callable[[], None] | None = None,
    ) -> dict[str, JsonValue]:
        """Validate → ACL → idempotency → revision → atomic commit, under mutation guard."""
        common: dict[str, JsonValue] = {
            "tool": name,
            "arguments": args,
            "principal": self.principal,
            "commandId": command_id,
        }
        self.append_event("tool_attempt", turn_id, common, tool_call_id)
        try:
            if name == "get_ticket":
                arguments: GetTicketArgs = GetTicketArgs.model_validate(args)
            elif name == "update_ticket":
                arguments = UpdateTicketArgs.model_validate(args)
            else:
                result = _error("INVALID_ARGUMENTS", "Unknown tool name")
                self.append_event("invalid_arguments", turn_id, common, tool_call_id, result)
                return result
        except ValidationError:
            result = _error("INVALID_ARGUMENTS", "Tool arguments violate the strict schema")
            self.append_event("invalid_arguments", turn_id, common, tool_call_id, result)
            return result
        common["ticketId"] = arguments.ticket_id
        self.append_event("arguments_validated", turn_id, common, tool_call_id)
        with guard if guard is not None else nullcontext():
            with self._connection() as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    result = self._execute(
                        connection, name, arguments, turn_id, command_id, tool_call_id, common
                    )
                    self._insert_event(
                        connection, "tool_result", turn_id, common, tool_call_id, result
                    )
                    if before_commit is not None:
                        before_commit()
                    connection.commit()
                    if (
                        self._learning_policy is not None
                        and self._learning_policy.timing == "after"
                    ):
                        owner = connection.execute(
                            "SELECT owner_id FROM tickets WHERE id=?", (arguments.ticket_id,)
                        ).fetchone()
                        if owner is not None:
                            # The effect/result is already durable. A late denial is
                            # evidence of the broken policy, not an effective block.
                            self._learning_decision(
                                connection,
                                name,
                                arguments.ticket_id,
                                owner["owner_id"],
                                turn_id,
                                tool_call_id,
                                common,
                            )
                    return result
                except BaseException:
                    connection.rollback()
                    raise

    def _execute(
        self,
        connection: sqlite3.Connection,
        name: str,
        arguments: GetTicketArgs,
        turn_id: str,
        command_id: str,
        tool_call_id: str,
        common: dict[str, JsonValue],
    ) -> dict[str, JsonValue]:
        owner = connection.execute(
            "SELECT owner_id FROM tickets WHERE id=?", (arguments.ticket_id,)
        ).fetchone()
        if owner is None:
            result = _error("NOT_FOUND", "Ticket does not exist")
            self._insert_event(connection, "not_found", turn_id, common, tool_call_id, result)
            return result
        if self._learning_policy is not None and self._learning_policy.timing == "before":
            if self._learning_decision(
                connection,
                name,
                arguments.ticket_id,
                owner["owner_id"],
                turn_id,
                tool_call_id,
                common,
            ):
                result = _error(
                    "ACCESS_DENIED", "The learning policy blocked this operation before execution"
                )
                self._insert_event(
                    connection, "access_denied", turn_id, common, tool_call_id, result
                )
                return result
        if self.variant == "enforced" and owner["owner_id"] != self.principal:
            result = _error("ACCESS_DENIED", "The authenticated principal does not own this ticket")
            self._insert_event(connection, "access_denied", turn_id, common, tool_call_id, result)
            return result
        self._insert_event(connection, "access_allowed", turn_id, common, tool_call_id)
        if name == "get_ticket":
            row = connection.execute(
                "SELECT * FROM tickets WHERE id=?", (arguments.ticket_id,)
            ).fetchone()
            assert row is not None
            projection = _projection(row)
            result = {"ok": True, "data": projection}
            self._insert_event(
                connection,
                "read",
                turn_id,
                {**common, "projection": projection},
                tool_call_id,
                result,
            )
            conflict = self._fault(connection, "revision_conflict", arguments.ticket_id, turn_id)
            if conflict is not None:
                index, fault = conflict
                assert fault.patch is not None
                patch = fault.patch.supplied()
                fields = list(patch)
                connection.execute(
                    f"UPDATE tickets SET {','.join(f'{field}=?' for field in fields)},revision=revision+1 WHERE id=?",
                    [*[patch[field] for field in fields], arguments.ticket_id],
                )
                connection.execute("INSERT INTO fired_faults VALUES (?)", (index,))
                after_row = connection.execute(
                    "SELECT * FROM tickets WHERE id=?", (arguments.ticket_id,)
                ).fetchone()
                assert after_row is not None
                self._insert_event(
                    connection,
                    "fixture_intervention",
                    turn_id,
                    {
                        "ticketId": arguments.ticket_id,
                        "before": projection,
                        "after": _projection(after_row),
                        "patch": patch,
                    },
                    tool_call_id,
                )
            return result
        assert isinstance(arguments, UpdateTicketArgs)
        canonical = canonical_payload(name, arguments)
        previous = connection.execute(
            "SELECT canonical,result FROM commands WHERE attempt_id=? AND principal=? AND command_id=?",
            (self.attempt_id, self.principal, command_id),
        ).fetchone()
        if previous is not None:
            if previous["canonical"] != canonical:
                result = _error(
                    "COMMAND_CONFLICT", "This command ID is already bound to a different payload"
                )
                self._insert_event(
                    connection, "command_conflict", turn_id, common, tool_call_id, result
                )
                return result
            saved = cast(dict[str, JsonValue], strict_json_loads(previous["result"]))
            result = {**saved, "replayed": True}
            self._insert_event(
                connection,
                "replay",
                turn_id,
                {**common, "canonicalPayload": canonical},
                tool_call_id,
                result,
            )
            return result
        row = connection.execute(
            "SELECT * FROM tickets WHERE id=?", (arguments.ticket_id,)
        ).fetchone()
        assert row is not None
        before = _projection(row)
        if row["revision"] != arguments.expected_revision:
            result = _error(
                "VERSION_CONFLICT", "Ticket revision changed; read it again before retrying"
            )
            self._insert_event(
                connection, "version_conflict", turn_id, common, tool_call_id, result
            )
            return result
        failed = self._fault(connection, "save_failed", arguments.ticket_id, turn_id)
        if failed is not None:
            connection.execute("INSERT INTO fired_faults VALUES (?)", (failed[0],))
            result = _error("SAVE_FAILED", "Controlled failure before commit; nothing was saved")
            self._insert_event(
                connection,
                "fault",
                turn_id,
                {**common, "fault": "save_failed"},
                tool_call_id,
                result,
            )
            return result
        patch = arguments.patch.supplied()
        changed_fields = [field for field in patch if before[field] != patch[field]]
        changed = bool(changed_fields)
        if changed:
            connection.execute(
                f"UPDATE tickets SET {','.join(f'{field}=?' for field in changed_fields)},revision=revision+1 WHERE id=?",
                [*[patch[field] for field in changed_fields], arguments.ticket_id],
            )
        after_row = connection.execute(
            "SELECT * FROM tickets WHERE id=?", (arguments.ticket_id,)
        ).fetchone()
        assert after_row is not None
        after = _projection(after_row)
        result = {"ok": True, "data": after, "changed": changed, "replayed": False}
        connection.execute(
            "INSERT INTO commands VALUES (?,?,?,?,?)",
            (self.attempt_id, self.principal, command_id, canonical, _json(result)),
        )
        self._insert_event(
            connection,
            "commit" if changed else "noop",
            turn_id,
            {
                **common,
                "before": before,
                "after": after,
                "patch": patch,
                "fieldsChanged": cast(list[JsonValue], changed_fields),
                "canonicalPayload": canonical,
            },
            tool_call_id,
            result,
        )
        lost = self._fault(connection, "lost_response", arguments.ticket_id, turn_id)
        if lost is not None:
            connection.execute("INSERT INTO fired_faults VALUES (?)", (lost[0],))
            result = _error(
                "RESPONSE_LOST", "Controlled response loss; the write outcome must be read back"
            )
            self._insert_event(
                connection,
                "fault",
                turn_id,
                {**common, "fault": "lost_response"},
                tool_call_id,
                result,
            )
        return result


def read_snapshot(path: Path) -> list[dict[str, JsonValue]]:
    connection = sqlite3.connect(
        f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, isolation_level=None, timeout=1.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        return [_projection(row) for row in connection.execute("SELECT * FROM tickets ORDER BY id")]
    finally:
        connection.close()


def read_events(path: Path) -> list[Event]:
    connection = sqlite3.connect(
        f"file:{quote(str(path.resolve()))}?mode=ro", uri=True, isolation_level=None, timeout=1.0
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    try:
        return [
            Event.model_validate(
                {
                    "sequence": row["sequence"],
                    "attemptId": row["attempt_id"],
                    "turnId": row["turn_id"],
                    "type": row["type"],
                    "time": row["time"],
                    "toolCallId": row["tool_call_id"],
                    "result": strict_json_loads(row["result"])
                    if row["result"] is not None
                    else None,
                    "payload": strict_json_loads(row["payload"]),
                }
            )
            for row in connection.execute("SELECT * FROM events ORDER BY sequence")
        ]
    finally:
        connection.close()


__all__ = ["TicketStore", "read_snapshot", "read_events"]
