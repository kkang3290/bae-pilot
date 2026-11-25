import asyncio

import pytest

from bae_pilot.config import Settings
from bae_pilot.domain import AppError, Capabilities, DeliveryResult, SendIntent
from bae_pilot.models.fake import FakeProvider
from bae_pilot.services.runtime import Runtime
from tests.helpers import RecordingProvider, message, running


async def test_short_path_can_escalate_without_sending_its_initial_draft(db_config):
    from bae_pilot.agents.schemas import TalkResult

    class Escalate(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "talk" and "strategy" not in context:
                return TalkResult(reply="不可发送的旧稿", requires_deep=True)
            return await super().complete(role, context, schema)

    async with running(db_config, provider=Escalate()) as r:
        incoming = message()
        await r.ingest(incoming)
        await r.drain()
        result = (await r.store.latest_run(incoming.key))["result"]
        assert result["trace"] == [
            "talk",
            "understanding",
            "strategy",
            "talk",
            "review",
            "validate",
        ]
        assert len(await r.store.outbox(incoming.key)) == 1
        assert "不可发送" not in (await r.store.outbox(incoming.key))[0]["text"]


async def test_exhausted_model_budget_hands_off_without_outbox(db_config):
    async with running(db_config, model_max_calls=1, max_job_attempts=1) as r:
        incoming = message("复杂的沟通")
        await r.ingest(incoming)
        await r.drain()
        assert (await r.store.latest_run(incoming.key))["error"] == "model_budget_exhausted"
        assert (await r.store.conversation(incoming.key))["mode"] == "handoff"
        assert await r.store.outbox(incoming.key) == []


async def test_memory_failure_cannot_undo_successful_send(db_config):
    class BrokenMemory(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "memory":
                raise ValueError("simulated external model error")
            return await super().complete(role, context, schema)

    async with running(db_config, provider=BrokenMemory(), max_job_attempts=1) as r:
        incoming = message()
        await r.ingest(incoming)
        await r.drain()
        assert (await r.store.outbox(incoming.key))[0]["status"] == "sent"
        assert (await r.store.conversation(incoming.key))["mode"] == "auto"
        assert await r.store.memories(incoming.key) == []


async def test_old_memory_cannot_overwrite_newer_and_evidence_is_scoped(runtime):
    from bae_pilot.agents.schemas import MemoryItem

    incoming = message()
    accepted = await runtime.ingest(incoming)
    await runtime.drain()
    original = await runtime.store.memories(incoming.key)
    await runtime.store.save_memories(
        incoming.key,
        accepted.record_id,
        [MemoryItem(kind="inference", content="过时推测", source_event_ids=[accepted.record_id])],
        runtime.clock(),
    )
    assert await runtime.store.memories(incoming.key) == original
    with pytest.raises(AppError, match="invalid_memory_evidence"):
        await runtime.store.save_memories(
            incoming.key,
            9999,
            [MemoryItem(kind="statement", content="无依据内容", source_event_ids=[9999])],
            runtime.clock(),
        )


@pytest.mark.parametrize("accepted_by_channel", [True, False])
async def test_crash_during_send_recovers_unknown_without_resending(db_config, accepted_by_channel):
    settings = Settings(**db_config, debounce_s=0, min_send_interval_s=0)
    r = Runtime(settings)
    await r.start(worker=False)
    incoming = message()
    await r.ingest(incoming)
    snapshot = await r.store.snapshot(incoming.key, r.clock(), 40, r.settings.max_context_chars)
    result = await r.graph.run({**snapshot, "style": "自然"})
    send_id = await r.store.finish_run(
        incoming.key, snapshot, result.model_dump(), r.clock(), 60, 0
    )
    row = await r.store.claim_delivery(send_id, True, r.clock())
    if accepted_by_channel:
        await r.connectors["mock"].send(
            SendIntent(
                send_id=send_id,
                key=incoming.key,
                text=row["text"],
                source_revision=row["source_revision"],
            )
        )
    # The crash point is after durable claim, before recording any acknowledgement.
    await r.close()
    recovered = Runtime(settings)
    await recovered.start(worker=False)
    try:
        await recovered.ingest(message("新消息", event_id="two"))
        await recovered.drain()
        assert [x["status"] for x in await recovered.store.outbox(incoming.key)] == ["unknown"]
        outcome = await recovered.reconcile(send_id)
        assert outcome["status"] == ("sent" if accepted_by_channel else "unknown")
        assert len(await recovered.store.outbox(incoming.key)) == 1
    finally:
        await recovered.close()


async def test_pause_reports_already_started_send_and_blocks_later_claims(db_config):
    from bae_pilot.connectors.mock import MockConnector

    entered, release = asyncio.Event(), asyncio.Event()
    async with running(db_config) as r:

        class SlowTransport(MockConnector):
            async def send(self, intent):
                entered.set()
                await release.wait()
                return await super().send(intent)

        r.connectors["mock"] = SlowTransport(r.store)
        incoming = message()
        await r.ingest(incoming)
        await r.tick()
        await asyncio.wait_for(entered.wait(), 2)
        current = await r.store.conversation(incoming.key)
        paused = await r.change_mode(incoming.key, "paused", current["revision"])
        assert paused["in_flight"] == 1
        release.set()
        await r.drain()
        await r.ingest(message("第二条", event_id="two"))
        await r.drain()
        assert len(await r.store.outbox(incoming.key)) == 1


async def test_rate_limit_uses_all_connector_acknowledgements_not_mock_receipts(db_config):
    class Clock:
        value = 1000.0

        def __call__(self):
            return self.value

    class OtherTransport:
        capabilities = Capabilities(channel="mock", available=True, send_text=True)

        async def send(self, intent):
            return DeliveryResult(status="sent", external_message_id="external-" + intent.send_id)

    clock = Clock()
    r = Runtime(
        Settings(**db_config, debounce_s=0, min_send_interval_s=10),
        clock=clock,
    )
    await r.start(worker=False)
    r.connectors["mock"] = OtherTransport()
    try:
        await r.ingest(message())
        await r.drain()
        await r.ingest(message(event_id="two"))
        await r.drain()
        assert [x["status"] for x in await r.store.outbox(message().key)] == ["sent", "pending"]
        clock.value += 10
        await r.drain()
        assert [x["status"] for x in await r.store.outbox(message().key)] == ["sent", "sent"]
    finally:
        await r.close()


async def test_outbound_anchor_outside_window_is_retained_and_echo_is_excluded(db_config):
    provider = RecordingProvider()
    async with running(db_config, provider=provider, context_message_limit=1) as r:
        await r.ingest(message())
        await r.drain()
        sent = (await r.store.outbox(message().key))[0]
        anchor = await r.store._one("SELECT record_id FROM events WHERE status='sent'")
        echo = await r.ingest(
            message(
                sent["text"],
                event_id="echo",
                direction="outbound",
                origin="bot",
                sender_id="self",
                client_send_id=sent["send_id"],
            )
        )
        current = await r.ingest(message("谢谢", event_id="next"))
        await r.drain()
        requests = [ctx for role, ctx in provider.calls if role == "talk"]
        assert [m["record_id"] for m in requests[1]["messages"]] == [
            anchor["record_id"],
            current.record_id,
        ]
        assert echo.record_id not in {m["record_id"] for m in requests[1]["messages"]}
        assert len(await r.store.outbox(message().key)) == 2


async def test_late_send_acknowledgement_is_visible_beyond_queued_watermark(db_config):
    from bae_pilot.connectors.mock import MockConnector

    entered, release = asyncio.Event(), asyncio.Event()
    provider = RecordingProvider()
    async with running(db_config, provider=provider) as r:

        class SlowFirstSend(MockConnector):
            async def send(self, intent):
                if not entered.is_set():
                    entered.set()
                    await release.wait()
                return await super().send(intent)

        r.connectors["mock"] = SlowFirstSend(r.store)
        try:
            await r.ingest(message())
            await r.tick()
            await asyncio.wait_for(entered.wait(), 5)
            current = await r.ingest(message(event_id="while-sending"))
            release.set()
            await r.drain()
            requests = [ctx for role, ctx in provider.calls if role == "talk"]
            assert len(requests) == 2
            outgoing = [m for m in requests[1]["messages"] if m["direction"] == "outbound"]
            assert len(outgoing) == 1 and outgoing[0]["record_id"] > current.record_id
            assert [row["status"] for row in await r.store.outbox(message().key)] == [
                "sent",
                "sent",
            ]
        finally:
            release.set()


async def test_pending_event_survives_restart_without_being_lost(db_config):
    settings = Settings(**db_config, debounce_s=0)
    r = Runtime(settings)
    await r.start(worker=False)
    await r.ingest(message())
    await r.close()
    recovered = Runtime(settings)
    await recovered.start(worker=False)
    try:
        await recovered.drain()
        assert (await recovered.store.outbox(message().key))[0]["status"] == "sent"
    finally:
        await recovered.close()
