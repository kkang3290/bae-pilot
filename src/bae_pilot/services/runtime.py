"""Single-process durable scheduler with per-conversation serialization."""

import asyncio
import logging
import time

from bae_pilot.agents.graph import ConversationGraph
from bae_pilot.connectors.mock import MockConnector
from bae_pilot.connectors.wechat import WeChatConnector
from bae_pilot.domain import AppError, ConversationKey
from bae_pilot.models.fake import FakeProvider
from bae_pilot.services.dispatch import DispatchService
from bae_pilot.services.ingestion import IngestionService
from bae_pilot.services.memory import MemoryService
from bae_pilot.storage.repositories import Store

logger = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings, *, provider=None, clock=time.time):
        self.settings = settings
        self.provider = provider
        self.clock = clock
        self.store = None
        self.connectors = {}
        self.control_gate = asyncio.Lock()
        self._tick_gate = asyncio.Lock()
        self.wake = asyncio.Event()
        self._active = {}
        self._worker = None
        self._closing = False
        self.ingestion = IngestionService(self)
        self.dispatch = DispatchService(self)
        self.memory = MemoryService(self)

    async def start(self, *, worker=True):
        if self.store is not None:
            raise AppError("runtime_already_started")
        self.store = await Store.open(self.settings)
        try:
            await self.store.recover(self.clock())
            if self.provider is None:
                if self.settings.provider == "fake":
                    self.provider = FakeProvider()
                else:
                    from bae_pilot.models.anthropic import AnthropicProvider

                    self.provider = AnthropicProvider(self.settings)
            self.graph = ConversationGraph(self.provider, self.settings)
            self.connectors.update(
                mock=MockConnector(self.store, self.clock), wechat=WeChatConnector()
            )
            self._closing = False
            if worker:
                self._worker = asyncio.create_task(self._loop(), name="baepilot-worker")
        except BaseException:
            await self.store.close()
            self.store = None
            raise

    async def close(self):
        self._closing = True
        self.wake.set()
        if self._worker:
            self._worker.cancel()
            await asyncio.gather(self._worker, return_exceptions=True)
            self._worker = None
        tasks = list(self._active.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._active.clear()
        if self.provider is not None:
            await self.provider.close()
        if self.store:
            await self.store.close()
            self.store = None

    async def ingest(self, event, *, bound_channel=None):
        return await self.ingestion.accept(event, bound_channel)

    async def change_mode(self, key, mode, expected_revision):
        if mode == "auto" and self.settings.rule_for(key) is None:
            raise AppError("conversation_not_authorized", 403)
        async with self.control_gate:
            result = await self.store.change_mode(key, mode, expected_revision, self.clock())
        self.wake.set()
        return result

    async def change_global(self, mode, expected_revision, *, channel=None, account_id=None):
        key = None
        if channel is not None or account_id is not None:
            if not channel or not account_id:
                raise AppError("account_identity_required", 422)
            key = ConversationKey(channel=channel, account_id=account_id, conversation_id="control")
            if self.settings.account_for(key) is None:
                raise AppError("account_not_authorized", 403)
        async with self.control_gate:
            result = await self.store.change_control(mode, expected_revision, self.clock(), key)
        self.wake.set()
        return result

    async def reconcile(self, send_id, confirmed_status=None):
        return await self.dispatch.reconcile(send_id, confirmed_status)

    async def _run_job(self, job):
        key = ConversationKey.from_scope(job["scope"])
        snapshot = None
        try:
            if job["kind"] == "memory":
                await self.memory.update(key, job["payload"]["source_version"])
            else:
                rule = self.settings.rule_for(key)
                if rule is not None:
                    snapshot = await self.store.snapshot(
                        key, self.clock(), self.settings.context_message_limit
                    )
                    if snapshot:
                        result = await self.graph.run({**snapshot, "style": rule.style})
                        send_id = await self.store.finish_run(
                            key,
                            snapshot,
                            result.model_dump(),
                            self.clock(),
                            self.settings.reply_ttl_s,
                            self.settings.min_send_interval_s,
                        )
                        if send_id:
                            await self.dispatch.send(send_id)
            await self.store.finish_job(job["job_id"])
        except Exception as exc:
            code = exc.code if isinstance(exc, AppError) else type(exc).__name__
            if snapshot:
                await self.store.fail_run(snapshot["run_id"], code)
            attempts = job["attempts"] + 1
            retry = (
                self.clock() + 2**attempts if attempts < self.settings.max_job_attempts else None
            )
            await self.store.finish_job(job["job_id"], error=code, retry_at=retry)
            if retry is None and snapshot:
                async with self.control_gate:
                    current = await self.store.conversation(key)
                    if current["revision"] == snapshot["revision"] and current["mode"] == "auto":
                        await self.store.change_mode(
                            key, "handoff", current["revision"], self.clock()
                        )
            logger.warning("job_failed", extra={"job_id": job["job_id"], "error_code": code})

    async def _send_pending(self, send_id):
        try:
            await self.dispatch.send(send_id)
        except Exception as exc:
            logger.warning("dispatch_failed", extra={"error_code": type(exc).__name__})

    def _reap(self):
        for scope, task in list(self._active.items()):
            if task.done():
                del self._active[scope]
                task.result()

    async def tick(self):
        async with self._tick_gate:
            return await self._tick()

    def _foreground_count(self):
        return sum(kind != "memory" for kind, _ in self._active)

    async def _tick(self):
        self._reap()
        started = 0
        for job in await self.store.due_jobs(self.clock()):
            identity = (job["kind"], job["scope"])
            if identity in self._active:
                continue
            if job["kind"] == "memory":
                if any(kind == "memory" for kind, _ in self._active):
                    continue
            elif self._foreground_count() >= self.settings.max_parallel:
                continue
            if await self.store.claim_job(job["job_id"]):
                self._active[identity] = asyncio.create_task(self._run_job(job))
                started += 1
        for row in await self.store.pending_deliveries(self.clock()):
            identity = ("process", row["scope"])
            if (
                identity not in self._active
                and self._foreground_count() < self.settings.max_parallel
            ):
                self._active[identity] = asyncio.create_task(self._send_pending(row["send_id"]))
                started += 1
        return started

    async def drain(self, max_rounds=200):
        """Process currently due work only; tests advance a clock for future work."""
        for _ in range(max_rounds):
            started = await self.tick()
            if self._active:
                await asyncio.gather(*list(self._active.values()))
            elif not started:
                return
        raise AppError("drain_budget_exhausted", 503)

    async def _loop(self):
        while not self._closing:
            self.wake.clear()
            await self.tick()
            try:
                await asyncio.wait_for(self.wake.wait(), timeout=0.05)
            except TimeoutError:
                pass
