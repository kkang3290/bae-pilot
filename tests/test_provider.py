"""Real SDK over an in-memory HTTP transport: no credentials or billable requests."""

import json

import pytest


def sse(text, stop="end_turn"):
    values = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [],
                    "model": "claude-opus-5-5",
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 10, "output_tokens": 0},
                },
            },
        ),
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": 0,
                "content_block": {"type": "text", "text": ""},
            },
        ),
        (
            "content_block_delta",
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "text_delta", "text": text},
            },
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop, "stop_sequence": None},
                "usage": {"output_tokens": 20},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return "".join(f"event: {name}\ndata: {json.dumps(value)}\n\n" for name, value in values)


async def build_provider(text, stop="end_turn", fallback=True, status=200):
    anthropic = pytest.importorskip("anthropic")
    httpx = pytest.importorskip("httpx2")
    from bae_pilot.config import Settings
    from bae_pilot.models.anthropic import AnthropicProvider

    requests = []

    def handle(request):
        requests.append(request)
        if status != 200:
            return httpx.Response(
                status,
                json={
                    "type": "error",
                    "error": {"type": "invalid_request_error", "message": "bad request"},
                },
            )
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, text=sse(text, stop)
        )

    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx.MockTransport(handle)),
    )
    return AnthropicProvider(
        Settings(provider="anthropic", refusal_fallback=fallback), client=client
    ), requests


async def test_official_sdk_streams_and_validates_structured_role_output():
    from bae_pilot.agents.schemas import TalkResult

    provider, requests = await build_provider(
        '{"reply":"我在听","action":"reply","requires_deep":false}'
    )
    try:
        result = await provider.complete("talk", {"messages": [{"text": "hello"}]}, TalkResult)
        assert result.reply == "我在听"
        payload = json.loads(requests[0].content)
        assert payload["stream"] is True
        assert payload["model"] == "claude-opus-5-5"
        assert payload["output_config"]["format"]["type"] == "json_schema"
        assert payload["thinking"]["type"] == "adaptive"
        assert payload["fallbacks"] == "default"
        assert "server-side-fallback-2026-07-01" in requests[0].headers["anthropic-beta"]
        assert payload["messages"][0]["role"] == "user"
        assert "hello" not in payload["system"]
    finally:
        await provider.close()


@pytest.mark.parametrize(
    ("text", "stop", "error"),
    [
        ('{"reply":"unfinished"}', "max_tokens", "model_incomplete"),
        ("", "refusal", "model_refused"),
        ('{"reply":"","action":"reply"}', "end_turn", "invalid_model_output"),
    ],
)
async def test_incomplete_refused_or_invalid_results_never_become_replies(text, stop, error):
    from bae_pilot.agents.schemas import TalkResult
    from bae_pilot.domain import AppError

    provider, _ = await build_provider(text, stop)
    try:
        with pytest.raises(AppError, match=error):
            await provider.complete("talk", {"messages": []}, TalkResult)
    finally:
        await provider.close()


async def test_fallback_can_be_disabled_and_api_failure_is_not_a_fake_reply():
    from bae_pilot.agents.schemas import TalkResult
    from bae_pilot.domain import AppError

    provider, requests = await build_provider("", fallback=False, status=400)
    try:
        with pytest.raises(AppError, match="model_request_rejected"):
            await provider.complete("talk", {"messages": []}, TalkResult)
        assert "fallbacks" not in json.loads(requests[0].content)
    finally:
        await provider.close()
