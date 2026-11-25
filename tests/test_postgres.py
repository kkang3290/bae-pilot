import asyncio

import psycopg
import pytest
from psycopg import sql

from bae_pilot.config import Settings
from bae_pilot.domain import AppError
from bae_pilot.services.runtime import Runtime
from bae_pilot.storage.repositories import Store
from tests.helpers import message


async def test_postgres_persists_native_json_evidence_and_timestamps(runtime, db_config):
    accepted = await runtime.ingest(message("对方的合成陈述"))
    await runtime.drain()
    with psycopg.connect(db_config["database_url"]) as connection:
        event = connection.execute(
            sql.SQL(
                "SELECT payload->>'text',pg_typeof(payload)::text,pg_typeof(received_at)::text "
                "FROM {}.events WHERE record_id=%s"
            ).format(sql.Identifier(db_config["database_schema"])),
            (accepted.record_id,),
        ).fetchone()
        memory = connection.execute(
            sql.SQL(
                "SELECT source_event_ids,pg_typeof(source_event_ids)::text FROM {}.memories"
            ).format(sql.Identifier(db_config["database_schema"]))
        ).fetchone()
    assert event == ("对方的合成陈述", "jsonb", "timestamp with time zone")
    assert memory == ([accepted.record_id], "bigint[]")
    assert isinstance((await runtime.store.event(accepted.record_id))["received_at"], float)
    assert (await runtime.store.latest_run(message().key))["result"]["status"] == "reply"


async def test_queue_failure_rolls_back_event_revision_and_pending_reply(runtime):
    await runtime.ingest(message())
    before = await runtime.store.conversation(message().key)
    with pytest.raises(AppError, match="queue_full"):
        await runtime.store.ingest(
            message(conversation_id="other-chat"),
            initial_mode="auto",
            authorized=True,
            now=1000,
            debounce=0,
            max_wait=0,
            capacity=1,
        )
    assert await runtime.store.conversation(message().key) == before
    assert [m["event_id"] for m in await runtime.store.messages(message().key)] == ["one"]
    with pytest.raises(AppError, match="conversation_not_found"):
        await runtime.store.conversation(message(conversation_id="other-chat").key)
    await runtime.drain()
    assert (await runtime.store.outbox(message().key))[0]["status"] == "sent"


async def test_session_lock_rejects_second_instance_and_is_released_on_close(db_config):
    settings = Settings(**db_config, debounce_s=0, min_send_interval_s=0)
    first, second = Runtime(settings), Runtime(settings)
    await first.start(worker=False)
    try:
        with pytest.raises(AppError, match="database_in_use"):
            await second.start(worker=False)
        await first.ingest(message())
    finally:
        await first.close()
        await second.close()
    restarted = Runtime(settings)
    await restarted.start(worker=False)
    try:
        await restarted.drain()
        assert (await restarted.store.outbox(message().key))[0]["status"] == "sent"
    finally:
        await restarted.close()


async def test_job_claim_is_atomic_and_outbox_cursor_is_stable(runtime):
    await runtime.ingest(message())
    job = (await runtime.store.due_jobs(runtime.clock()))[0]
    claims = await asyncio.gather(*(runtime.store.claim_job(job["job_id"]) for _ in range(5)))
    assert claims.count(True) == 1
    await runtime.store.recover(runtime.clock())
    for index in range(3):
        if index:
            await runtime.ingest(message(event_id=f"next-{index}"))
        await runtime.drain()
    newest = await runtime.store.outbox(message().key, limit=2)
    oldest = await runtime.store.outbox(message().key, limit=2, before=newest[0]["cursor"])
    assert len(newest) == 2 and len(oldest) == 1
    assert len({row["send_id"] for row in [*oldest, *newest]}) == 3
    assert oldest[0]["cursor"] < newest[0]["cursor"] < newest[1]["cursor"]


async def test_unsupported_schema_version_fails_without_changing_data(db_config):
    schema = sql.Identifier(db_config["database_schema"])
    with psycopg.connect(db_config["database_url"]) as connection:
        connection.execute(
            sql.SQL("CREATE TABLE {}.schema_version(version integer NOT NULL)").format(schema)
        )
        connection.execute(sql.SQL("INSERT INTO {}.schema_version VALUES (999)").format(schema))
    with pytest.raises(AppError, match="unsupported_database_version"):
        await Store.open(Settings(**db_config))
    with psycopg.connect(db_config["database_url"]) as connection:
        assert connection.execute(
            sql.SQL("SELECT version FROM {}.schema_version").format(schema)
        ).fetchone() == (999,)


async def test_nonempty_unversioned_schema_is_not_adopted_or_overwritten(db_config):
    schema = sql.Identifier(db_config["database_schema"])
    with psycopg.connect(db_config["database_url"]) as connection:
        connection.execute(sql.SQL("CREATE TABLE {}.valuable_data(content text)").format(schema))
        connection.execute(
            sql.SQL("INSERT INTO {}.valuable_data VALUES ('keep-me')").format(schema)
        )
    with pytest.raises(AppError, match="unsupported_database_version"):
        await Store.open(Settings(**db_config))
    with psycopg.connect(db_config["database_url"]) as connection:
        assert connection.execute(
            sql.SQL("SELECT content FROM {}.valuable_data").format(schema)
        ).fetchall() == [("keep-me",)]
        assert connection.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname=%s", (db_config["database_schema"],)
        ).fetchall() == [("valuable_data",)]


async def test_lost_database_session_cannot_accept_or_send_new_work(runtime, db_config):
    await runtime.ingest(message())
    await runtime.store.db.close()
    with pytest.raises(AppError, match="database_error"):
        await runtime.ingest(message(event_id="after-disconnect"))
    with psycopg.connect(db_config["database_url"]) as connection:
        schema = sql.Identifier(db_config["database_schema"])
        assert connection.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.events").format(schema)
        ).fetchone() == (1,)
        assert connection.execute(
            sql.SQL("SELECT COUNT(*) FROM {}.outbox").format(schema)
        ).fetchone() == (0,)


@pytest.mark.parametrize("account_only", [False, True])
async def test_control_change_cancels_all_selected_conversations_only(db_config, account_only):
    from bae_pilot.config import Account, ConversationRule
    from tests.helpers import running

    async with running(
        db_config,
        accounts=[Account(), Account(account_id="other-account")],
        conversations=[
            ConversationRule(),
            ConversationRule(conversation_id="queued-chat"),
            ConversationRule(account_id="other-account"),
        ],
    ) as runtime:
        events = [
            message(),
            message(conversation_id="queued-chat"),
            message(account_id="other-account"),
        ]
        accepted = [await runtime.ingest(event) for event in events]
        for event in (events[0], events[2]):
            snapshot = await runtime.store.snapshot(
                event.key, runtime.clock(), 40, runtime.settings.max_context_chars
            )
            result = await runtime.graph.run(snapshot)
            await runtime.store.finish_run(
                event.key, snapshot, result.model_dump(), runtime.clock(), 60, 0
            )
        before = [await runtime.store.conversation(event.key) for event in events]
        kwargs = {"channel": "mock", "account_id": "demo-account"} if account_only else {}
        outcome = await runtime.change_global("paused", 0, **kwargs)
        assert outcome["mode"] == "paused" and outcome["revision"] == 1
        after = [await runtime.store.conversation(event.key) for event in events]
        assert [
            row["revision"] - previous["revision"]
            for row, previous in zip(after, before, strict=True)
        ] == ([1, 1, 0] if account_only else [1, 1, 1])
        assert (await runtime.store.outbox(events[0].key))[0]["status"] == "cancelled"
        assert (await runtime.store.outbox(events[2].key))[0]["status"] == (
            "pending" if account_only else "cancelled"
        )
        assert (await runtime.store.event(accepted[1].record_id))["status"] == "ignored_control"
        assert (await runtime.store.event(accepted[0].record_id))["status"] == "handled"
        assert [job["scope"] for job in await runtime.store.due_jobs(runtime.clock())] == (
            [events[2].key.scope] if account_only else []
        )


async def test_memory_snapshot_saves_all_items_and_can_be_cleared(runtime):
    from bae_pilot.agents.schemas import MemoryItem

    first = await runtime.ingest(message())
    items = [
        MemoryItem(kind="summary", content=f"memory-{index}", source_event_ids=[first.record_id])
        for index in range(20)
    ]
    await runtime.store.save_memories(message().key, first.record_id, items, 1000.25)
    saved = await runtime.store.memories(message().key)
    assert len(saved) == 20
    assert saved[0]["content"] == "memory-0"
    assert saved[-1]["content"] == "memory-19"
    assert all(item["source_event_ids"] == [first.record_id] for item in saved)
    assert all(
        item["source_version"] == first.record_id and item["created_at"] == 1000.25
        for item in saved
    )
    second = await runtime.ingest(message(event_id="second"))
    await runtime.store.save_memories(message().key, second.record_id, [], 1001)
    assert await runtime.store.memories(message().key) == []
    assert (await runtime.store.conversation(message().key))["memory_version"] == second.record_id
    await runtime.store.save_memories(message().key, first.record_id, items, 1002)
    assert await runtime.store.memories(message().key) == []


@pytest.mark.parametrize("source_id", [-1, 2**80])
async def test_memory_rejects_invalid_id_ranges_without_database_cast_errors(runtime, source_id):
    from bae_pilot.agents.schemas import MemoryItem

    accepted = await runtime.ingest(message())
    with pytest.raises(AppError, match="invalid_memory_evidence") as error:
        await runtime.store.save_memories(
            message().key,
            accepted.record_id,
            [MemoryItem(kind="statement", content="unsupported", source_event_ids=[source_id])],
            runtime.clock(),
        )
    assert error.value.status == 502
    assert await runtime.store.memories(message().key) == []


async def test_snapshot_includes_every_queued_message_beyond_recent_window(db_config):
    from tests.helpers import RecordingProvider, running

    provider = RecordingProvider()
    async with running(db_config, provider=provider) as runtime:
        accepted = [await runtime.ingest(message(event_id=f"queued-{i}")) for i in range(201)]
        await runtime.drain()
        assert [m["record_id"] for m in provider.calls[0][1]["messages"]] == [
            a.record_id for a in accepted
        ]
        assert [row["status"] for row in await runtime.store.outbox(message().key)] == ["sent"]
        state = await runtime.store.conversation(message().key)
        assert state["last_processed"] == accepted[-1].record_id
        assert await runtime.store._all("SELECT record_id FROM events WHERE status='queued'") == []
        assert await runtime.store._all(
            "SELECT COUNT(*) AS count FROM events WHERE status='handled'"
        ) == [{"count": 201}]


@pytest.mark.parametrize("max_chars", [1000, 32000])
async def test_snapshot_decodes_queued_payloads_once_or_not_at_all_on_overflow(runtime, max_chars):
    first = await runtime.ingest(message("旧" * 4000))
    second = await runtime.ingest(message("新" * 4000, event_id="second"))
    original_factory = runtime.store.db.row_factory
    decoded_messages = []

    def observe_rows(cursor):
        build = original_factory(cursor)

        def observe(values):
            row = build(values)
            if isinstance(row.get("payload"), dict):
                decoded_messages.append(row)
            return row

        return observe

    runtime.store.db.row_factory = observe_rows
    try:
        snapshot = await runtime.store.snapshot(message().key, runtime.clock(), 40, max_chars)
    finally:
        runtime.store.db.row_factory = original_factory
    assert snapshot["queued_count"] == 2
    if max_chars == 1000:
        assert snapshot["context_overflow"] is True
        assert "messages" not in snapshot and "queued_event_ids" not in snapshot
        assert decoded_messages == []
    else:
        assert [row["record_id"] for row in decoded_messages] == [
            first.record_id,
            second.record_id,
        ]
        assert snapshot["queued_event_ids"] == [first.record_id, second.record_id]
        assert [row["record_id"] for row in snapshot["messages"]] == [
            first.record_id,
            second.record_id,
        ]
    assert (await runtime.store.latest_run(message().key))["status"] == "running"
    assert (await runtime.store.conversation(message().key))["last_processed"] == 0


@pytest.mark.parametrize("damage", ["missing", "duplicate", "foreign", "overflow"])
async def test_incomplete_snapshot_cannot_confirm_messages_or_create_outbox(runtime, damage):
    from bae_pilot.agents.schemas import GraphResult

    foreign = await runtime.ingest(message(conversation_id="not-authorized"))
    first = await runtime.ingest(message())
    second = await runtime.ingest(message(event_id="second"))
    snapshot = await runtime.store.snapshot(
        message().key, runtime.clock(), 40, runtime.settings.max_context_chars
    )
    snapshot["queued_event_ids"] = [first.record_id, second.record_id]
    if damage == "missing":
        snapshot["queued_event_ids"] = [second.record_id]
        snapshot["messages"] = [
            m for m in snapshot["messages"] if m["record_id"] == second.record_id
        ]
    elif damage == "duplicate":
        snapshot["queued_event_ids"].append(second.record_id)
    elif damage == "foreign":
        snapshot["queued_event_ids"] = [foreign.record_id, second.record_id]
        snapshot["messages"].append({"record_id": foreign.record_id})
    else:
        snapshot["context_overflow"] = True
    with pytest.raises(AppError, match="incomplete_snapshot"):
        await runtime.store.finish_run(
            message().key,
            snapshot,
            GraphResult(
                status="reply", reply="不应发送", trace=["talk", "validate"], model_calls=1
            ).model_dump(),
            runtime.clock(),
            60,
            0,
        )
    assert (await runtime.store.event(first.record_id))["status"] == "queued"
    assert (await runtime.store.event(second.record_id))["status"] == "queued"
    assert (await runtime.store.conversation(message().key))["last_processed"] == 0
    assert (await runtime.store.latest_run(message().key))["status"] == "running"
    assert await runtime.store.outbox(message().key) == []


async def test_unreachable_postgres_fails_without_falling_back_or_leaking_password():
    settings = Settings(
        database_url="postgresql://test:private-db-password@127.0.0.1:1/baepilot?connect_timeout=1"
    )
    runtime = Runtime(settings)
    try:
        with pytest.raises(AppError, match="database_unavailable") as error:
            await runtime.start(worker=False)
        assert "private-db-password" not in str(error.value)
    finally:
        await runtime.close()
