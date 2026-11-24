"""No model calls or credential discovery on module import."""

import asyncio
import json
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from bae_pilot.domain import AppError

T = TypeVar("T", bound=BaseModel)


class ModelProvider(Protocol):
    async def complete(self, role: str, context: dict, schema: type[T]) -> T: ...

    async def close(self) -> None: ...


def model_messages(messages):
    """One explicit allowlist for foreground and memory model inputs."""
    return [
        {
            field: message[field]
            for field in ("record_id", "direction", "origin", "message_type", "text")
        }
        for message in messages
    ]


async def complete_role(
    provider: ModelProvider, settings, role: str, context: dict, schema: type[T]
) -> T:
    """Apply shared per-call limits and validate untrusted provider output."""
    if len(json.dumps(context, ensure_ascii=False)) > settings.max_context_chars:
        raise AppError("context_too_large", 413)
    try:
        async with asyncio.timeout(settings.model_timeout_s):
            result = await provider.complete(role, context, schema)
        if isinstance(result, BaseModel):
            result = result.model_dump()
        return schema.model_validate(result)
    except ValidationError as exc:
        raise AppError("invalid_model_output", 502) from exc
