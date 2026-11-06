"""Bounded HTTP transports. One complete() call is exactly one HTTP request."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Awaitable, Callable
from typing import Protocol, TypeVar, cast

import httpx
from pydantic import JsonValue, ValidationError

from llm_test_lab.contracts import (
    Limits,
    ModelResponse,
    Profile,
    ToolCall,
    Usage,
    strict_json_loads,
)

T = TypeVar("T")


class TransportFailure(RuntimeError):
    def __init__(self, code: str, terminal: str = "provider_error", status: int | None = None):
        super().__init__(code)
        self.code = code
        self.terminal = terminal
        self.status = status


class CancelScope:
    """A monotonic deadline plus a durable admission/owner check during active HTTP."""

    def __init__(self, deadline: float, check: Callable[[], None] | None = None):
        self.deadline = deadline
        self._check = check

    def check(self) -> None:
        if self._check is not None:
            self._check()
        self.check_deadline()

    def check_deadline(self) -> None:
        """No control DB access: safe while the mutation guard transaction is held."""
        if time.monotonic() >= self.deadline:
            raise TransportFailure("ATTEMPT_TIMEOUT", "timeout")

    @property
    def remaining(self) -> float:
        self.check()
        return max(0.001, self.deadline - time.monotonic())

    async def wait(self, awaitable: Awaitable[T]) -> T:
        task = asyncio.ensure_future(awaitable)
        try:
            while not task.done():
                self.check()
                await asyncio.wait({task}, timeout=min(0.1, self.remaining))
            self.check()
            return task.result()
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)


class ModelTransport(Protocol):
    async def complete(
        self,
        messages: list[dict[str, JsonValue]],
        tool_schemas: list[dict[str, JsonValue]],
        parameters: dict[str, JsonValue],
        limits: Limits,
        cancel_scope: CancelScope,
    ) -> ModelResponse: ...


def redact(value: JsonValue, secrets: tuple[str, ...]) -> JsonValue:
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [redact(item, secrets) for item in value]
    if isinstance(value, dict):
        result: dict[str, JsonValue] = {}
        for key, item in value.items():
            safe_key = key
            for secret in secrets:
                if secret:
                    safe_key = safe_key.replace(secret, "[REDACTED]")
            if safe_key in result:
                safe_key += f"_redacted_duplicate_{len(result)}"
            result[safe_key] = (
                "[REDACTED]"
                if key.lower() in {"authorization", "api_key", "api-key"}
                else redact(item, secrets)
            )
        return result
    return value


def _object(value: JsonValue, code: str = "INVALID_PROVIDER_RESPONSE") -> dict[str, JsonValue]:
    if not isinstance(value, dict):
        raise TransportFailure(code)
    return value


def _usage_int(value: JsonValue) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def _calls(value: JsonValue, ollama: bool) -> list[ToolCall]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise TransportFailure("INVALID_TOOL_PROTOCOL", "model_error")
    result = []
    for index, entry in enumerate(value):
        item = _object(entry)
        function = _object(item.get("function"))
        arguments = function.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = strict_json_loads(arguments)
            except ValueError:
                # Invalid arguments remain a tool failure, not an executable string.
                arguments = {"__invalid_arguments__": "not valid JSON"}
        if not isinstance(arguments, dict):
            arguments = {"__invalid_arguments__": "arguments must be an object"}
        call_id = item.get("id")
        # Ollama does not require wire IDs. Internal evidence IDs never alter its wire message.
        if ollama and call_id is None:
            call_id = f"ollama-index-{index}"
        try:
            result.append(
                ToolCall.model_validate(
                    {"id": call_id, "name": function.get("name"), "arguments": arguments}
                )
            )
        except ValidationError as exc:
            raise TransportFailure("INVALID_TOOL_PROTOCOL", "model_error") from exc
    if len({call.id for call in result}) != len(result):
        raise TransportFailure("DUPLICATE_TOOL_CALL_ID", "model_error")
    return result


class HttpTransport:
    def __init__(
        self,
        profile: Profile,
        model: str,
        api_key: str | None = None,
        client: httpx.AsyncClient | None = None,
        on_event: Callable[[str, dict[str, JsonValue]], None] | None = None,
    ):
        self.profile = profile
        self.model = model
        self.api_key = api_key
        self._client = client
        self._owns_client = client is None
        self._on_event = on_event

    async def __aenter__(self) -> HttpTransport:
        if self._client is None:
            self._client = httpx.AsyncClient(
                trust_env=False,
                follow_redirects=False,
                transport=httpx.AsyncHTTPTransport(retries=0),
            )
        return self

    async def __aexit__(self, *_: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()

    def event(self, kind: str, payload: dict[str, JsonValue]) -> None:
        if self._on_event is not None:
            self._on_event(kind, cast(dict[str, JsonValue], redact(payload, (self.api_key or "",))))

    def _body(
        self,
        messages: list[dict[str, JsonValue]],
        tools: list[dict[str, JsonValue]],
        parameters: dict[str, JsonValue],
        limits: Limits,
    ) -> dict[str, JsonValue]:
        allowed = {"temperature", "top_p", "seed"}
        allowed.add("think" if self.profile.transport == "ollama" else "reasoning_effort")
        if not set(parameters).issubset(allowed):
            raise TransportFailure("UNSUPPORTED_GENERATION_PARAMETER", "executor_error")
        output_cap = min(limits.max_output_tokens, self.profile.limits.max_output_tokens)
        if self.profile.transport == "ollama":
            options: dict[str, JsonValue] = {
                **{name: value for name, value in parameters.items() if name != "think"},
                "num_predict": output_cap,
                "num_ctx": self.profile.context_window,
            }
            body: dict[str, JsonValue] = {
                "model": self.model,
                "messages": cast(JsonValue, messages),
                "tools": cast(JsonValue, tools),
                "stream": False,
                "options": options,
            }
            if "think" in parameters:
                body["think"] = parameters["think"]
            return body
        return {
            **parameters,
            "model": self.model,
            "messages": cast(JsonValue, messages),
            "tools": cast(JsonValue, tools),
            "stream": False,
            self.profile.output_token_parameter: output_cap,
            "tool_choice": "auto",
        }

    async def complete(
        self,
        messages: list[dict[str, JsonValue]],
        tool_schemas: list[dict[str, JsonValue]],
        parameters: dict[str, JsonValue],
        limits: Limits,
        cancel_scope: CancelScope,
    ) -> ModelResponse:
        if self._client is None:
            raise RuntimeError("Use HttpTransport as an async context manager")
        cancel_scope.check()
        body = self._body(messages, tool_schemas, parameters, limits)
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), allow_nan=False
        ).encode()
        if len(encoded) > limits.max_request_bytes:
            raise TransportFailure("REQUEST_SIZE_LIMIT", "limit_reached")
        # Deliberately conservative bound: one UTF-8 byte per token + framing reserve.
        # This is not a tokenizer conformance claim; experimental profiles stay experimental.
        if (
            len(encoded)
            + 1024
            + min(limits.max_output_tokens, self.profile.limits.max_output_tokens)
            > self.profile.context_window
        ):
            raise TransportFailure("CONTEXT_LIMIT", "limit_reached")
        path = "/api/chat" if self.profile.transport == "ollama" else "/chat/completions"
        url = self.profile.endpoint.rstrip("/") + path
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
        if self.api_key:
            headers["Authorization"] = "Bearer " + self.api_key
        self.event("model_request_prepared", {"request": body})
        cancel_scope.check()
        self.event("model_dispatch", {"endpoint": url, "model": self.model})

        async def read_bounded_response() -> bytes:
            assert self._client is not None
            async with self._client.stream(
                "POST",
                url,
                content=encoded,
                headers=headers,
                follow_redirects=False,
                timeout=min(cancel_scope.remaining, self.profile.limits.request_timeout_ms / 1000),
            ) as response:
                self.event("http_response", {"status": response.status_code})
                if response.status_code != 200:
                    code = {401: "AUTH_FAILED", 403: "AUTH_FAILED", 429: "RATE_LIMITED"}.get(
                        response.status_code, "PROVIDER_HTTP_ERROR"
                    )
                    raise TransportFailure(code, status=response.status_code)
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    # Avoid an unbounded decompression step before the byte cap.
                    raise TransportFailure("UNSUPPORTED_RESPONSE_ENCODING")
                data = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=65536):
                    cancel_scope.check()
                    if len(data) + len(chunk) > 2 * 1024 * 1024:
                        raise TransportFailure("PROVIDER_RESPONSE_TOO_LARGE")
                    data.extend(chunk)
                return bytes(data)

        try:
            received = await cancel_scope.wait(read_bounded_response())
        except httpx.TimeoutException as exc:
            raise TransportFailure("PROVIDER_TIMEOUT", "timeout") from exc
        except httpx.HTTPError as exc:
            raise TransportFailure("PROVIDER_UNAVAILABLE") from exc
        try:
            raw = _object(strict_json_loads(received.decode("utf-8")))
            raw = cast(dict[str, JsonValue], redact(raw, (self.api_key or "",)))
            self.event("model_response", {"response": raw})
            parsed = self._parse(raw)
        except (ValueError, ValidationError) as exc:
            raise TransportFailure("INVALID_PROVIDER_RESPONSE") from exc
        return parsed

    def _parse(self, raw: dict[str, JsonValue]) -> ModelResponse:
        if self.profile.transport == "ollama":
            message = _object(raw.get("message"))
            if raw.get("done") is not True:
                raise TransportFailure("INCOMPLETE_PROVIDER_RESPONSE", "model_error")
            calls = _calls(message.get("tool_calls"), True)
            reason = raw.get("done_reason")
            usage = Usage(
                input_tokens=_usage_int(raw.get("prompt_eval_count")),
                output_tokens=_usage_int(raw.get("eval_count")),
            )
        else:
            choices = raw.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                raise TransportFailure("INVALID_PROVIDER_CHOICES")
            choice = _object(choices[0])
            message = _object(choice.get("message"))
            calls = _calls(message.get("tool_calls"), False)
            reason = choice.get("finish_reason")
            usage_raw = raw.get("usage")
            usage_obj = usage_raw if isinstance(usage_raw, dict) else {}
            usage = Usage(
                input_tokens=_usage_int(usage_obj.get("prompt_tokens")),
                output_tokens=_usage_int(usage_obj.get("completion_tokens")),
            )
        if not isinstance(reason, str) or reason not in {"stop", "tool_calls"}:
            raise TransportFailure(
                "OUTPUT_TRUNCATED" if reason == "length" else "UNKNOWN_FINISH_REASON", "model_error"
            )
        if reason == "tool_calls" and not calls:
            raise TransportFailure("INCOMPLETE_TOOL_PROTOCOL", "model_error")
        if message.get("role") != "assistant":
            raise TransportFailure("INVALID_ASSISTANT_ROLE", "model_error")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise TransportFailure("INVALID_ASSISTANT_CONTENT", "model_error")
        model = raw.get("model")
        return ModelResponse(
            content=content,
            tool_calls=calls,
            finish_reason=reason,
            usage=usage,
            model=model if isinstance(model, str) else self.model,
            raw=raw,
            assistant_message=message,
        )
