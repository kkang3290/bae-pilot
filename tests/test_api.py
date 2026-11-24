import httpx
import pytest


@pytest.fixture
async def client(tmp_path):
    from bae_pilot.app import create_app
    from bae_pilot.config import Settings

    app = create_app(
        Settings(
            db_path=tmp_path / "api.sqlite",
            api_token="test-management-token",
            debounce_s=0,
            min_send_interval_s=0,
        ),
        start_worker=False,
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as http:
            yield http, app.state.runtime


def body(**updates):
    return {
        "account_id": "demo-account",
        "conversation_id": "demo-chat",
        "event_id": "api-one",
        "sender_id": "peer",
        "text": "你好",
        **updates,
    }


HEADERS = {"Authorization": "Bearer test-management-token"}
PATH = "/api/v1/channels/mock/accounts/demo-account/conversations/demo-chat"


async def test_health_is_public_but_data_and_controls_require_auth(client):
    http, _ = client
    assert (await http.get("/healthz")).status_code == 200
    result = await http.get("/api/v1/connectors")
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "unauthorized"
    assert result.json()["error"]["request_id"]
    assert (
        await http.get("/api/v1/connectors", headers={"Authorization": "Bearer wrong"})
    ).status_code == 401


async def test_api_event_to_automatic_reply_and_control_conflicts(client):
    http, runtime = client
    accepted = await http.post("/api/v1/mock/events", json=body(), headers=HEADERS)
    assert accepted.status_code == 202
    assert accepted.headers["location"].startswith("/api/v1/events/")
    await runtime.drain()
    status = await http.get(accepted.headers["location"], headers=HEADERS)
    assert status.status_code == 200 and status.json()["data"]["status"] == "handled"
    delivered = await http.get(PATH + "/outbox", headers=HEADERS)
    assert delivered.json()["data"][0]["status"] == "sent"
    state = (await http.get(PATH, headers=HEADERS)).json()["data"]
    paused = await http.patch(
        PATH, json={"mode": "paused", "expected_revision": state["revision"]}, headers=HEADERS
    )
    assert paused.status_code == 200 and paused.json()["data"]["mode"] == "paused"
    stale = await http.patch(
        PATH, json={"mode": "auto", "expected_revision": state["revision"]}, headers=HEADERS
    )
    assert stale.status_code == 409


async def test_mock_ingress_cannot_impersonate_wechat_or_unknown_account(client):
    http, _ = client
    impersonated = await http.post(
        "/api/v1/mock/events", json=body(channel="wechat"), headers=HEADERS
    )
    assert impersonated.status_code == 422
    unknown = await http.post(
        "/api/v1/mock/events", json=body(account_id="unknown"), headers=HEADERS
    )
    assert unknown.status_code == 403
    connectors = (await http.get("/api/v1/connectors", headers=HEADERS)).json()["data"]
    wechat = next(item for item in connectors if item["channel"] == "wechat")
    assert wechat["available"] is False
    assert wechat["reason"] == "connector_not_implemented"


async def test_invalid_payload_and_duplicate_conflict_have_correct_statuses(client):
    http, _ = client
    assert (
        await http.post("/api/v1/mock/events", json=body(text=""), headers=HEADERS)
    ).status_code == 422
    assert (await http.post("/api/v1/mock/events", json=body(), headers=HEADERS)).status_code == 202
    duplicate = await http.post("/api/v1/mock/events", json=body(), headers=HEADERS)
    assert duplicate.status_code == 200 and duplicate.json()["data"]["duplicate"]
    conflict = await http.post("/api/v1/mock/events", json=body(text="不同"), headers=HEADERS)
    assert conflict.status_code == 409
    assert (await http.get(PATH + "/messages?limit=10001", headers=HEADERS)).status_code == 422


async def test_http_server_requires_explicit_management_secret(tmp_path):
    from bae_pilot.app import create_app
    from bae_pilot.config import Settings
    from bae_pilot.domain import AppError

    with pytest.raises(AppError, match="api_token_required"):
        create_app(Settings(db_path=tmp_path / "no-token.sqlite"))


@pytest.mark.parametrize(
    "method,path,code",
    [("GET", "/missing", "not_found"), ("DELETE", "/healthz", "method_not_allowed")],
)
async def test_routing_errors_use_safe_envelope(client, method, path, code):
    http, _ = client
    response = await http.request(method, path, headers=HEADERS)
    assert response.json()["error"]["code"] == code
    assert response.json()["error"]["request_id"] == response.headers["x-request-id"]


async def test_oversized_stream_is_rejected_with_security_headers(client):
    http, runtime = client

    async def chunks():
        yield b"x" * 40000
        yield b"y" * 40000

    result = await http.post("/api/v1/mock/events", content=chunks(), headers=HEADERS)
    assert result.status_code == 413
    assert result.json()["error"]["code"] == "request_too_large"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert result.headers["cache-control"] == "no-store"
    assert (await runtime.store.status())["conversations"] == []


async def test_message_pagination_has_no_gaps_or_repeats(client):
    http, _ = client
    for index in range(5):
        await http.post("/api/v1/mock/events", json=body(event_id=f"page-{index}"), headers=HEADERS)
    recent = (await http.get(PATH + "/messages?limit=2", headers=HEADERS)).json()
    assert [m["event_id"] for m in recent["data"]] == ["page-3", "page-4"]
    older = (
        await http.get(
            PATH + f"/messages?limit=2&before={recent['meta']['next_cursor']}", headers=HEADERS
        )
    ).json()
    assert [m["event_id"] for m in older["data"]] == ["page-1", "page-2"]
    last = (
        await http.get(
            PATH + f"/messages?limit=2&before={older['meta']['next_cursor']}", headers=HEADERS
        )
    ).json()
    assert [m["event_id"] for m in last["data"]] == ["page-0"]
    assert last["meta"] == {"has_more": False, "next_cursor": None}


async def test_validation_does_not_echo_private_input(client):
    http, _ = client
    result = await http.post(
        "/api/v1/mock/events", json=body(private_secret="do-not-echo-this"), headers=HEADERS
    )
    assert result.status_code == 422
    assert "do-not-echo-this" not in result.text


async def test_api_limiter_counts_invalid_authentication_attempts(client):
    http, _ = client
    for _ in range(120):
        assert (await http.get("/api/v1/status")).status_code == 401
    result = await http.get("/api/v1/status", headers=HEADERS)
    assert result.status_code == 429
    assert result.headers["retry-after"] == "60"


async def test_internal_error_is_contained_without_secret_traceback(client, monkeypatch, caplog):
    http, runtime = client

    async def broken_status():
        raise ValueError("private-chat-must-not-be-logged")

    monkeypatch.setattr(runtime.store, "status", broken_status)
    # ASGITransport propagates escaped server exceptions by default; containment is part of the contract.
    result = await http.get("/api/v1/status", headers=HEADERS)
    assert result.status_code == 500
    assert result.json()["error"]["code"] == "internal_error"
    assert result.headers["cache-control"] == "no-store"
    assert "private-chat" not in result.text + caplog.text
    assert all(record.exc_info is None for record in caplog.records)


async def test_non_ascii_bearer_is_rejected_not_a_server_error(client):
    http, _ = client
    result = await http.get("/api/v1/status", headers={b"Authorization": b"Bearer \xff"})
    assert result.status_code == 401
    assert result.json()["error"]["code"] == "unauthorized"
