"""Provider failures have bounded retries; rejected requests fail immediately."""

from __future__ import annotations

import asyncio
from email.utils import formatdate
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import openai
import pytest

from tcad.llm import client as llm_module
from tcad.llm.client import OpenAIClient


def _response():
    return SimpleNamespace(
        choices=[SimpleNamespace(
            message=SimpleNamespace(content="ready", tool_calls=None,
                                    reasoning_content="preserved protocol state"),
            finish_reason="stop",
        )],
        usage=SimpleNamespace(prompt_tokens=11, completion_tokens=7),
    )


def _status_error(status, *, headers=None, body=None):
    """Use actual SDK exception types, including statuses without a subclass."""
    response = httpx.Response(
        status,
        headers=headers,
        request=httpx.Request("POST", "https://provider.invalid/v1/chat/completions"),
    )
    exception_type = {
        400: openai.BadRequestError,
        401: openai.AuthenticationError,
        403: openai.PermissionDeniedError,
        404: openai.NotFoundError,
        409: openai.ConflictError,
        422: openai.UnprocessableEntityError,
        429: openai.RateLimitError,
    }.get(status, openai.InternalServerError if status >= 500 else openai.APIStatusError)
    return exception_type("provider rejected request", response=response, body=body)


@pytest.fixture
async def make_client():
    clients = []

    def factory(*outcomes, max_retries=2):
        def unexpected_network(request):
            raise AssertionError("unit test must not contact a provider")

        transport = httpx.MockTransport(unexpected_network)
        client = OpenAIClient(
            base_url="https://provider.invalid/v1",
            api_key="test-placeholder",
            model="test-model",
            max_retries=max_retries,
            http_client=httpx.AsyncClient(transport=transport),
        )
        client._client.chat.completions.create = AsyncMock(side_effect=outcomes)
        clients.append(client)
        return client, client._client.chat.completions.create

    yield factory
    for client in clients:
        await client._client.close()


@pytest.fixture
def sleeps(monkeypatch):
    delays = []

    async def fake_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(llm_module.asyncio, "sleep", fake_sleep)
    return delays


@pytest.mark.parametrize("status", [408, 409, 429, 500, 502, 503, 504, 599])
async def test_transient_status_is_retried(make_client, sleeps, status):
    client, create = make_client(_status_error(status), _response())

    reply = await client.chat(messages=[{"role": "user", "content": "hello"}])

    assert create.await_count == 2
    assert sleeps == [1.0]
    assert reply.text == "ready"
    assert reply.reasoning_content == "preserved protocol state"
    assert reply.usage.prompt_tokens == 11
    assert reply.usage.completion_tokens == 7


@pytest.mark.parametrize("error_type", [openai.APIConnectionError, openai.APITimeoutError])
async def test_transport_and_timeout_are_retried(make_client, sleeps, error_type):
    error = error_type(request=httpx.Request("POST", "https://provider.invalid/v1"))
    client, create = make_client(error, _response())

    await client.chat(messages=[])

    assert create.await_count == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422, 451, 499, 600])
async def test_permanent_status_is_not_retried(make_client, sleeps, status):
    # A provider hint must never promote a permanent rejection to retryable.
    error = _status_error(status, headers={"retry-after": "2", "x-should-retry": "true"})
    client, create = make_client(error, _response())

    with pytest.raises(type(error)) as caught:
        await client.chat(messages=[])

    assert caught.value is error
    assert create.await_count == 1
    assert sleeps == []


@pytest.mark.parametrize("status", [400, 429, 500])
@pytest.mark.parametrize("code", ["context_length_exceeded", "context_window_exceeded"])
@pytest.mark.parametrize("nested", [False, True])
async def test_context_overflow_is_not_retried(make_client, sleeps, status, code, nested):
    body = {
        "code": code,
        "message": "Maximum context length exceeded",
        "type": "invalid_request_error",
    }
    error = _status_error(status, body={"error": body} if nested else body)
    client, create = make_client(error, _response())

    with pytest.raises(type(error)) as caught:
        await client.chat(messages=[])

    assert caught.value is error
    assert create.await_count == 1
    assert sleeps == []


@pytest.mark.parametrize("nested", [False, True])
async def test_insufficient_quota_is_not_retried(make_client, sleeps, nested):
    body = {"code": "insufficient_quota", "type": "insufficient_quota"}
    error = _status_error(
        429, body={"error": body} if nested else body,
        headers={"retry-after": "2", "x-should-retry": "true"},
    )
    client, create = make_client(error, _response())

    with pytest.raises(openai.RateLimitError) as caught:
        await client.chat(messages=[])

    assert caught.value is error
    assert create.await_count == 1
    assert sleeps == []


@pytest.mark.parametrize("code", ["rate_limit_exceeded", "quota_exceeded"])
@pytest.mark.parametrize("nested", [False, True])
async def test_transient_or_ambiguous_rate_limit_code_is_retried(make_client, sleeps, code, nested):
    # Only a known permanent code overrides 429; do not infer billing exhaustion
    # from ambiguous compatible-provider codes such as quota_exceeded.
    body = {"code": code}
    client, create = make_client(
        _status_error(429, body={"error": body} if nested else body), _response(),
    )

    reply = await client.chat(messages=[])

    assert reply.text == "ready"
    assert create.await_count == 2
    assert sleeps == [1.0]


@pytest.mark.parametrize("kind", ["api", "validation", "json", "runtime"])
async def test_unclassified_and_malformed_response_errors_are_not_retried(make_client, sleeps, kind):
    request = httpx.Request("POST", "https://provider.invalid/v1")
    error = {
        "api": openai.APIError("unclassified provider failure", request=request, body=None),
        "validation": openai.APIResponseValidationError(
            response=httpx.Response(200, request=request), body={"choices": "wrong shape"},
        ),
        "json": json.JSONDecodeError("invalid provider JSON", "", 0),
        "runtime": RuntimeError("application error"),
    }[kind]
    client, create = make_client(error, _response())

    with pytest.raises(type(error)) as caught:
        await client.chat(messages=[])

    assert caught.value is error
    assert create.await_count == 1
    assert sleeps == []


@pytest.mark.parametrize("max_retries", [0, 1, 2, 7])
async def test_retry_exhaustion_keeps_configured_attempt_count_and_capped_backoff(
    make_client, sleeps, max_retries,
):
    errors = [_status_error(503) for _ in range(max_retries + 1)]
    client, create = make_client(*errors, max_retries=max_retries)

    with pytest.raises(openai.InternalServerError) as caught:
        await client.chat(messages=[])

    assert caught.value is errors[-1]
    assert create.await_count == max_retries + 1
    assert sleeps == [min(2.0 ** attempt, 30.0) for attempt in range(max_retries)]
    assert client.max_retries == max_retries
    assert client._client.max_retries == 0


async def test_retry_preserves_request_arguments(make_client, sleeps):
    client, create = make_client(_status_error(429), _status_error(503), _response())
    messages = [{"role": "user", "content": "make a part"}]
    tools = [{"type": "function", "function": {"name": "ir_get"}}]

    await client.chat(messages=messages, tools=tools, tool_choice="auto", temperature=0.0)

    expected = dict(model="test-model", messages=messages, tools=tools,
                    tool_choice="auto", temperature=0.0, max_tokens=4096)
    assert [call.kwargs for call in create.await_args_list] == [expected] * 3
    assert sleeps == [1.0, 2.0]


@pytest.mark.parametrize(("headers", "expected"), [
    ({"retry-after-ms": "250"}, 0.25),
    ({"retry-after-ms": "1500.5"}, 1.5005),
    ({"retry-after-ms": "0"}, 0.0),
    ({"retry-after-ms": "600000"}, 30.0),
    ({"retry-after-ms": "500", "retry-after": "9"}, 0.5),
    ({"retry-after-ms": "500", "retry-after": "nonsense"}, 0.5),
    ({"retry-after": "3"}, 3.0),
    ({"Retry-After": "2.5"}, 2.5),
    ({"retry-after": "0"}, 0.0),
    ({"retry-after": "1000000"}, 30.0),
    ({"retry-after-ms": "bad", "retry-after": "4"}, 4.0),
    ({"retry-after-ms": "-10", "retry-after": "4"}, 4.0),
    ({"retry-after-ms": "nan", "retry-after": "4"}, 4.0),
    ({"retry-after-ms": "inf", "retry-after": "4"}, 4.0),
    ({"retry-after-ms": "-inf", "retry-after": "4"}, 4.0),
    ({"retry-after-ms": "1e9999", "retry-after": "4"}, 4.0),
    ({}, 1.0),
    ({"retry-after": ""}, 1.0),
    ({"retry-after": "broken date"}, 1.0),
    ({"retry-after": "-1"}, 1.0),
    ({"retry-after": "NaN"}, 1.0),
    ({"retry-after": "Infinity"}, 1.0),
    ({"retry-after": "-inf"}, 1.0),
    ({"retry-after": "1e9999"}, 1.0),
    ({"retry-after-ms": "NaN"}, 1.0),
    ({"retry-after-ms": "Infinity"}, 1.0),
    ({"retry-after-ms": "-1", "retry-after": "NaN"}, 1.0),
    ({"retry-after": "Fri, 32 Oct 2026 15:00:00 GMT"}, 1.0),
])
async def test_retry_after_headers_are_validated_and_bounded(make_client, sleeps, headers, expected):
    client, create = make_client(_status_error(429, headers=headers), _response())

    await client.chat(messages=[])

    assert create.await_count == 2
    assert sleeps == [expected]


@pytest.mark.parametrize(("offset", "expected"), [(12, 12.0), (60, 30.0), (-1, 1.0), (0, 0.0)])
async def test_retry_after_http_date(make_client, sleeps, monkeypatch, offset, expected):
    now = 1_790_953_200.0
    monkeypatch.setattr(llm_module.time, "time", lambda: now)
    headers = {"retry-after": formatdate(now + offset, usegmt=True)}
    client, create = make_client(_status_error(503, headers=headers), _response())

    await client.chat(messages=[])

    assert create.await_count == 2
    assert sleeps == [expected]


async def test_invalid_milliseconds_fall_back_to_http_date(make_client, sleeps, monkeypatch):
    now = 1_790_953_200.0
    monkeypatch.setattr(llm_module.time, "time", lambda: now)
    headers = {"retry-after-ms": "nan", "retry-after": formatdate(now + 5, usegmt=True)}
    client, _ = make_client(_status_error(503, headers=headers), _response())

    await client.chat(messages=[])

    assert sleeps == [5.0]


async def test_backoff_still_advances_after_provider_hint(make_client, sleeps):
    client, create = make_client(
        _status_error(429, headers={"retry-after": "0.25"}),
        _status_error(503, headers={"retry-after": "invalid"}),
        _response(),
    )

    await client.chat(messages=[])

    assert create.await_count == 3
    assert sleeps == [0.25, 2.0]


async def test_successful_response_parse_failure_is_not_retried(make_client, sleeps):
    response = _response()
    response.choices = []
    client, create = make_client(response, _response())

    with pytest.raises(IndexError):
        await client.chat(messages=[])

    assert create.await_count == 1
    assert sleeps == []


async def test_even_transient_parse_exception_is_not_retried(make_client, sleeps, monkeypatch):
    # Keep the retry boundary at the network call, independently of exception type.
    error = _status_error(503)
    client, create = make_client(_response(), _response())

    def parse_error(response):
        raise error

    monkeypatch.setattr(client, "_parse", parse_error)

    with pytest.raises(openai.InternalServerError) as caught:
        await client.chat(messages=[])

    assert caught.value is error
    assert create.await_count == 1
    assert sleeps == []


async def test_malformed_tool_arguments_are_recorded_without_retry(make_client, sleeps):
    response = _response()
    response.choices[0].message.tool_calls = [SimpleNamespace(
        id="call1", function=SimpleNamespace(name="ir_patch", arguments='{"ops": ['),
    )]
    response.choices[0].finish_reason = "length"
    client, create = make_client(response)

    reply = await client.chat(messages=[])

    assert create.await_count == 1
    assert sleeps == []
    assert reply.finish_reason == "length"
    assert reply.reasoning_content == "preserved protocol state"
    assert reply.tool_calls[0].args_error is not None
    assert reply.tool_calls[0].args_raw_len == len('{"ops": [')


async def test_request_cancellation_propagates_without_retry(make_client, sleeps):
    cancelled = asyncio.CancelledError()
    client, create = make_client(cancelled, _response())

    with pytest.raises(asyncio.CancelledError) as caught:
        await client.chat(messages=[])

    assert caught.value is cancelled
    assert create.await_count == 1
    assert sleeps == []


async def test_cancellation_during_retry_wait_stops_next_attempt(make_client, monkeypatch):
    waiting = asyncio.Event()
    never_resume = asyncio.Event()
    delays = []

    async def blocked_sleep(delay):
        delays.append(delay)
        waiting.set()
        await never_resume.wait()

    monkeypatch.setattr(llm_module.asyncio, "sleep", blocked_sleep)
    client, create = make_client(_status_error(429), _response())
    task = asyncio.create_task(client.chat(messages=[]))
    try:
        await asyncio.wait_for(waiting.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        if not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    assert create.await_count == 1
    assert delays == [1.0]


async def test_sdk_transport_has_no_hidden_retry_layer(sleeps):
    attempts = []

    def reject(request):
        attempts.append(request)
        return httpx.Response(503, json={"error": {"message": "temporarily unavailable"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(reject)) as transport:
        client = OpenAIClient(
            base_url="https://provider.invalid/v1", api_key="test-placeholder",
            model="test-model", max_retries=2, http_client=transport,
        )
        with pytest.raises(openai.InternalServerError):
            await client.chat(messages=[])

    assert len(attempts) == 3
    assert sleeps == [1.0, 2.0]
