import asyncio
from functools import partial

import pytest

from tests.helpers import message

event = partial(message, event_id="e1")


class Clock:
    def __init__(self):
        self.value = 1000.0

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += seconds


async def test_authorized_message_is_automatically_delivered_and_remembered(runtime):
    incoming = event()
    await runtime.ingest(incoming)
    await runtime.drain()
    deliveries = await runtime.store.outbox(incoming.key)
    assert [d["status"] for d in deliveries] == ["sent"]
    assert deliveries[0]["text"]
    receipt = await runtime.connectors["mock"].query(deliveries[0]["send_id"])
    assert receipt.status == "sent"
    run = await runtime.store.latest_run(incoming.key)
    assert run["result"]["trace"] == ["talk", "validate"]
    assert run["result"]["model_calls"] == 1
    memories = await runtime.store.memories(incoming.key)
    assert memories
    assert all(m["source_event_ids"] for m in memories)


async def test_duplicate_event_is_idempotent_and_conflicting_payload_is_rejected(runtime):
    from bae_pilot.domain import AppError

    incoming = event()
    first = await runtime.ingest(incoming)
    again = await runtime.ingest(incoming)
    assert again.duplicate and first.record_id == again.record_id
    with pytest.raises(AppError, match="event_id_conflict"):
        await runtime.ingest(event("不同内容"))
    await runtime.drain()
    assert len(await runtime.store.outbox(incoming.key)) == 1


async def test_unlisted_conversation_cannot_send(runtime):
    incoming = event(conversation_id="not-authorized")
    await runtime.ingest(incoming)
    await runtime.drain()
    assert await runtime.store.outbox(incoming.key) == []


async def test_complex_conversation_uses_specialists(runtime):
    incoming = event("你每次都说下次注意，我很失望")
    await runtime.ingest(incoming)
    await runtime.drain()
    run = await runtime.store.latest_run(incoming.key)
    assert run["result"]["trace"] == ["understanding", "strategy", "talk", "review", "validate"]
    assert run["result"]["model_calls"] == 4
    assert (await runtime.store.outbox(incoming.key))[0]["status"] == "sent"


async def test_review_rewrite_is_bounded_and_never_sends_failed_review(db_config):
    from bae_pilot.agents.schemas import ReviewResult
    from bae_pilot.config import Settings
    from bae_pilot.models.fake import FakeProvider
    from bae_pilot.services.runtime import Runtime

    class NeverPasses(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "review":
                return ReviewResult(verdict="revise", reason="不符合策略")
            return await super().complete(role, context, schema)

    r = Runtime(Settings(**db_config, debounce_s=0), provider=NeverPasses())
    await r.start(worker=False)
    try:
        incoming = event("我很失望")
        await r.ingest(incoming)
        await r.drain()
        result = (await r.store.latest_run(incoming.key))["result"]
        assert result["trace"].count("review") == 2
        assert result["model_calls"] == 6
        assert result["status"] == "handoff"
        assert await r.store.outbox(incoming.key) == []
    finally:
        await r.close()


async def test_pause_and_human_handoff_prevent_pending_replies(runtime):
    incoming = event()
    await runtime.ingest(incoming)
    state = await runtime.store.conversation(incoming.key)
    await runtime.change_mode(incoming.key, "paused", state["revision"])
    await runtime.drain()
    assert await runtime.store.outbox(incoming.key) == []
    state = await runtime.store.conversation(incoming.key)
    await runtime.change_mode(incoming.key, "auto", state["revision"])
    await runtime.ingest(event(event_id="e2"))
    await runtime.ingest(
        event("我来回复", event_id="human", direction="outbound", origin="human", sender_id="self")
    )
    await runtime.drain()
    assert (await runtime.store.conversation(incoming.key))["mode"] == "handoff"
    assert await runtime.store.outbox(incoming.key) == []


async def test_known_bot_echo_does_not_trigger_reply_or_handoff(runtime):
    incoming = event()
    await runtime.ingest(incoming)
    await runtime.drain()
    sent = (await runtime.store.outbox(incoming.key))[0]
    await runtime.ingest(
        event(
            sent["text"],
            event_id="echo",
            direction="outbound",
            origin="bot",
            sender_id="self",
            client_send_id=sent["send_id"],
        )
    )
    await runtime.drain()
    assert len(await runtime.store.outbox(incoming.key)) == 1
    assert (await runtime.store.conversation(incoming.key))["mode"] == "auto"


async def test_new_message_during_generation_supersedes_old_reply(db_config):
    from bae_pilot.config import Settings
    from bae_pilot.models.fake import FakeProvider
    from bae_pilot.services.runtime import Runtime

    entered, release = asyncio.Event(), asyncio.Event()

    class Blocking(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "talk" and not entered.is_set():
                entered.set()
                await release.wait()
            return await super().complete(role, context, schema)

    r = Runtime(
        Settings(**db_config, debounce_s=0, min_send_interval_s=0),
        provider=Blocking(),
    )
    await r.start(worker=False)
    try:
        await r.ingest(event())
        await r.tick()
        await asyncio.wait_for(entered.wait(), 2)
        await r.ingest(event("还有一件事", event_id="e2"))
        release.set()
        await r.drain()
        rows = await r.store.outbox(event().key)
        assert len(rows) == 1 and rows[0]["status"] == "sent"
        async with r.store.db.cursor() as cursor:
            await cursor.execute(
                "SELECT status FROM runs WHERE scope=%s ORDER BY run_seq", (event().key.scope,)
            )
            assert [row["status"] for row in await cursor.fetchall()] == ["superseded", "completed"]
    finally:
        release.set()
        await r.close()


async def test_delivery_timeout_is_unknown_not_blindly_retried(runtime):
    from bae_pilot.connectors.mock import MockConnector

    class AcceptedThenTimeout(MockConnector):
        async def send(self, intent):
            await super().send(intent)
            raise TimeoutError("lost acknowledgement")

    runtime.connectors["mock"] = AcceptedThenTimeout(runtime.store)
    incoming = event()
    await runtime.ingest(incoming)
    await runtime.drain()
    sent = (await runtime.store.outbox(incoming.key))[0]
    assert sent["status"] == "unknown"
    await runtime.ingest(event("继续", event_id="e2"))
    await runtime.drain()
    assert len(await runtime.store.outbox(incoming.key)) == 1
    resolved = await runtime.reconcile(sent["send_id"])
    assert resolved["status"] == "sent"


async def test_debounce_coalesces_messages_without_real_sleep(db_config):
    from bae_pilot.config import Settings
    from bae_pilot.services.runtime import Runtime

    clock = Clock()
    r = Runtime(
        Settings(**db_config, debounce_s=0.5, max_wait_s=2, min_send_interval_s=0),
        clock=clock,
    )
    await r.start(worker=False)
    try:
        await r.ingest(event())
        assert await r.tick() == 0
        clock.advance(0.4)
        await r.ingest(event("第二句", event_id="e2"))
        clock.advance(0.4)
        assert await r.tick() == 0
        clock.advance(0.2)
        await r.drain()
        assert len(await r.store.outbox(event().key)) == 1
    finally:
        await r.close()


async def test_same_conversation_id_on_other_account_is_isolated(db_config):
    from bae_pilot.config import Account, ConversationRule, Settings
    from bae_pilot.services.runtime import Runtime

    settings = Settings(
        **db_config,
        debounce_s=0,
        min_send_interval_s=0,
        accounts=[Account(), Account(account_id="second")],
        conversations=[ConversationRule(), ConversationRule(account_id="second")],
    )
    r = Runtime(settings)
    await r.start(worker=False)
    try:
        a, b = event("你好"), event("不同账号", account_id="second")
        await r.ingest(a)
        await r.ingest(b)
        await r.drain()
        assert len(await r.store.outbox(a.key)) == len(await r.store.outbox(b.key)) == 1
        assert (await r.store.messages(a.key))[0]["text"] == "你好"
        assert (await r.store.messages(b.key))[0]["text"] == "不同账号"
        assert (await r.store.memories(a.key))[0]["content"] != (await r.store.memories(b.key))[0][
            "content"
        ]
    finally:
        await r.close()


async def test_same_database_cannot_have_two_active_instances(runtime):
    from bae_pilot.domain import AppError
    from bae_pilot.services.runtime import Runtime

    second = Runtime(runtime.settings)
    with pytest.raises(AppError, match="database_in_use"):
        await second.start(worker=False)
    await second.close()


async def test_global_stop_blocks_new_messages(runtime):
    await runtime.change_global("stopped", 0)
    incoming = event()
    await runtime.ingest(incoming)
    await runtime.drain()
    assert await runtime.store.outbox(incoming.key) == []


async def test_unknown_outbound_origin_conservatively_hands_off(runtime):
    incoming = event(
        "本人或来源不明的发言", direction="outbound", origin="unknown", sender_id="self"
    )
    await runtime.ingest(incoming)
    assert (await runtime.store.conversation(incoming.key))["mode"] == "handoff"


async def test_restart_keeps_completed_delivery_and_deduplication(db_config):
    from bae_pilot.config import Settings
    from bae_pilot.services.runtime import Runtime

    settings = Settings(**db_config, debounce_s=0, min_send_interval_s=0)
    r = Runtime(settings)
    await r.start(worker=False)
    await r.ingest(event())
    await r.drain()
    await r.close()
    other = Runtime(settings)
    await other.start(worker=False)
    try:
        assert (await other.ingest(event())).duplicate
        await other.drain()
        assert len(await other.store.outbox(event().key)) == 1
    finally:
        await other.close()
