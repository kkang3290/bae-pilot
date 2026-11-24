"""Short, serialized SQLite transactions; never await a model or channel in a transaction."""

import asyncio
import json
import os
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from bae_pilot.domain import Acceptance, AppError, ConversationKey, IncomingEvent


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def account_scope(key):
    return encode([key.channel, key.account_id])


class Store:
    def __init__(self, connection, lock_file):
        self.db = connection
        self._file = lock_file
        self._gate = asyncio.Lock()
        self._closed = False

    @classmethod
    async def open(cls, path: Path):
        if await asyncio.to_thread(path.is_symlink):
            raise AppError("unsafe_database_path", 400)
        path = await asyncio.to_thread(path.resolve)
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock_path = path.with_suffix(path.suffix + ".lock")
        if path.is_symlink() or lock_path.is_symlink():
            raise AppError("unsafe_database_path", 400)
        handle = os.fdopen(os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600), "a+b")
        try:
            if os.name == "nt":
                import msvcrt

                handle.write(b"0")
                handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise AppError("database_in_use", 409) from exc
        connection = None
        try:
            os.close(os.open(path, os.O_CREAT | os.O_RDWR, 0o600))
            connection = await aiosqlite.connect(path, isolation_level=None)
            connection.row_factory = aiosqlite.Row
            await connection.execute("PRAGMA journal_mode=WAL")
            await connection.execute("PRAGMA foreign_keys=ON")
            await connection.execute("PRAGMA busy_timeout=5000")
            async with connection.execute("PRAGMA user_version") as cursor:
                version = (await cursor.fetchone())[0]
            if version not in (0, 1):
                raise AppError("unsupported_database_version", 503)
            if version == 0:
                await connection.executescript(Path(__file__).with_name("schema.sql").read_text())
            return cls(connection, handle)
        except BaseException:
            if connection:
                await connection.close()
            handle.close()
            raise

    async def close(self):
        if not self._closed:
            self._closed = True
            try:
                await self.db.close()
            finally:
                self._file.close()

    @asynccontextmanager
    async def transaction(self):
        async with self._gate:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
            except BaseException:
                await self.db.rollback()
                raise
            else:
                await self.db.commit()

    async def _one(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def _all(self, sql, args=()):
        async with self.db.execute(sql, args) as cursor:
            return [dict(row) for row in await cursor.fetchall()]

    async def _audit(self, scope, action, now, **details):
        await self.db.execute(
            "INSERT INTO audit_log(scope,action,details,created_at) VALUES(?,?,?,?)",
            (scope, action, encode(details), now),
        )

    async def _open(self, key):
        controls = await self._all(
            "SELECT mode FROM controls WHERE scope IN (?,?)", ("*", account_scope(key))
        )
        return all(item["mode"] == "auto" for item in controls)

    async def _unknown(self, scope):
        return (
            await self._one(
                "SELECT send_id FROM outbox WHERE scope=? AND status='unknown' LIMIT 1", (scope,)
            )
            is not None
        )

    async def _cancel(self, scope, status="cancelled"):
        await self.db.execute(
            "UPDATE outbox SET status=? WHERE scope=? AND status='pending'", (status, scope)
        )
        if status == "cancelled":
            await self.db.execute(
                "UPDATE jobs SET status='cancelled' WHERE scope=? AND status='pending'", (scope,)
            )
            await self.db.execute(
                "UPDATE events SET status='ignored_control' WHERE scope=? AND status='queued'",
                (scope,),
            )

    async def _enqueue(self, kind, scope, now, payload=None, debounce=0, max_wait=0, capacity=1000):
        current = await self._one(
            "SELECT * FROM jobs WHERE kind=? AND scope=? AND status='pending'", (kind, scope)
        )
        if current:
            due = min(now + debounce, current["first_at"] + max_wait)
            if kind == "memory" and json.loads(current["payload"]).get("source_version", 0) > (
                payload or {}
            ).get("source_version", 0):
                return
            await self.db.execute(
                "UPDATE jobs SET due_at=?,payload=? WHERE job_id=?",
                (due, encode(payload or {}), current["job_id"]),
            )
        else:
            count = await self._one(
                "SELECT COUNT(*) AS n FROM jobs WHERE status IN ('pending','running')"
            )
            if count["n"] >= capacity:
                raise AppError("queue_full", 503)
            await self.db.execute(
                "INSERT INTO jobs(kind,scope,status,due_at,first_at,payload) VALUES(?,?,'pending',?,?,?)",
                (kind, scope, now + debounce, now, encode(payload or {})),
            )

    async def ingest(self, event, *, initial_mode, authorized, now, debounce, max_wait, capacity):
        key, scope = event.key, event.key.scope
        async with self.transaction():
            existing = await self._one(
                "SELECT record_id,payload_hash,status FROM events WHERE scope=? AND event_id=?",
                (scope, event.event_id),
            )
            if existing:
                if existing["payload_hash"] != event.payload_hash:
                    raise AppError("event_id_conflict")
                return Acceptance(
                    record_id=existing["record_id"], status=existing["status"], duplicate=True
                )
            await self.db.execute(
                "INSERT OR IGNORE INTO conversations(scope,channel,account_id,conversation_id,mode) VALUES(?,?,?,?,?)",
                (scope, key.channel, key.account_id, key.conversation_id, initial_mode),
            )
            conversation = await self._one("SELECT * FROM conversations WHERE scope=?", (scope,))
            echo = None
            if event.origin == "bot":
                echo = await self._one(
                    "SELECT send_id FROM outbox WHERE send_id=? AND scope=? AND text=? AND status IN ('sending','sent','unknown')",
                    (event.client_send_id, scope, event.text),
                )
            unsupported = event.message_type != "text" or event.conversation_type != "direct"
            if echo:
                status = "echo"
            elif event.direction == "outbound" or unsupported:
                status = "handoff" if not unsupported else "unsupported"
                await self.db.execute(
                    "UPDATE conversations SET mode='handoff',revision=revision+1 WHERE scope=?",
                    (scope,),
                )
                await self._cancel(scope)
            else:
                eligible = (
                    authorized
                    and conversation["mode"] == "auto"
                    and await self._open(key)
                    and not await self._unknown(scope)
                )
                status = "queued" if eligible else "ignored_mode"
                await self.db.execute(
                    "UPDATE conversations SET revision=revision+1 WHERE scope=?", (scope,)
                )
                await self._cancel(scope, "superseded")
            cursor = await self.db.execute(
                "INSERT INTO events(scope,event_id,payload_hash,payload,direction,text,status,received_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    scope,
                    event.event_id,
                    event.payload_hash,
                    event.model_dump_json(),
                    event.direction,
                    event.text,
                    status,
                    now,
                ),
            )
            record_id = cursor.lastrowid
            await cursor.close()
            if status == "queued":
                await self._enqueue(
                    "process", scope, now, debounce=debounce, max_wait=max_wait, capacity=capacity
                )
            return Acceptance(record_id=record_id, status=status)

    async def conversation(self, key):
        async with self._gate:
            result = await self._one("SELECT * FROM conversations WHERE scope=?", (key.scope,))
            if not result:
                raise AppError("conversation_not_found", 404)
            result["in_flight"] = (
                await self._one(
                    "SELECT COUNT(*) AS n FROM outbox WHERE scope=? AND status='sending'",
                    (key.scope,),
                )
            )["n"]
            result["unknown_deliveries"] = await self._unknown(key.scope)
            return result

    async def event(self, record_id):
        async with self._gate:
            result = await self._one("SELECT * FROM events WHERE record_id=?", (record_id,))
            if not result:
                raise AppError("event_not_found", 404)
            result["payload"] = json.loads(result["payload"])
            return result

    async def messages(self, key, limit=50, before=None):
        async with self._gate:
            rows = await self._all(
                "SELECT * FROM events WHERE scope=? AND record_id<? ORDER BY record_id DESC LIMIT ?",
                (key.scope, before or 9223372036854775807, limit),
            )
            return [
                {
                    **json.loads(row["payload"]),
                    "record_id": row["record_id"],
                    "status": row["status"],
                }
                for row in reversed(rows)
            ]

    async def outbox(self, key, limit=50, before=None):
        async with self._gate:
            rows = await self._all(
                "SELECT rowid AS cursor,* FROM outbox WHERE scope=? AND rowid<? ORDER BY rowid DESC LIMIT ?",
                (key.scope, before or 9223372036854775807, limit),
            )
            return list(reversed(rows))

    async def delivery(self, send_id):
        async with self._gate:
            result = await self._one("SELECT * FROM outbox WHERE send_id=?", (send_id,))
            if not result:
                raise AppError("delivery_not_found", 404)
            return result

    async def latest_run(self, key):
        async with self._gate:
            row = await self._one(
                "SELECT * FROM runs WHERE scope=? ORDER BY rowid DESC LIMIT 1", (key.scope,)
            )
            if row:
                row["result"] = json.loads(row["result"]) if row["result"] else None
            return row

    async def change_mode(self, key, mode, expected_revision, now):
        async with self.transaction():
            row = await self._one("SELECT * FROM conversations WHERE scope=?", (key.scope,))
            if not row:
                raise AppError("conversation_not_found", 404)
            if row["revision"] != expected_revision:
                raise AppError("revision_conflict")
            await self.db.execute(
                "UPDATE conversations SET mode=?,revision=revision+1 WHERE scope=?",
                (mode, key.scope),
            )
            await self._cancel(key.scope)
            await self._audit(key.scope, "mode_changed", now, mode=mode)
        return await self.conversation(key)

    async def change_control(self, mode, expected_revision, now, key=None):
        scope = account_scope(key) if key else "*"
        async with self.transaction():
            await self.db.execute("INSERT OR IGNORE INTO controls VALUES(?,'auto',0)", (scope,))
            row = await self._one("SELECT * FROM controls WHERE scope=?", (scope,))
            if row["revision"] != expected_revision:
                raise AppError("revision_conflict")
            await self.db.execute(
                "UPDATE controls SET mode=?,revision=revision+1 WHERE scope=?", (mode, scope)
            )
            conversations = await self._all(
                "SELECT scope FROM conversations"
                if key is None
                else "SELECT scope FROM conversations WHERE channel=? AND account_id=?",
                () if key is None else (key.channel, key.account_id),
            )
            for item in conversations:
                await self.db.execute(
                    "UPDATE conversations SET revision=revision+1 WHERE scope=?", (item["scope"],)
                )
                await self._cancel(item["scope"])
            await self._audit(scope, "control_changed", now, mode=mode)
            result = await self._one("SELECT * FROM controls WHERE scope=?", (scope,))
            result["in_flight"] = (
                await self._one(
                    "SELECT COUNT(*) AS n FROM outbox WHERE status='sending'"
                    if key is None
                    else "SELECT COUNT(*) AS n FROM outbox o JOIN conversations c ON c.scope=o.scope WHERE o.status='sending' AND c.channel=? AND c.account_id=?",
                    () if key is None else (key.channel, key.account_id),
                )
            )["n"]
            return result

    async def recover(self, now):
        async with self.transaction():
            await self.db.execute(
                "UPDATE outbox SET status='unknown',error='interrupted_send' WHERE status='sending'"
            )
            await self.db.execute(
                "UPDATE runs SET status='interrupted',error='interrupted_generation' WHERE status='running'"
            )
            running = await self._all("SELECT * FROM jobs WHERE status='running'")
            for job in running:
                pending = await self._one(
                    "SELECT job_id FROM jobs WHERE kind=? AND scope=? AND status='pending'",
                    (job["kind"], job["scope"]),
                )
                await self.db.execute(
                    "UPDATE jobs SET status=?,due_at=? WHERE job_id=?",
                    ("cancelled" if pending else "pending", now, job["job_id"]),
                )

    async def due_jobs(self, now):
        async with self._gate:
            return await self._all(
                "SELECT * FROM jobs WHERE status='pending' AND due_at<=? ORDER BY (kind='process') DESC,due_at,job_id LIMIT 100",
                (now,),
            )

    async def claim_job(self, job_id):
        async with self.transaction():
            cursor = await self.db.execute(
                "UPDATE jobs SET status='running',attempts=attempts+1 WHERE job_id=? AND status='pending'",
                (job_id,),
            )
            claimed = cursor.rowcount == 1
            await cursor.close()
            return claimed

    async def finish_job(self, job_id, *, error=None, retry_at=None):
        async with self.transaction():
            job = await self._one("SELECT * FROM jobs WHERE job_id=?", (job_id,))
            pending = await self._one(
                "SELECT job_id FROM jobs WHERE kind=? AND scope=? AND status='pending'",
                (job["kind"], job["scope"]),
            )
            status = "done" if error is None else "failed"
            if error and retry_at is not None and not pending:
                status = "pending"
            await self.db.execute(
                "UPDATE jobs SET status=?,error=?,due_at=? WHERE job_id=?",
                (status, error, retry_at or job["due_at"], job_id),
            )

    async def snapshot(self, key, now, limit):
        async with self.transaction():
            row = await self._one("SELECT * FROM conversations WHERE scope=?", (key.scope,))
            pending = await self._one(
                "SELECT MAX(record_id) AS last FROM events WHERE scope=? AND status='queued'",
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
                "SELECT * FROM events WHERE scope=? AND status!='echo' ORDER BY record_id DESC LIMIT ?",
                (key.scope, limit),
            )
            run_id = uuid.uuid4().hex
            await self.db.execute(
                "INSERT INTO runs(run_id,scope,revision,last_event_id,status,created_at) VALUES(?,?,?,?,'running',?)",
                (run_id, key.scope, row["revision"], pending["last"], now),
            )
            return {
                "run_id": run_id,
                "revision": row["revision"],
                "last_event_id": pending["last"],
                "messages": [
                    {**json.loads(item["payload"]), "record_id": item["record_id"]}
                    for item in reversed(rows)
                ],
                "memories": await self._memories(key.scope),
                "memory_version": row["memory_version"],
                "history_window": limit,
            }

    async def finish_run(self, key, snapshot, result, now, ttl, min_interval):
        async with self.transaction():
            row = await self._one("SELECT * FROM conversations WHERE scope=?", (key.scope,))
            current = (
                row["revision"] == snapshot["revision"]
                and row["mode"] == "auto"
                and await self._open(key)
                and not await self._unknown(key.scope)
            )
            await self.db.execute(
                "UPDATE runs SET status=?,result=? WHERE run_id=?",
                ("completed" if current else "superseded", encode(result), snapshot["run_id"]),
            )
            if not current:
                return None
            await self.db.execute(
                "UPDATE events SET status='handled' WHERE scope=? AND record_id<=? AND status='queued'",
                (key.scope, snapshot["last_event_id"]),
            )
            await self.db.execute(
                "UPDATE conversations SET last_processed=? WHERE scope=?",
                (snapshot["last_event_id"], key.scope),
            )
            if result["status"] == "handoff":
                await self.db.execute(
                    "UPDATE conversations SET mode='handoff',revision=revision+1 WHERE scope=?",
                    (key.scope,),
                )
                await self._cancel(key.scope)
            if result["status"] != "reply":
                return None
            send_id = uuid.uuid4().hex
            previous = await self._one(
                "SELECT MAX(sent_at) AS last FROM outbox WHERE scope=? AND status='sent'",
                (key.scope,),
            )
            not_before = max(now, (previous["last"] or 0) + min_interval)
            await self.db.execute(
                "INSERT INTO outbox(send_id,scope,run_id,source_revision,text,status,created_at,expires_at,not_before) VALUES(?,?,?,?,?,'pending',?,?,?)",
                (
                    send_id,
                    key.scope,
                    snapshot["run_id"],
                    snapshot["revision"],
                    result["reply"],
                    now,
                    now + ttl,
                    not_before,
                ),
            )
            return send_id

    async def fail_run(self, run_id, error):
        async with self.transaction():
            await self.db.execute(
                "UPDATE runs SET status='failed',error=? WHERE run_id=?", (error, run_id)
            )

    async def pending_deliveries(self, now):
        async with self._gate:
            return await self._all(
                "SELECT * FROM outbox WHERE status='pending' AND not_before<=? ORDER BY created_at LIMIT 100",
                (now,),
            )

    async def claim_delivery(self, send_id, authorized, now):
        async with self.transaction():
            row = await self._one("SELECT * FROM outbox WHERE send_id=?", (send_id,))
            if not row or row["status"] != "pending" or row["not_before"] > now:
                return None
            key = ConversationKey.from_scope(row["scope"])
            conversation = await self._one(
                "SELECT * FROM conversations WHERE scope=?", (key.scope,)
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
                await self.db.execute(
                    "UPDATE outbox SET status='cancelled',error=? WHERE send_id=?", (error, send_id)
                )
                return None
            await self.db.execute("UPDATE outbox SET status='sending' WHERE send_id=?", (send_id,))
            return row

    async def record_delivery(
        self, send_id, result, self_id, now, capacity=1000, reconciliation=False
    ):
        async with self.transaction():
            row = await self._one("SELECT * FROM outbox WHERE send_id=?", (send_id,))
            if not row:
                raise AppError("delivery_not_found", 404)
            if row["status"] not in ("sending", "unknown"):
                return row
            await self.db.execute(
                "UPDATE outbox SET status=?,external_message_id=?,error=?,sent_at=CASE WHEN ?='sent' THEN ? ELSE sent_at END WHERE send_id=?",
                (
                    result.status,
                    result.external_message_id,
                    result.reason,
                    result.status,
                    now,
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
                cursor = await self.db.execute(
                    "INSERT OR IGNORE INTO events(scope,event_id,payload_hash,payload,direction,text,status,received_at) VALUES(?,?,?,?,?,?,'sent',?)",
                    (
                        key.scope,
                        event.event_id,
                        event.payload_hash,
                        event.model_dump_json(),
                        "outbound",
                        event.text,
                        now,
                    ),
                )
                version = cursor.lastrowid
                await cursor.close()
                await self.db.execute(
                    "UPDATE conversations SET revision=revision+1 WHERE scope=?", (key.scope,)
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
            row = await self._one("SELECT * FROM mock_receipts WHERE send_id=?", (intent.send_id,))
            if row and (row["scope"] != intent.key.scope or row["text"] != intent.text):
                raise AppError("send_id_conflict")
            if not row:
                await self.db.execute(
                    "INSERT INTO mock_receipts VALUES(?,?,?,?,?)",
                    (intent.send_id, intent.key.scope, intent.text, "mock-" + intent.send_id, now),
                )
            return "mock-" + intent.send_id

    async def mock_receipt(self, send_id):
        async with self._gate:
            return await self._one("SELECT * FROM mock_receipts WHERE send_id=?", (send_id,))

    async def _memories(self, scope):
        rows = await self._all("SELECT * FROM memories WHERE scope=? ORDER BY memory_id", (scope,))
        return [{**row, "source_event_ids": json.loads(row["source_event_ids"])} for row in rows]

    async def memories(self, key):
        async with self._gate:
            return await self._memories(key.scope)

    async def memory_context(self, key, version, limit):
        async with self._gate:
            rows = await self._all(
                "SELECT * FROM events WHERE scope=? AND record_id<=? AND status!='echo' ORDER BY record_id DESC LIMIT ?",
                (key.scope, version, limit),
            )
            return [
                {**json.loads(row["payload"]), "record_id": row["record_id"]}
                for row in reversed(rows)
            ]

    async def save_memories(self, key, version, items, now):
        async with self.transaction():
            row = await self._one(
                "SELECT memory_version FROM conversations WHERE scope=?", (key.scope,)
            )
            if row["memory_version"] >= version:
                return
            if any(not item.source_event_ids for item in items):
                raise AppError("invalid_memory_evidence", 502)
            cited = sorted({source for item in items for source in item.source_event_ids})
            allowed = (
                await self._all(
                    "SELECT record_id FROM events WHERE scope=? AND record_id<=? AND record_id IN ("
                    + ",".join("?" for _ in cited)
                    + ")",
                    (key.scope, version, *cited),
                )
                if cited
                else []
            )
            if {row["record_id"] for row in allowed} != set(cited):
                raise AppError("invalid_memory_evidence", 502)
            await self.db.execute("DELETE FROM memories WHERE scope=?", (key.scope,))
            for item in items:
                await self.db.execute(
                    "INSERT INTO memories(scope,kind,content,source_event_ids,source_version,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        key.scope,
                        item.kind,
                        item.content,
                        encode(item.source_event_ids),
                        version,
                        now,
                    ),
                )
            await self.db.execute(
                "UPDATE conversations SET memory_version=? WHERE scope=?", (version, key.scope)
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
