import asyncio

from bae_pilot.config import ConversationRule, Settings
from bae_pilot.connectors.mock import MockConnector
from bae_pilot.models.fake import FakeProvider
from bae_pilot.services.runtime import Runtime
from tests.helpers import RecordingProvider, message, running


async def test_slow_background_memory_does_not_block_foreground_with_one_slot(db_config):
    entered, release, second_sent = asyncio.Event(), asyncio.Event(), asyncio.Event()

    class SlowMemory(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "memory" and not entered.is_set():
                entered.set()
                await release.wait()
            return await super().complete(role, context, schema)

    r = Runtime(
        Settings(**db_config, debounce_s=0, min_send_interval_s=0, max_parallel=1),
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


async def test_context_budget_includes_style_and_memory_not_only_message_text(db_config):
    settings = Settings(
        **db_config,
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


async def test_oversized_pending_input_fails_once_without_confirming_or_calling_model(db_config):
    provider = RecordingProvider()
    async with running(db_config, provider=provider, max_context_chars=1000) as r:
        accepted = await r.ingest(message("当前" * 1500))
        await r.drain()
        state = await r.store.conversation(message().key)
        assert state["mode"] == "handoff" and state["last_processed"] == 0
        assert (await r.store.event(accepted.record_id))["status"] == "ignored_control"
        run = await r.store.latest_run(message().key)
        assert run["status"] == "failed" and run["error"] == "context_too_large"
        jobs = await r.store._all("SELECT status,attempts,error FROM jobs WHERE kind='process'")
        assert jobs == [{"status": "failed", "attempts": 1, "error": "context_too_large"}]
        assert await r.store.outbox(message().key) == []
        assert provider.calls == []


async def test_oversized_role_output_does_not_restart_graph(db_config):
    from bae_pilot.agents.schemas import UnderstandingResult

    provider = RecordingProvider(understanding=[UnderstandingResult(unknowns=["未知" * 1500])])
    async with running(db_config, provider=provider, max_context_chars=1200) as r:
        await r.ingest(message("需要谈谈"))
        await r.drain()
        assert (await r.store.conversation(message().key))["mode"] == "handoff"
        assert [role for role, _ in provider.calls] == ["understanding"]
        assert await r.store.outbox(message().key) == []
        assert (await r.store.latest_run(message().key))["error"] == "context_too_large"


async def test_memory_overflow_keeps_prior_snapshot_and_successful_delivery(db_config):
    from bae_pilot.agents.schemas import MemoryItem

    async with running(db_config, max_context_chars=1000) as r:
        old = await r.ingest(message("历史" * 1500))
        await r.store.save_memories(
            message().key,
            old.record_id,
            [
                MemoryItem(
                    kind="summary", content="已保存的明确边界", source_event_ids=[old.record_id]
                )
            ],
            r.clock(),
        )
        prior = await r.store.memories(message().key)
        state = await r.store.conversation(message().key)
        paused = await r.change_mode(message().key, "paused", state["revision"])
        await r.change_mode(message().key, "auto", paused["revision"])
        await r.ingest(message(event_id="new"))
        await r.drain()
        assert (await r.store.conversation(message().key))["mode"] == "auto"
        assert (await r.store.conversation(message().key))["memory_version"] == old.record_id
        assert await r.store.memories(message().key) == prior
        assert [row["status"] for row in await r.store.outbox(message().key)] == ["sent"]
        jobs = await r.store._all("SELECT status,attempts,error FROM jobs WHERE kind='memory'")
        assert jobs == [{"status": "failed", "attempts": 1, "error": "context_too_large"}]


async def test_temporary_provider_failure_still_retries_with_backoff(db_config):
    class Flaky(FakeProvider):
        failed = False

        async def complete(self, role, context, schema):
            if role == "talk" and not self.failed:
                self.failed = True
                raise TimeoutError("synthetic transient failure")
            return await super().complete(role, context, schema)

    now = [1000.0]
    async with running(db_config, provider=Flaky(), clock=lambda: now[0]) as r:
        await r.ingest(message())
        await r.drain()
        assert (await r.store.conversation(message().key))["mode"] == "auto"
        assert await r.store.outbox(message().key) == []
        assert (await r.store.due_jobs(now[0])) == []
        now[0] += 2
        await r.drain()
        assert [row["status"] for row in await r.store.outbox(message().key)] == ["sent"]
        assert await r.store._all("SELECT status,attempts FROM jobs WHERE kind='process'") == [
            {"status": "done", "attempts": 2}
        ]


async def test_old_revision_overflow_cannot_handoff_newer_messages(db_config):
    from bae_pilot.agents.schemas import UnderstandingResult

    entered, release = asyncio.Event(), asyncio.Event()

    class SlowUnderstanding(FakeProvider):
        async def complete(self, role, context, schema):
            if role == "understanding":
                entered.set()
                await release.wait()
                return UnderstandingResult(unknowns=["未知" * 1500])
            return await super().complete(role, context, schema)

    async with running(db_config, provider=SlowUnderstanding(), max_context_chars=1200) as r:
        try:
            await r.ingest(message("需要谈谈"))
            await r.tick()
            await asyncio.wait_for(entered.wait(), 5)
            await r.ingest(message(event_id="newer"))
            release.set()
            await r.drain()
            assert (await r.store.conversation(message().key))["mode"] == "auto"
            assert [row["status"] for row in await r.store.outbox(message().key)] == ["sent"]
            assert (await r.store.latest_run(message().key))["status"] == "completed"
        finally:
            release.set()


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


async def test_failed_memory_extraction_cannot_reference_another_conversation(db_config):
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

    async with running(db_config, provider=WrongEvidence(), max_job_attempts=1) as r:
        await r.ingest(message())
        await r.drain()
        assert await r.store.memories(message().key) == []
        assert (await r.store.outbox(message().key))[0]["status"] == "sent"


async def test_account_pause_reports_only_its_own_inflight_sends(db_config):
    from bae_pilot.config import Account

    entered, release = asyncio.Event(), asyncio.Event()
    r = Runtime(
        Settings(
            **db_config,
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
