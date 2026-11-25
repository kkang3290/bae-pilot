"""Deliberately synthetic end-to-end demonstration; never inherits real-model settings."""

import os
import uuid
from contextlib import asynccontextmanager

import psycopg
from psycopg import sql

from bae_pilot.config import Settings
from bae_pilot.connectors.mock import MockConnector
from bae_pilot.domain import AppError, IncomingEvent
from bae_pilot.services.runtime import Runtime


def require(condition):
    if not condition:
        raise AppError("demo_check_failed", 500)
    return True


@asynccontextmanager
async def temporary_demo_database():
    """Only a freshly created, marked schema can be removed; never use the configured schema."""
    schema = "baepilot_demo_" + uuid.uuid4().hex
    owner = "baepilot-demo:" + uuid.uuid4().hex
    database = {}
    if "BAEPILOT_DATABASE_URL" in os.environ:
        database["database_url"] = os.environ["BAEPILOT_DATABASE_URL"]
    settings = Settings(
        **database, database_schema=schema, provider="fake", debounce_s=0, min_send_interval_s=0
    )
    connection = None
    try:
        connection = await psycopg.AsyncConnection.connect(
            settings.database_url.get_secret_value(), autocommit=True, connect_timeout=5
        )
        async with connection.transaction():
            await connection.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
            await connection.execute(
                sql.SQL("COMMENT ON SCHEMA {} IS {}").format(
                    sql.Identifier(schema), sql.Literal(owner)
                )
            )
        try:
            yield settings
        finally:
            async with connection.transaction():
                cursor = await connection.execute(
                    "SELECT obj_description(oid, 'pg_namespace') FROM pg_namespace "
                    "WHERE nspname=%s AND nspowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)",
                    (schema,),
                )
                marker = await cursor.fetchone()
                await cursor.close()
                if marker != (owner,):
                    raise AppError("demo_schema_ownership_lost", 409)
                await connection.execute(
                    sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema))
                )
    except psycopg.Error:
        raise AppError("database_unavailable", 503) from None
    finally:
        if connection is not None:
            await connection.close()


async def run_demo():
    async with temporary_demo_database() as settings:
        runtime = Runtime(settings)
        await runtime.start(worker=False)
        checks = {}
        try:
            first = IncomingEvent(
                account_id="demo-account",
                conversation_id="demo-chat",
                event_id="demo-1",
                sender_id="peer",
                text="你好",
            )
            await runtime.ingest(first)
            await runtime.drain()
            deliveries = await runtime.store.outbox(first.key)
            checks["automatic_reply"] = require(
                len(deliveries) == 1 and deliveries[0]["status"] == "sent"
            )
            repeated = await runtime.ingest(first)
            await runtime.drain()
            checks["duplicate_event"] = require(
                repeated.duplicate and len(await runtime.store.outbox(first.key)) == 1
            )
            await runtime.ingest(
                first.model_copy(
                    update={"event_id": "demo-2", "text": "你每次都说下次注意，我很失望"}
                )
            )
            await runtime.drain()
            deep = await runtime.store.latest_run(first.key)
            checks["complex_path"] = require(
                deep["result"]["trace"]
                == ["understanding", "strategy", "talk", "review", "validate"]
            )
            manual = IncomingEvent(
                account_id="demo-account",
                conversation_id="demo-chat",
                event_id="demo-human",
                sender_id="self",
                direction="outbound",
                origin="human",
                text="这次由本人回复",
            )
            await runtime.ingest(manual)
            await runtime.ingest(first.model_copy(update={"event_id": "demo-3"}))
            await runtime.drain()
            state = await runtime.store.conversation(first.key)
            checks["human_handoff"] = require(
                state["mode"] == "handoff" and len(await runtime.store.outbox(first.key)) == 2
            )
            await runtime.change_mode(first.key, "auto", state["revision"])

            class LostAcknowledgement(MockConnector):
                async def send(self, intent):
                    await super().send(intent)
                    raise TimeoutError("synthetic acknowledgement loss")

            runtime.connectors["mock"] = LostAcknowledgement(runtime.store)
            await runtime.ingest(first.model_copy(update={"event_id": "demo-4"}))
            await runtime.drain()
            unknown = (await runtime.store.outbox(first.key))[-1]
            require(unknown["status"] == "unknown")
            reconciled = await runtime.reconcile(unknown["send_id"])
            await runtime.drain()
            checks["unknown_delivery"] = require(
                reconciled["status"] == "sent" and len(await runtime.store.outbox(first.key)) == 3
            )
            return {
                "mode": "synthetic",
                "database": "postgresql",
                "provider": "fake",
                "connector": "mock",
                "checks": checks,
                "deliveries": len(await runtime.store.outbox(first.key)),
                "note": "仅验证框架流程；未连接微信、未调用真实模型。",
            }
        finally:
            await runtime.close()
