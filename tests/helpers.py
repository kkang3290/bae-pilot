"""Shared synthetic inputs and ordinary runtime lifecycle; never used by production."""

from contextlib import asynccontextmanager

from bae_pilot.config import Settings
from bae_pilot.domain import IncomingEvent
from bae_pilot.services.runtime import Runtime


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
async def running(tmp_path, **options):
    provider = options.pop("provider", None)
    clock = options.pop("clock", None)
    settings = Settings(
        **{
            "db_path": tmp_path / "runtime.sqlite",
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
