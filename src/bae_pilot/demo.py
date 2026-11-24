"""Deliberately synthetic end-to-end demonstration; never inherits real-model settings."""

import tempfile
from pathlib import Path

from bae_pilot.config import Settings
from bae_pilot.connectors.mock import MockConnector
from bae_pilot.domain import AppError, IncomingEvent
from bae_pilot.services.runtime import Runtime


def require(condition):
    if not condition:
        raise AppError("demo_check_failed", 500)
    return True


async def run_demo():
    with tempfile.TemporaryDirectory(prefix="baepilot-demo-") as directory:
        runtime = Runtime(
            Settings(
                db_path=Path(directory) / "demo.sqlite",
                provider="fake",
                debounce_s=0,
                min_send_interval_s=0,
            )
        )
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
            checks["automatic_reply"] = require(
                (await runtime.store.outbox(first.key))[0]["status"] == "sent"
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
                "mode": "offline",
                "provider": "fake",
                "connector": "mock",
                "checks": checks,
                "deliveries": len(await runtime.store.outbox(first.key)),
                "note": "仅验证框架流程；未连接微信、未调用真实模型。",
            }
        finally:
            await runtime.close()
