"""Useful validation diagnostics without echoing untrusted values or dictionary keys."""

from __future__ import annotations

import re
from typing import get_args

from pydantic import BaseModel, ValidationError


def _trusted_models() -> list[type[BaseModel]]:
    pending = list(BaseModel.__subclasses__())
    seen: set[type[BaseModel]] = set()
    result: list[type[BaseModel]] = []
    while pending:
        model = pending.pop()
        if model in seen:
            continue
        seen.add(model)
        pending.extend(model.__subclasses__())
        if model.__module__.startswith("llm_test_lab."):
            result.append(model)
    return result


def _known_fields(model: type[BaseModel] | None) -> set[str]:
    if model is None:
        return set()
    names: set[str] = set()
    models = [model]
    seen: set[type[BaseModel]] = set()
    while models:
        current = models.pop()
        if current in seen:
            continue
        seen.add(current)
        for name, field in current.model_fields.items():
            names.add(name)
            if isinstance(field.alias, str):
                names.add(field.alias)
            annotations: list[object] = [field.annotation]
            while annotations:
                annotation = annotations.pop()
                if isinstance(annotation, type) and issubclass(annotation, BaseModel):
                    models.append(annotation)
                else:
                    annotations.extend(get_args(annotation))
    return names


def safe_validation_error(error: ValidationError, *, model: type[BaseModel] | None = None) -> str:
    """Only trusted field names and error codes cross a log/CLI boundary.

    Pydantic's input, context, message, title and unknown field names may all
    contain user-supplied credentials. Do not stringify the original exception.
    """
    if model is None:
        model = next((item for item in _trusted_models() if item.__name__ == error.title), None)
    fields = _known_fields(model)
    descriptions: list[str] = []
    errors = error.errors(include_input=False, include_context=False, include_url=False)
    for item in errors[:8]:
        parts: list[str] = []
        for part in item["loc"][:8]:
            if isinstance(part, str) and part in fields:
                parts.append(part)
            elif isinstance(part, int):
                parts.append("[]")
            else:
                parts.append("<unknown field>")
        location = ".".join(parts) if parts else "<document>"
        code = item["type"]
        if re.fullmatch(r"[a-z_][a-z0-9_.-]{0,79}", code) is None:
            code = "validation_error"
        description = f"{location}: {code}"
        if any(part in {"apiKeyEnv", "api_key_env"} for part in parts):
            description += "; apiKeyEnv — имя переменной окружения, не значение ключа"
        descriptions.append(description)
    if len(errors) > 8:
        descriptions.append("additional validation errors omitted")
    return "; ".join(descriptions) or "invalid structured input"
