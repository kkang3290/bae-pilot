"""Shared synthetic inputs and ordinary runtime lifecycle; never used by production."""

from contextlib import asynccontextmanager
from copy import deepcopy

from bae_pilot.config import Settings
from bae_pilot.domain import IncomingEvent
from bae_pilot.models.fake import FakeProvider
from bae_pilot.services.runtime import Runtime


class RecordingProvider(FakeProvider):
    """Capture real graph requests and optionally script only the external model output."""

    def __init__(self, **responses):
        self.calls = []
        self.responses = {role: list(items) for role, items in responses.items()}

    async def complete(self, role, context, schema):
        self.calls.append((role, deepcopy(context)))
        if self.responses.get(role):
            return self.responses[role].pop(0)
        return await super().complete(role, context, schema)


def message(text="你好", **changes):
    return IncomingEvent(
        **{
            "account_id": "demo-account",
            "conversation_id": "demo-chat",
            "event_id": "one",
            "sender_id": "peer",
            "text": text,
            **changes,
        }
    )


@asynccontextmanager
async def running(db_config, **options):
    provider = options.pop("provider", None)
    clock = options.pop("clock", None)
    settings = Settings(
        **{
            **db_config,
            "debounce_s": 0,
            "min_send_interval_s": 0,
            **options,
        }
    )
    instance = Runtime(settings, provider=provider, **({"clock": clock} if clock else {}))
    await instance.start(worker=False)
    try:
        yield instance
    finally:
        await instance.close()
