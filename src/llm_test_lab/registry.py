"""Bounded local JSON registry with strict graph and clean/attack pair validation."""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import TypeVar

from pydantic import JsonValue, ValidationError

from llm_test_lab.contracts import (
    Case,
    Contract,
    ExpectationSet,
    Fixture,
    Profile,
    ScriptedResponseSet,
    load_json,
)
from llm_test_lab.validation import safe_validation_error

T = TypeVar("T", bound=Contract)


class RegistryError(ValueError):
    pass


class Registry:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, directory: str, identifier: str | Path) -> Path:
        base = (self.root / directory).resolve()
        if not base.is_relative_to(self.root):
            raise RegistryError("PATH_OUTSIDE_REGISTRY: registry directory escapes project")
        if isinstance(identifier, Path) or str(identifier).endswith(".json"):
            supplied = Path(identifier)
            path = supplied if supplied.is_absolute() else self.root / supplied
        else:
            if (
                not identifier
                or any(
                    c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_.-"
                    for c in identifier
                )
                or identifier in (".", "..")
            ):
                raise RegistryError("INVALID_REFERENCE: registry IDs must be local identifiers")
            path = base / f"{identifier}.json"
        path = path.resolve()
        if not path.is_relative_to(base) or path.suffix != ".json":
            raise RegistryError(f"PATH_OUTSIDE_REGISTRY: only JSON inside {directory} is allowed")
        if not path.is_file():
            raise RegistryError(f"REFERENCE_NOT_FOUND: {path.name}")
        return path

    def _load(self, directory: str, identifier: str | Path, model: type[T]) -> T:
        path = self._path(directory, identifier)
        try:
            value = model.model_validate(load_json(path))
        except ValidationError as exc:
            details = safe_validation_error(exc, model=model)
            raise RegistryError(
                f"INVALID_{model.__name__.upper()}: {path.name}: {details}"
            ) from None
        except ValueError:
            # Invalid JSON/encoding diagnostics must not reproduce input or duplicate keys.
            raise RegistryError(
                f"INVALID_{model.__name__.upper()}: {path.name}: invalid_json"
            ) from None
        except OSError as exc:
            raise RegistryError(f"READ_ERROR: {path.name}: {type(exc).__name__}") from None
        if (
            not isinstance(identifier, Path)
            and not str(identifier).endswith(".json")
            and getattr(value, "id") != identifier
        ):
            raise RegistryError(f"REGISTRY_ID_MISMATCH: {path.name}")
        return value

    def load_case(self, id_or_path: str | Path) -> Case:
        return self._load("scenarios", id_or_path, Case)

    def load_fixture(self, identifier: str) -> Fixture:
        return self._load("fixtures", identifier, Fixture)

    def load_expectation(self, identifier: str) -> ExpectationSet:
        return self._load("fixtures/expectations", identifier, ExpectationSet)

    def load_script(self, identifier: str) -> ScriptedResponseSet:
        return self._load("fixtures/responses", identifier, ScriptedResponseSet)

    def load_profile(self, identifier: str) -> Profile:
        from llm_test_lab.conformance import validate_supported_profile

        profile = self._load("profiles", identifier, Profile)
        validate_supported_profile(self.root, profile)
        return profile

    def _all(self, directory: str, model: type[T]) -> list[T]:
        result: list[T] = []
        ids: set[str] = set()
        for path in sorted((self.root / directory).glob("*.json")):
            item = self._load(directory, path, model)
            identifier = str(getattr(item, "id"))
            if identifier in ids:
                raise RegistryError(f"DUPLICATE_ID: {directory}/{identifier}")
            ids.add(identifier)
            if path.stem != identifier:
                raise RegistryError(f"REGISTRY_ID_MISMATCH: filename {path.name} must match id")
            result.append(item)
        return result

    def cases(self) -> list[Case]:
        return self._all("scenarios", Case)

    def validate_case(self, case: Case) -> None:
        fixture = self.load_fixture(case.fixture)
        expectation = self.load_expectation(case.expectation_set)
        script = self.load_script(case.scripted_response_set)
        if case.actor not in fixture.users:
            raise RegistryError(f"UNKNOWN_ACTOR: {case.id}")
        ticket_ids = {ticket.id for ticket in fixture.tickets}
        referenced = set(
            expectation.allowed_mutations.ticket_ids
            + expectation.forbidden_access.read
            + expectation.forbidden_access.write
        )
        referenced.update(state.ticket_id for state in expectation.final_state)
        if case.attack_goal is not None:
            referenced.add(case.attack_goal.ticket_id)
        if not referenced.issubset(ticket_ids):
            raise RegistryError(f"UNKNOWN_TICKET_REFERENCE: {case.id}")
        turn_ids = {turn.id for turn in case.turns}
        if set(expectation.turn_outcomes) != turn_ids:
            raise RegistryError(f"TURN_EXPECTATION_MISMATCH: {case.id}")
        if not set(expectation.required_facts).issubset(turn_ids) or any(
            rule.turn_id not in turn_ids for rule in expectation.turn_rules
        ):
            raise RegistryError(f"UNKNOWN_TURN_REFERENCE: {case.id}")
        if any(step.turn_id not in turn_ids for step in script.steps):
            raise RegistryError(f"UNKNOWN_SCRIPT_TURN: {case.id}")
        if any(
            fault.turn_id is not None and fault.turn_id not in turn_ids for fault in fixture.faults
        ):
            raise RegistryError(f"UNKNOWN_FAULT_TURN: {case.id}")
        indexes = {turn.id: index for index, turn in enumerate(case.turns)}
        for variant in ("vulnerable", "prompt_only", "enforced"):
            selected = [
                step for step in script.steps if step.variants is None or variant in step.variants
            ]
            selected_indexes = [indexes[step.turn_id] for step in selected]
            if not selected or selected_indexes != sorted(selected_indexes):
                raise RegistryError(f"SCRIPT_TURN_ORDER: {case.id}/{variant}")

    def validate_pairs(self) -> int:
        pairs: dict[str, list[Case]] = {}
        for case in self.cases():
            if case.pair_id is not None:
                pairs.setdefault(case.pair_id, []).append(case)
        for pair_id, cases in pairs.items():
            if len(cases) != 2 or {case.condition for case in cases} != {"clean", "attack"}:
                raise RegistryError(
                    f"INVALID_PAIR: {pair_id} requires exactly one clean and one attack case"
                )
            clean, attack = sorted(cases, key=lambda case: case.condition == "attack")
            if (
                clean.actor != attack.actor
                or clean.turns != attack.turns
                or clean.expectation_set != attack.expectation_set
            ):
                raise RegistryError(f"PAIR_CONTRACT_DRIFT: {pair_id}")
            left = self._pair_projection(self.load_fixture(clean.fixture))
            right = self._pair_projection(self.load_fixture(attack.fixture))
            if left != right:
                raise RegistryError(f"PAIR_FIXTURE_DRIFT: {pair_id}; only A-101.note may differ")
        return len(pairs)

    @staticmethod
    def _pair_projection(fixture: Fixture) -> dict[str, JsonValue]:
        value = deepcopy(fixture.to_dict())
        value.pop("id")
        value["users"] = [user for user in sorted(fixture.users)]
        tickets: list[JsonValue] = []
        for ticket in sorted(fixture.tickets, key=lambda ticket: ticket.id):
            projection = ticket.to_dict()
            if ticket.id == "A-101":
                projection.pop("note")
            tickets.append(projection)
        value["tickets"] = tickets
        return value

    def validate_all(self) -> dict[str, int]:
        cases = self.cases()
        fixtures = self._all("fixtures", Fixture)
        expectations = self._all("fixtures/expectations", ExpectationSet)
        scripts = self._all("fixtures/responses", ScriptedResponseSet)
        profiles = self._all("profiles", Profile)
        from llm_test_lab.conformance import validate_supported_profile

        for profile in profiles:
            validate_supported_profile(self.root, profile)
        for case in cases:
            self.validate_case(case)
        return {
            "cases": len(cases),
            "fixtures": len(fixtures),
            "expectations": len(expectations),
            "scripts": len(scripts),
            "profiles": len(profiles),
            "pairs": self.validate_pairs(),
        }
