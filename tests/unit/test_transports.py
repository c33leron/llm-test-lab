import asyncio
import json
import time

import httpx
import pytest

from llm_test_lab.contracts import Limits, Profile, tool_schemas
from llm_test_lab.transports import CancelScope, HttpTransport, TransportFailure, redact


def profile(transport="chat-completions", **values):
    return Profile.model_validate(
        {
            "schemaVersion": 1,
            "id": "test",
            "transport": transport,
            "endpoint": "http://127.0.0.1:11434"
            if transport == "ollama"
            else "https://example.invalid/v1",
            "model": "test-model",
            "contextWindow": 32768,
            "limits": {"requestTimeoutMs": 1000, "maxOutputTokens": 512},
            "compatibility": "experimental",
            **values,
        }
    )


def response(content="hello", **values):
    return {
        "model": "test-model",
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
        **values,
    }


async def complete(handler, *, config=None, events=None, limits=None):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with HttpTransport(
            config or profile(),
            "test-model",
            client=client,
            on_event=lambda kind, payload: (
                events.append((kind, payload)) if events is not None else None
            ),
        ) as transport:
            return await transport.complete(
                [{"role": "user", "content": "test"}],
                tool_schemas(),
                {},
                limits or Limits(),
                CancelScope(time.monotonic() + 5),
            )


async def test_one_call_one_request_unknown_usage() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=response())

    result = await complete(handler)
    assert len(requests) == 1
    assert result.usage.input_tokens is None
    assert result.usage.output_tokens is None
    body = json.loads(requests[0].content)
    assert body["stream"] is False
    assert body["max_tokens"] == 512
    assert requests[0].url.path == "/v1/chat/completions"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (429, "RATE_LIMITED"),
        (401, "AUTH_FAILED"),
        (500, "PROVIDER_HTTP_ERROR"),
        (302, "PROVIDER_HTTP_ERROR"),
    ],
)
async def test_no_retry_or_redirect(status: int, code: str) -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(status, headers={"location": "https://other.invalid"})

    with pytest.raises(TransportFailure) as error:
        await complete(handler)
    assert error.value.code == code
    assert calls == 1


async def test_native_tool_ids_and_provider_message_preserved() -> None:
    wire = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "native-id-42",
                "type": "function",
                "function": {"name": "get_ticket", "arguments": '{"ticketId":"A-101"}'},
            }
        ],
    }
    raw = {
        "model": "actual-model",
        "choices": [{"finish_reason": "tool_calls", "message": wire}],
        "usage": {"prompt_tokens": 20, "completion_tokens": 7},
    }
    result = await complete(lambda request: httpx.Response(200, json=raw))
    assert result.tool_calls[0].id == "native-id-42"
    assert result.assistant_message == wire
    assert result.model == "actual-model"
    assert result.usage.input_tokens == 20


async def test_ollama_wire_caps_and_tool_message() -> None:
    requests = []

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "local-model",
                "done": True,
                "done_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"function": {"name": "get_ticket", "arguments": {"ticketId": "A-101"}}}
                    ],
                },
            },
        )

    result = await complete(handler, config=profile("ollama"))
    assert result.tool_calls[0].name == "get_ticket"
    assert requests[0]["options"]["num_ctx"] == 32768
    assert requests[0]["options"]["num_predict"] == 512
    assert result.assistant_message["tool_calls"][0].get("id") is None


async def test_textual_json_tool_is_not_executable() -> None:
    result = await complete(
        lambda request: httpx.Response(
            200, json=response('{"name":"update_ticket","arguments":{}}')
        )
    )
    assert result.tool_calls == []


async def test_truncation_is_model_error_but_raw_response_is_saved() -> None:
    raw = response()
    raw["choices"][0]["finish_reason"] = "length"
    events = []
    with pytest.raises(TransportFailure) as error:
        await complete(lambda request: httpx.Response(200, json=raw), events=events)
    assert error.value.terminal == "model_error"
    assert error.value.code == "OUTPUT_TRUNCATED"
    assert any(kind == "model_response" for kind, _ in events)


async def test_request_limit_before_dispatch() -> None:
    calls = 0

    def handler(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json=response())

    with pytest.raises(TransportFailure, match="REQUEST_SIZE_LIMIT"):
        await complete(handler, limits=Limits(max_request_bytes=1))
    assert calls == 0


async def test_context_limit_before_dispatch() -> None:
    with pytest.raises(TransportFailure, match="CONTEXT_LIMIT"):
        await complete(lambda _: pytest.fail("must not send"), config=profile(contextWindow=512))


async def test_owner_loss_cancels_inflight_http() -> None:
    started = asyncio.Event()
    stopped = asyncio.Event()

    async def handler(request):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped.set()

    def check():
        if started.is_set():
            raise RuntimeError("owner lost")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with HttpTransport(profile(), "test-model", client=client) as transport:
            with pytest.raises(RuntimeError, match="owner lost"):
                await transport.complete(
                    [], tool_schemas(), {}, Limits(), CancelScope(time.monotonic() + 5, check)
                )
    assert stopped.is_set()


async def test_redaction_applies_to_provider_echo_and_raw_events() -> None:
    events = []
    secret = "synthetic-key-123"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=response(secret)))
    ) as client:
        async with HttpTransport(
            profile(),
            "test",
            api_key=secret,
            client=client,
            on_event=lambda kind, data: events.append(data),
        ) as transport:
            result = await transport.complete(
                [], [], {}, Limits(), CancelScope(time.monotonic() + 5)
            )
    assert secret not in json.dumps(result.to_dict())
    assert secret not in json.dumps(events)
    assert redact({"Authorization": "Bearer anything", "value": secret}, (secret,)) == {
        "Authorization": "[REDACTED]",
        "value": "[REDACTED]",
    }


async def test_http_timeout_classification() -> None:
    def handler(request):
        raise httpx.ReadTimeout("sensitive URL must not leak")

    with pytest.raises(TransportFailure) as error:
        await complete(handler)
    assert error.value.terminal == "timeout"
    assert "sensitive" not in str(error.value)


async def test_secret_echo_as_json_key_is_redacted() -> None:
    secret = "SYNTHETIC_REVIEW_KEY_ABC123"
    raw = response()
    raw["debug"] = {secret: "echo", "[REDACTED]": "collision"}
    events = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=raw))
    ) as client:
        async with HttpTransport(
            profile(),
            "test",
            api_key=secret,
            client=client,
            on_event=lambda kind, data: events.append(data),
        ) as transport:
            result = await transport.complete(
                [], [], {}, Limits(), CancelScope(time.monotonic() + 5)
            )
    assert secret not in json.dumps(result.to_dict())
    assert secret not in json.dumps(events)
    assert len(result.raw["debug"]) == 2


async def test_assistant_response_cannot_change_message_role() -> None:
    raw = response()
    raw["choices"][0]["message"]["role"] = "system"
    with pytest.raises(TransportFailure, match="INVALID_ASSISTANT_ROLE"):
        await complete(lambda request: httpx.Response(200, json=raw))


async def test_response_is_bounded_while_reading_and_closed() -> None:
    class LargeStream(httpx.AsyncByteStream):
        chunks = 0
        closed = False

        async def __aiter__(self):
            for _ in range(200):
                self.chunks += 1
                yield b"x" * 65536

        async def aclose(self):
            self.closed = True

    stream = LargeStream()
    with pytest.raises(TransportFailure, match="PROVIDER_RESPONSE_TOO_LARGE"):
        await complete(lambda _: httpx.Response(200, stream=stream))
    assert stream.chunks <= 33
    assert stream.closed


async def test_modern_completion_cap_is_explicit_not_a_retry_fallback() -> None:
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(200, json=response())

    await complete(handler, config=profile(outputTokenParameter="max_completion_tokens"))
    assert len(seen) == 1
    assert seen[0]["max_completion_tokens"] == 512
    assert "max_tokens" not in seen[0]


async def test_ollama_think_is_top_level_not_generation_option() -> None:
    seen = []

    def handler(request):
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "model": "test-model",
                "message": {"role": "assistant", "content": "ok"},
                "done": True,
                "done_reason": "stop",
            },
        )

    config = profile("ollama", parameters={"think": False, "temperature": 0})
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with HttpTransport(config, "test-model", client=client) as transport:
            await transport.complete(
                [], tool_schemas(), config.parameters, Limits(), CancelScope(time.monotonic() + 5)
            )
    assert seen[0]["think"] is False
    assert "think" not in seen[0]["options"]


@pytest.mark.parametrize(
    "parameters",
    [
        {"temperature": True},
        {"temperature": 3},
        {"top_p": -1},
        {"seed": False},
        {"seed": -1},
        {"shell": "no"},
        {"think": True},
        {"reasoning_effort": "unlimited"},
    ],
)
def test_invalid_generation_configuration_fails_before_network(parameters) -> None:
    with pytest.raises(ValueError):
        profile(parameters=parameters)


async def test_invalid_utf8_is_provider_error_not_unredacted_exception() -> None:
    with pytest.raises(TransportFailure, match="INVALID_PROVIDER_RESPONSE"):
        await complete(lambda _: httpx.Response(200, content=b"\xff"))


async def test_injected_client_cannot_enable_redirects() -> None:
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://other.invalid"})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    ) as client:
        async with HttpTransport(profile(), "test-model", client=client) as transport:
            with pytest.raises(TransportFailure, match="PROVIDER_HTTP_ERROR"):
                await transport.complete([], [], {}, Limits(), CancelScope(time.monotonic() + 5))
    assert len(requests) == 1
