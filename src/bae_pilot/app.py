"""Authenticated loopback management API; channel events are bound at ingress."""

import hmac
import logging
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, FastAPI, Header, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import Field
from starlette.exceptions import HTTPException

from bae_pilot.domain import AppError, Contract, ConversationKey, Identifier, IncomingEvent, Mode
from bae_pilot.services.runtime import Runtime

logger = logging.getLogger(__name__)


def error_response(code, status, request_id, *, message=None, details=None, headers=None):
    error = {
        "code": code,
        "message": message if message is not None else code,
        "request_id": request_id,
    }
    if details is not None:
        error["details"] = details
    return JSONResponse({"error": error}, status_code=status, headers=headers)


def page_response(rows, limit, cursor_field):
    more = len(rows) > limit
    rows = rows[-limit:]
    return {
        "data": rows,
        "meta": {"has_more": more, "next_cursor": rows[0][cursor_field] if more else None},
    }


class ModeChange(Contract):
    mode: Mode
    expected_revision: int = Field(ge=0)


class ControlChange(Contract):
    mode: Literal["auto", "paused", "stopped"]
    expected_revision: int = Field(ge=0)
    channel: Identifier | None = None
    account_id: Identifier | None = None


class Reconciliation(Contract):
    confirmed_status: Literal["sent", "failed"] | None = None


class RequestBoundary:
    """Bound actual streamed bytes before parsing, not only Content-Length."""

    def __init__(self, app, max_bytes=65536):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id

        response_started = False

        async def secure_send(message):
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
                message["headers"] = [
                    *message.get("headers", []),
                    (b"x-request-id", request_id.encode()),
                    (b"x-content-type-options", b"nosniff"),
                    (b"cache-control", b"no-store"),
                    (b"x-frame-options", b"DENY"),
                    (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
                ]
            await send(message)

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                response = error_response("request_too_large", 413, request_id)
                return await response(scope, receive, secure_send)
            if not message.get("more_body", False):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        try:
            await self.app(scope, replay, secure_send)
        except Exception as exc:
            # Contain before ServerErrorMiddleware can re-raise private exception text to Uvicorn.
            logger.error(
                "request_failed", extra={"error_code": type(exc).__name__, "request_id": request_id}
            )
            if response_started:
                raise RuntimeError("response_interrupted") from None
            response = error_response("internal_error", 500, request_id)
            await response(scope, receive, secure_send)


def create_app(settings, *, start_worker=True):
    token = settings.api_token.get_secret_value() if settings.api_token else ""
    if len(token) < 16 or token == "replace-with-a-random-local-token":
        raise AppError("api_token_required", 400)

    @asynccontextmanager
    async def lifespan(app):
        runtime = Runtime(settings)
        await runtime.start(worker=start_worker)
        app.state.runtime = runtime
        try:
            yield
        finally:
            await runtime.close()

    app = FastAPI(
        title="BaePilot",
        version="0.1.0",
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(RequestBoundary)
    attempts = deque()

    async def authenticate(authorization: Annotated[str | None, Header()] = None):
        now = time.monotonic()
        while attempts and attempts[0] <= now - 60:
            attempts.popleft()
        if len(attempts) >= 120:
            raise AppError("rate_limited", 429)
        attempts.append(now)
        supplied = (
            authorization.removeprefix("Bearer ")
            if authorization and authorization.startswith("Bearer ")
            else ""
        )
        if not hmac.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
            raise AppError("unauthorized", 401)

    @app.exception_handler(AppError)
    async def app_error(request: Request, exc: AppError):
        headers = {"Retry-After": "60"} if exc.status == 429 else {}
        return error_response(exc.code, exc.status, request.state.request_id, headers=headers)

    @app.exception_handler(HTTPException)
    async def routing_error(request: Request, exc: HTTPException):
        code = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return error_response(code, exc.status_code, request.state.request_id, headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        details = [
            {"field": ".".join(str(x) for x in item["loc"]), "code": item["type"]}
            for item in exc.errors()[:20]
        ]
        return error_response(
            "validation_error",
            422,
            request.state.request_id,
            message="invalid_request",
            details=details,
        )

    @app.get("/healthz")
    async def health():
        return {"status": "ok", "mode": "local", "provider": settings.provider}

    api = APIRouter(prefix="/api/v1", dependencies=[Depends(authenticate)])

    @api.get("/openapi.json")
    async def openapi():
        return app.openapi()

    @api.get("/connectors")
    async def connectors(request: Request):
        return {
            "data": [
                c.capabilities.model_dump() for c in request.app.state.runtime.connectors.values()
            ]
        }

    @api.get("/status")
    async def status(request: Request):
        return {"data": await request.app.state.runtime.store.status()}

    @api.post("/mock/events")
    async def accept(event: IncomingEvent, request: Request):
        result = await request.app.state.runtime.ingest(event, bound_channel="mock")
        return JSONResponse(
            {"data": result.model_dump()},
            status_code=200 if result.duplicate else 202,
            headers={"Location": f"/api/v1/events/{result.record_id}"},
        )

    @api.get("/events/{record_id}")
    async def get_event(record_id: Annotated[int, Path(ge=1)], request: Request):
        return {"data": await request.app.state.runtime.store.event(record_id)}

    base = "/channels/{channel}/accounts/{account_id}/conversations/{conversation_id}"

    def key_of(channel, account_id, conversation_id):
        return ConversationKey(
            channel=channel, account_id=account_id, conversation_id=conversation_id
        )

    @api.get(base)
    async def conversation(
        channel: Identifier, account_id: Identifier, conversation_id: Identifier, request: Request
    ):
        key = key_of(channel, account_id, conversation_id)
        return {"data": await request.app.state.runtime.store.conversation(key)}

    @api.patch(base)
    async def change_mode(
        channel: Identifier,
        account_id: Identifier,
        conversation_id: Identifier,
        body: ModeChange,
        request: Request,
    ):
        return {
            "data": await request.app.state.runtime.change_mode(
                key_of(channel, account_id, conversation_id), body.mode, body.expected_revision
            )
        }

    @api.get(base + "/messages")
    async def messages(
        channel: Identifier,
        account_id: Identifier,
        conversation_id: Identifier,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: Annotated[int | None, Query(ge=1)] = None,
    ):
        rows = await request.app.state.runtime.store.messages(
            key_of(channel, account_id, conversation_id), limit + 1, before
        )
        return page_response(rows, limit, "record_id")

    @api.get(base + "/outbox")
    async def outbox(
        channel: Identifier,
        account_id: Identifier,
        conversation_id: Identifier,
        request: Request,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        before: Annotated[int | None, Query(ge=1)] = None,
    ):
        rows = await request.app.state.runtime.store.outbox(
            key_of(channel, account_id, conversation_id), limit + 1, before
        )
        return page_response(rows, limit, "cursor")

    @api.get(base + "/memories")
    async def memories(
        channel: Identifier, account_id: Identifier, conversation_id: Identifier, request: Request
    ):
        return {
            "data": await request.app.state.runtime.store.memories(
                key_of(channel, account_id, conversation_id)
            )
        }

    @api.get(base + "/runs")
    async def runs(
        channel: Identifier, account_id: Identifier, conversation_id: Identifier, request: Request
    ):
        return {
            "data": await request.app.state.runtime.store.latest_run(
                key_of(channel, account_id, conversation_id)
            )
        }

    @api.patch("/runtime-controls")
    async def controls(body: ControlChange, request: Request):
        return {
            "data": await request.app.state.runtime.change_global(
                body.mode, body.expected_revision, channel=body.channel, account_id=body.account_id
            )
        }

    @api.post("/outbox/{send_id}/reconciliations")
    async def reconcile(send_id: Identifier, body: Reconciliation, request: Request):
        return {"data": await request.app.state.runtime.reconcile(send_id, body.confirmed_status)}

    app.include_router(api)
    return app
