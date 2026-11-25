"""Short PostgreSQL transactions; never await a model or channel in a transaction."""

import asyncio
import json
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from bae_pilot.domain import Acceptance, AppError, ConversationKey, IncomingEvent


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def account_scope(key):
    return encode([key.channel, key.account_id])


def timestamp(value):
    return datetime.fromtimestamp(value, UTC)


def record(row):
    """Keep the existing epoch-seconds application/API contract, with native SQL timestamps."""
    return {
        key: value.timestamp() if isinstance(value, datetime) else value
        for key, value in row.items()
    }


class Store:
    def __init__(self, connection):
        self.db = connection
        self._gate = asyncio.Lock()

    @classmethod
    async def open(cls, settings):
        connection = None
        try:
            connection = await psycopg.AsyncConnection.connect(
                settings.database_url.get_secret_value(),
                autocommit=True,
                row_factory=dict_row,
                connect_timeout=5,
                application_name="baepilot",
            )
            store = cls(connection)
            # A dedicated connection owns this lock for the Store's entire lifetime.
            # No pooling/reconnect: a lost lock session must not silently resume sending.
            locked = await store._one(
                "SELECT pg_try_advisory_lock(hashtextextended(current_database() || ':' || %s, 0)) AS locked",
                (settings.database_schema,),
            )
            if not locked["locked"]:
                raise AppError("database_in_use", 409)
            await store._execute("SET statement_timeout = '10s'")
            await store._execute("SET lock_timeout = '5s'")
            await store._execute("SET idle_in_transaction_session_timeout = '15s'")
            async with store.transaction():
                await store._execute(
                    sql.SQL("CREATE SCHEMA IF NOT EXISTS {}").format(
                        sql.Identifier(settings.database_schema)
                    )
                )
                await store._execute(
                    sql.SQL("SET search_path TO {}, pg_catalog").format(
                        sql.Identifier(settings.database_schema)
                    )
                )
                tables = await store._all(
                    "SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname=%s",
                    (settings.database_schema,),
                )
                if not tables:
                    schema = await asyncio.to_thread(
                        Path(__file__).with_name("schema.sql").read_text
                    )
                    await store._execute(schema)
                elif any(table["tablename"] == "schema_version" for table in tables):
                    versions = await store._all("SELECT version FROM schema_version")
                    if versions != [{"version": 1}]:
                        raise AppError("unsupported_database_version", 503)
                else:
                    raise AppError("unsupported_database_version", 503)
            return store
        except BaseException as exc:
            if connection is not None:
                await connection.close()
            if isinstance(exc, psycopg.Error):
                raise AppError("database_unavailable", 503) from None
            raise

    async def close(self):
        await self.db.close()

    @asynccontextmanager
    async def transaction(self):
        async with self._gate:
            try:
                async with self.db.transaction():
                    yield
            except psycopg.Error:
                raise AppError("database_error", 503) from None

    async def _execute(self, query, args=None):
        async with self.db.cursor() as cursor:
            await cursor.execute(query, args)

    async def _one(self, query, args=None):
        async with self.db.cursor() as cursor:
            await cursor.execute(query, args)
            row = await cursor.fetchone()
            return record(row) if row is not None else None

    async def _all(self, query, args=None):
        async with self.db.cursor() as cursor:
            await cursor.execute(query, args)
            return [record(row) for row in await cursor.fetchall()]

    async def _audit(self, scope, action, now, **details):
        await self._execute(
            "INSERT INTO audit_log(scope,action,details,created_at) VALUES(%s,%s,%s,%s)",
            (scope, action, Jsonb(details), timestamp(now)),
        )

    async def _open(self, key):
        controls = await self._all(
            "SELECT mode FROM controls WHERE scope IN (%s,%s)", ("*", account_scope(key))
        )
        return all(item["mode"] == "auto" for item in controls)

    async def _unknown(self, scope):
        return (
            await self._one(
                "SELECT send_id FROM outbox WHERE scope=%s AND status='unknown' LIMIT 1", (scope,)
            )
            is not None
        )

    async def _cancel(self, scopes, status="cancelled"):
        await self._execute(
            "UPDATE outbox SET status=%s WHERE scope=ANY(%s::text[]) AND status='pending'",
            (status, scopes),
        )
        if status == "cancelled":
            await self._execute(
                "UPDATE jobs SET status='cancelled' WHERE scope=ANY(%s::text[]) AND status='pending'",
                (scopes,),
            )
            await self._execute(
                "UPDATE events SET status='ignored_control' WHERE scope=ANY(%s::text[]) AND status='queued'",
                (scopes,),
            )

    async def _enqueue(self, kind, scope, now, payload=None, debounce=0, max_wait=0, capacity=1000):
        current = await self._one(
            "SELECT * FROM jobs WHERE kind=%s AND scope=%s AND status='pending'", (kind, scope)
        )
        if current:
            due = min(now + debounce, current["first_at"] + max_wait)
            if kind == "memory" and current["payload"].get("source_version", 0) > (
                payload or {}
            ).get("source_version", 0):
                return
            await self._execute(
                "UPDATE jobs SET due_at=%s,payload=%s WHERE job_id=%s",
                (timestamp(due), Jsonb(payload or {}), current["job_id"]),
            )
        else:
            count = await self._one(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')"
            )
            if count["n"] >= capacity:
                raise AppError("queue_full", 503)
            await self._execute(
                "INSERT INTO jobs(kind,scope,status,due_at,first_at,payload) VALUES(%s,%s,'pending',%s,%s,%s)",
                (kind, scope, timestamp(now + debounce), timestamp(now), Jsonb(payload or {})),
            )

    async def ingest(self, event, *, initial_mode, authorized, now, debounce, max_wait, capacity):
        key, scope = event.key, event.key.scope
        async with self.transaction():
            existing = await self._one(
                "SELECT record_id,payload_hash,status FROM events WHERE scope=%s AND event_id=%s",
                (scope, event.event_id),
            )
            if existing:
                if existing["payload_hash"] != event.payload_hash:
                    raise AppError("event_id_conflict")
                return Acceptance(
                    record_id=existing["record_id"], status=existing["status"], duplicate=True
                )
            await self._execute(
                "INSERT INTO conversations(scope,channel,account_id,conversation_id,mode) VALUES(%s,%s,%s,%s,%s) ON CONFLICT (scope) DO NOTHING",
                (scope, key.channel, key.account_id, key.conversation_id, initial_mode),
            )
            conversation = await self._one("SELECT * FROM conversations WHERE scope=%s", (scope,))
            echo = None
            if event.origin == "bot":
                echo = await self._one(
                    "SELECT send_id FROM outbox WHERE send_id=%s AND scope=%s AND text=%s AND status IN ('sending','sent','unknown')",
                    (event.client_send_id, scope, event.text),
                )
            unsupported = event.message_type != "text" or event.conversation_type != "direct"
            if echo:
                status = "echo"
            elif event.direction == "outbound" or unsupported:
                status = "handoff" if not unsupported else "unsupported"
                await self._execute(
                    "UPDATE conversations SET mode='handoff',revision=revision+1 WHERE scope=%s",
                    (scope,),
                )
                await self._cancel([scope])
            else:
                eligible = (
                    authorized
                    and conversation["mode"] == "auto"
                    and await self._open(key)
                    and not await self._unknown(scope)
                )
                status = "queued" if eligible else "ignored_mode"
                await self._execute(
                    "UPDATE conversations SET revision=revision+1 WHERE scope=%s", (scope,)
                )
                await self._cancel([scope], "superseded")
            inserted = await self._one(
                "INSERT INTO events(scope,event_id,payload_hash,payload,direction,text,status,received_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING record_id",
                (
                    scope,
                    event.event_id,
                    event.payload_hash,
                    Jsonb(event.model_dump(mode="json")),
                    event.direction,
                    event.text,
                    status,
                    timestamp(now),
                ),
            )
            record_id = inserted["record_id"]
            if status == "queued":
                await self._enqueue(
                    "process", scope, now, debounce=debounce, max_wait=max_wait, capacity=capacity
                )
            return Acceptance(record_id=record_id, status=status)

    async def conversation(self, key):
        async with self._gate:
            result = await self._one("SELECT * FROM conversations WHERE scope=%s", (key.scope,))
            if not result:
                raise AppError("conversation_not_found", 404)
            result["in_flight"] = (
                await self._one(
                    "SELECT COUNT(*) AS n FROM outbox WHERE scope=%s AND status='sending'",
                    (key.scope,),
                )
            )["n"]
            result["unknown_deliveries"] = await self._unknown(key.scope)
            return result

    async def event(self, record_id):
        async with self._gate:
            result = await self._one("SELECT * FROM events WHERE record_id=%s", (record_id,))
            if not result:
                raise AppError("event_not_found", 404)
            return result

    async def messages(self, key, limit=50, before=None):
        async with self._gate:
            rows = await self._all(
                "SELECT * FROM events WHERE scope=%s AND record_id<%s ORDER BY record_id DESC LIMIT %s",
                (key.scope, before or 9223372036854775807, limit),
            )
            return [
                {
                    **row["payload"],
                    "record_id": row["record_id"],
                    "status": row["status"],
                }
                for row in reversed(rows)
            ]

    async def outbox(self, key, limit=50, before=None):
        async with self._gate:
            rows = await self._all(
                "SELECT * FROM outbox WHERE scope=%s AND cursor<%s ORDER BY cursor DESC LIMIT %s",
                (key.scope, before or 9223372036854775807, limit),
            )
            return list(reversed(rows))

    async def delivery(self, send_id):
        async with self._gate:
            result = await self._one("SELECT * FROM outbox WHERE send_id=%s", (send_id,))
            if not result:
                raise AppError("delivery_not_found", 404)
            return result

    async def latest_run(self, key):
        async with self._gate:
            row = await self._one(
                "SELECT * FROM runs WHERE scope=%s ORDER BY run_seq DESC LIMIT 1", (key.scope,)
            )
            return row

    async def change_mode(self, key, mode, expected_revision, now):
        async with self.transaction():
            row = await self._one("SELECT * FROM conversations WHERE scope=%s", (key.scope,))
            if not row:
                raise AppError("conversation_not_found", 404)
            if row["revision"] != expected_revision:
                raise AppError("revision_conflict")
            await self._execute(
                "UPDATE conversations SET mode=%s,revision=revision+1 WHERE scope=%s",
                (mode, key.scope),
            )
            await self._cancel([key.scope])
            await self._audit(key.scope, "mode_changed", now, mode=mode)
        return await self.conversation(key)

    async def change_control(self, mode, expected_revision, now, key=None):
        scope = account_scope(key) if key else "*"
        async with self.transaction():
            await self._execute(
                "INSERT INTO controls VALUES(%s,'auto',0) ON CONFLICT (scope) DO NOTHING", (scope,)
            )
            row = await self._one("SELECT * FROM controls WHERE scope=%s", (scope,))
            if row["revision"] != expected_revision:
                raise AppError("revision_conflict")
            await self._execute(
                "UPDATE controls SET mode=%s,revision=revision+1 WHERE scope=%s", (mode, scope)
            )
            conversations = await self._all(
                "SELECT scope FROM conversations"
                if key is None
                else "SELECT scope FROM conversations WHERE channel=%s AND account_id=%s",
                () if key is None else (key.channel, key.account_id),
            )
            scopes = [item["scope"] for item in conversations]
            await self._execute(
                "UPDATE conversations SET revision=revision+1 WHERE scope=ANY(%s::text[])",
                (scopes,),
            )
            await self._cancel(scopes)
            await self._audit(scope, "control_changed", now, mode=mode)
            result = await self._one("SELECT * FROM controls WHERE scope=%s", (scope,))
            result["in_flight"] = (
                await self._one(
                    "SELECT COUNT(*) AS n FROM outbox WHERE status='sending'"
                    if key is None
                    else "SELECT COUNT(*) AS n FROM outbox o JOIN conversations c ON c.scope=o.scope WHERE o.status='sending' AND c.channel=%s AND c.account_id=%s",
                    () if key is None else (key.channel, key.account_id),
                )
            )["n"]
            return result

    async def recover(self, now):
        async with self.transaction():
            await self._execute(
                "UPDATE outbox SET status='unknown',error='interrupted_send' WHERE status='sending'"
            )
            await self._execute(
                "UPDATE runs SET status='interrupted',error='interrupted_generation' WHERE status='running'"
            )
            running = await self._all("SELECT * FROM jobs WHERE status='running'")
            for job in running:
                pending = await self._one(
                    "SELECT job_id FROM jobs WHERE kind=%s AND scope=%s AND status='pending'",
                    (job["kind"], job["scope"]),
                )
                await self._execute(
                    "UPDATE jobs SET status=%s,due_at=%s WHERE job_id=%s",
                    ("cancelled" if pending else "pending", timestamp(now), job["job_id"]),
                )

    async def due_jobs(self, now):
        async with self._gate:
            return await self._all(
                "SELECT * FROM jobs WHERE status='pending' AND due_at<=%s ORDER BY (kind='process') DESC,due_at,job_id LIMIT 100",
                (timestamp(now),),
            )

    async def claim_job(self, job_id):
        async with self.transaction():
            claimed = await self._one(
                "UPDATE jobs SET status='running',attempts=attempts+1 WHERE job_id=%s AND status='pending' RETURNING job_id",
                (job_id,),
            )
            return claimed is not None

    async def finish_job(self, job_id, *, error=None, retry_at=None):
        async with self.transaction():
            job = await self._one("SELECT * FROM jobs WHERE job_id=%s", (job_id,))
            pending = await self._one(
                "SELECT job_id FROM jobs WHERE kind=%s AND scope=%s AND status='pending'",
                (job["kind"], job["scope"]),
            )
            status = "done" if error is None else "failed"
            if error and retry_at is not None and not pending:
                status = "pending"
            await self._execute(
                "UPDATE jobs SET status=%s,error=%s,due_at=%s WHERE job_id=%s",
                (
                    status,
                    error,
                    timestamp(retry_at if retry_at is not None else job["due_at"]),
                    job_id,
                ),
            )

    async def snapshot(self, key, now, limit):
        async with self.transaction():
            row = await self._one("SELECT * FROM conversations WHERE scope=%s", (key.scope,))
            pending = await self._one(
                "SELECT MAX(record_id) AS last FROM events WHERE scope=%s AND status='queued'",
                (key.scope,),
            )
            if (
                not row
                or not pending["last"]
                or row["mode"] != "auto"
                or not await self._open(key)
                or await self._unknown(key.scope)
            ):
                return None
            rows = await self._all(
                "SELECT * FROM events WHERE scope=%s AND status!='echo' ORDER BY record_id DESC LIMIT %s",
                (key.scope, limit),
            )
            run_id = uuid.uuid4().hex
            await self._execute(
                "INSERT INTO runs(run_id,scope,revision,last_event_id,status,created_at) VALUES(%s,%s,%s,%s,'running',%s)",
                (run_id, key.scope, row["revision"], pending["last"], timestamp(now)),
            )
            return {
                "run_id": run_id,
                "revision": row["revision"],
                "last_event_id": pending["last"],
                "messages": [
                    {**item["payload"], "record_id": item["record_id"]} for item in reversed(rows)
                ],
                "memories": await self._memories(key.scope),
                "memory_version": row["memory_version"],
                "history_window": limit,
            }

    async def finish_run(self, key, snapshot, result, now, ttl, min_interval):
        async with self.transaction():
            row = await self._one("SELECT * FROM conversations WHERE scope=%s", (key.scope,))
            current = (
                row["revision"] == snapshot["revision"]
                and row["mode"] == "auto"
                and await self._open(key)
                and not await self._unknown(key.scope)
            )
            await self._execute(
                "UPDATE runs SET status=%s,result=%s WHERE run_id=%s",
                ("completed" if current else "superseded", Jsonb(result), snapshot["run_id"]),
            )
            if not current:
                return None
            await self._execute(
                "UPDATE events SET status='handled' WHERE scope=%s AND record_id<=%s AND status='queued'",
                (key.scope, snapshot["last_event_id"]),
            )
            await self._execute(
                "UPDATE conversations SET last_processed=%s WHERE scope=%s",
                (snapshot["last_event_id"], key.scope),
            )
            if result["status"] == "handoff":
                await self._execute(
                    "UPDATE conversations SET mode='handoff',revision=revision+1 WHERE scope=%s",
                    (key.scope,),
                )
                await self._cancel([key.scope])
            if result["status"] != "reply":
                return None
            send_id = uuid.uuid4().hex
            previous = await self._one(
                "SELECT MAX(sent_at) AS last FROM outbox WHERE scope=%s AND status='sent'",
                (key.scope,),
            )
            not_before = max(now, (previous["last"] or 0) + min_interval)
            await self._execute(
                "INSERT INTO outbox(send_id,scope,run_id,source_revision,text,status,created_at,expires_at,not_before) VALUES(%s,%s,%s,%s,%s,'pending',%s,%s,%s)",
                (
                    send_id,
                    key.scope,
                    snapshot["run_id"],
                    snapshot["revision"],
                    result["reply"],
                    timestamp(now),
                    timestamp(now + ttl),
                    timestamp(not_before),
                ),
            )
            return send_id

    async def fail_run(self, run_id, error):
        async with self.transaction():
            await self._execute(
                "UPDATE runs SET status='failed',error=%s WHERE run_id=%s", (error, run_id)
            )

    async def pending_deliveries(self, now):
        async with self._gate:
            return await self._all(
                "SELECT * FROM outbox WHERE status='pending' AND not_before<=%s ORDER BY created_at LIMIT 100",
                (timestamp(now),),
            )

    async def claim_delivery(self, send_id, authorized, now):
        async with self.transaction():
            row = await self._one("SELECT * FROM outbox WHERE send_id=%s", (send_id,))
            if not row or row["status"] != "pending" or row["not_before"] > now:
                return None
            key = ConversationKey.from_scope(row["scope"])
            conversation = await self._one(
                "SELECT * FROM conversations WHERE scope=%s", (key.scope,)
            )
            error = None
            if not authorized or conversation["mode"] != "auto" or not await self._open(key):
                error = "not_authorized"
            elif conversation["revision"] != row["source_revision"]:
                error = "stale_context"
            elif now >= row["expires_at"]:
                error = "reply_expired"
            elif await self._unknown(key.scope):
                return None
            if error:
                await self._execute(
                    "UPDATE outbox SET status='cancelled',error=%s WHERE send_id=%s",
                    (error, send_id),
                )
                return None
            await self._execute("UPDATE outbox SET status='sending' WHERE send_id=%s", (send_id,))
            return row

    async def record_delivery(
        self, send_id, result, self_id, now, capacity=1000, reconciliation=False
    ):
        async with self.transaction():
            row = await self._one("SELECT * FROM outbox WHERE send_id=%s", (send_id,))
            if not row:
                raise AppError("delivery_not_found", 404)
            if row["status"] not in ("sending", "unknown"):
                return row
            await self._execute(
                "UPDATE outbox SET status=%s,external_message_id=%s,error=%s,sent_at=CASE WHEN %s='sent' THEN %s ELSE sent_at END WHERE send_id=%s",
                (
                    result.status,
                    result.external_message_id,
                    result.reason,
                    result.status,
                    timestamp(now),
                    send_id,
                ),
            )
            if result.status == "sent":
                key = ConversationKey.from_scope(row["scope"])
                event = IncomingEvent(
                    **key.model_dump(),
                    event_id="sent-" + send_id,
                    sender_id=self_id,
                    direction="outbound",
                    origin="bot",
                    text=row["text"],
                    client_send_id=send_id,
                )
                inserted = await self._one(
                    "INSERT INTO events(scope,event_id,payload_hash,payload,direction,text,status,received_at) VALUES(%s,%s,%s,%s,%s,%s,'sent',%s) RETURNING record_id",
                    (
                        key.scope,
                        event.event_id,
                        event.payload_hash,
                        Jsonb(event.model_dump(mode="json")),
                        "outbound",
                        event.text,
                        timestamp(now),
                    ),
                )
                version = inserted["record_id"]
                await self._execute(
                    "UPDATE conversations SET revision=revision+1 WHERE scope=%s", (key.scope,)
                )
                try:
                    await self._enqueue(
                        "memory", key.scope, now, {"source_version": version}, capacity=capacity
                    )
                except AppError as exc:
                    if exc.code != "queue_full":
                        raise
                    await self._audit(key.scope, "memory_queue_full", now)
            await self._audit(
                row["scope"],
                "delivery_reconciled" if reconciliation else "delivery_result",
                now,
                send_id=send_id,
                status=result.status,
            )
        return await self.delivery(send_id)

    async def mock_send(self, intent, now):
        async with self.transaction():
            row = await self._one("SELECT * FROM mock_receipts WHERE send_id=%s", (intent.send_id,))
            if row and (row["scope"] != intent.key.scope or row["text"] != intent.text):
                raise AppError("send_id_conflict")
            if not row:
                await self._execute(
                    "INSERT INTO mock_receipts VALUES(%s,%s,%s,%s,%s)",
                    (
                        intent.send_id,
                        intent.key.scope,
                        intent.text,
                        "mock-" + intent.send_id,
                        timestamp(now),
                    ),
                )
            return "mock-" + intent.send_id

    async def mock_receipt(self, send_id):
        async with self._gate:
            return await self._one("SELECT * FROM mock_receipts WHERE send_id=%s", (send_id,))

    async def _memories(self, scope):
        return await self._all("SELECT * FROM memories WHERE scope=%s ORDER BY memory_id", (scope,))

    async def memories(self, key):
        async with self._gate:
            return await self._memories(key.scope)

    async def memory_context(self, key, version, limit):
        async with self._gate:
            rows = await self._all(
                "SELECT * FROM events WHERE scope=%s AND record_id<=%s AND status!='echo' ORDER BY record_id DESC LIMIT %s",
                (key.scope, version, limit),
            )
            return [{**row["payload"], "record_id": row["record_id"]} for row in reversed(rows)]

    async def save_memories(self, key, version, items, now):
        async with self.transaction():
            row = await self._one(
                "SELECT memory_version FROM conversations WHERE scope=%s", (key.scope,)
            )
            if row["memory_version"] >= version:
                return
            if any(not item.source_event_ids for item in items):
                raise AppError("invalid_memory_evidence", 502)
            cited = sorted({source for item in items for source in item.source_event_ids})
            allowed = await self._all(
                "SELECT record_id FROM events WHERE scope=%s AND record_id<=%s AND record_id=ANY(%s)",
                (key.scope, version, cited),
            )
            if {row["record_id"] for row in allowed} != set(cited):
                raise AppError("invalid_memory_evidence", 502)
            await self._execute("DELETE FROM memories WHERE scope=%s", (key.scope,))
            created_at = timestamp(now)
            async with self.db.cursor() as cursor:
                await cursor.executemany(
                    "INSERT INTO memories(scope,kind,content,source_event_ids,source_version,created_at) VALUES(%s,%s,%s,%s,%s,%s)",
                    [
                        (
                            key.scope,
                            item.kind,
                            item.content,
                            item.source_event_ids,
                            version,
                            created_at,
                        )
                        for item in items
                    ],
                )
            await self._execute(
                "UPDATE conversations SET memory_version=%s WHERE scope=%s", (version, key.scope)
            )

    async def status(self):
        async with self._gate:
            return {
                "conversations": await self._all("SELECT * FROM conversations"),
                "controls": await self._all("SELECT * FROM controls"),
                "jobs": await self._all(
                    "SELECT status,COUNT(*) AS count FROM jobs GROUP BY status"
                ),
                "deliveries": await self._all(
                    "SELECT status,COUNT(*) AS count FROM outbox GROUP BY status"
                ),
            }
