import asyncio

from bae_pilot.config import ConversationRule, Settings
from bae_pilot.connectors.mock import MockConnector
from bae_pilot.models.fake import FakeProvider
from bae_pilot.services.runtime import Runtime
from tests.helpers import message, running


async def test_slow_background_memory_does_not_block_foreground_with_one_slot(tmp_path):
    entered, release, second_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowMemory(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "memory" and not entered.is_set():
                entered.set()
                await release.wait()
            return await super().complete(role, context, schema)

    r = Runtime(
        Settings(
            db_path=tmp_path / "memory.sqlite", debounce_s=0, min_send_interval_s=0, max_parallel=1
        ),
        provider=SlowMemory(),
    )
    await r.start(worker=False)

    class ObservedChannel(MockConnector):
        async def send(self, intent):
            outcome = await super().send(intent)
            if len(await self.store.outbox(intent.key)) == 2:
                second_sent.set()
            return outcome

    r.connectors["mock"] = ObservedChannel(r.store)
    drain = None
    try:
        await r.ingest(message())
        drain = asyncio.create_task(r.drain())
        await asyncio.wait_for(entered.wait(), 2)
        await r.ingest(message(event_id="two"))
        await r.tick()
        await asyncio.wait_for(second_sent.wait(), 1)
        release.set()
        await drain
        assert [x["status"] for x in await r.store.outbox(message().key)] == ["sent", "sent"]
    finally:
        release.set()
        if drain:
            await asyncio.gather(drain, return_exceptions=True)
        await r.close()


async def test_context_budget_includes_style_and_memory_not_only_message_text(tmp_path):
    settings = Settings(
        db_path=tmp_path / "context.sqlite",
        debounce_s=0,
        max_context_chars=1000,
        max_job_attempts=1,
        conversations=[ConversationRule(style="很长的风格" * 250)],
    )
    r = Runtime(settings)
    await r.start(worker=False)
    try:
        await r.ingest(message())
        await r.drain()
        run = await r.store.latest_run(message().key)
        assert run["status"] == "failed" and run["error"] == "context_too_large"
        assert await r.store.outbox(message().key) == []
    finally:
        await r.close()


async def test_concurrent_duplicate_events_only_create_one_delivery(runtime):
    results = await asyncio.gather(*(runtime.ingest(message()) for _ in range(10)))
    assert sum(not item.duplicate for item in results) == 1
    await runtime.drain()
    assert len(await runtime.store.outbox(message().key)) == 1


async def test_unsupported_media_is_not_treated_as_understood_text(runtime):
    incoming = message(message_type="image", text="")
    await runtime.ingest(incoming)
    await runtime.drain()
    assert (await runtime.store.conversation(incoming.key))["mode"] == "handoff"
    assert await runtime.store.outbox(incoming.key) == []


async def test_failed_memory_extraction_cannot_reference_another_conversation(tmp_path):
    from bae_pilot.agents.schemas import MemoryItem, MemoryUpdate

    class WrongEvidence(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "memory":
                return MemoryUpdate(
                    items=[
                        MemoryItem(kind="statement", content="越界内容", source_event_ids=[999999])
                    ]
                )
            return await super().complete(role, context, schema)

    async with running(tmp_path, provider=WrongEvidence(), max_job_attempts=1) as r:
        await r.ingest(message())
        await r.drain()
        assert await r.store.memories(message().key) == []
        assert (await r.store.outbox(message().key))[0]["status"] == "sent"


async def test_account_pause_reports_only_its_own_inflight_sends(tmp_path):
    from bae_pilot.config import Account

    entered, release = asyncio.Event(), asyncio.Event()
    r = Runtime(
        Settings(
            db_path=tmp_path / "accounts.sqlite",
            debounce_s=0,
            min_send_interval_s=0,
            accounts=[Account(), Account(account_id="second-account")],
            conversations=[ConversationRule(), ConversationRule(account_id="second-account")],
        )
    )
    await r.start(worker=False)
    active_calls = 0

    class SlowChannel(MockConnector):
        async def send(self, intent):
            nonlocal active_calls
            active_calls += 1
            if active_calls == 2:
                entered.set()
            await release.wait()
            return await super().send(intent)

    r.connectors["mock"] = SlowChannel(r.store)
    try:
        await r.ingest(message())
        await r.ingest(message(account_id="second-account"))
        await r.tick()
        await asyncio.wait_for(entered.wait(), 2)
        paused = await r.change_global("paused", 0, channel="mock", account_id="demo-account")
        assert paused["in_flight"] == 1
        release.set()
        await r.drain()
        ignored = await r.ingest(message(event_id="two"))
        await r.ingest(message(account_id="second-account", event_id="two"))
        await r.drain()
        assert ignored.status == "ignored_mode"
        assert len(await r.store.outbox(message().key)) == 1
        assert len(await r.store.outbox(message(account_id="second-account").key)) == 2
    finally:
        release.set()
        await r.close()
