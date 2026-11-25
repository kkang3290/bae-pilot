"""No model calls or credential discovery on module import."""

import asyncio
import logging
from typing import Protocol, TypeVar

from pydantic import BaseModel, ValidationError

from bae_pilot.domain import AppError
from bae_pilot.models.context import context_chars
from bae_pilot.models.context import model_messages as model_messages

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class ModelProvider(Protocol):
    async def complete(self, role: str, context: dict, schema: type[T]) -> T: ...

    async def close(self) -> None: ...


async def complete_role(
    provider: ModelProvider, settings, role: str, context: dict, schema: type[T]
) -> T:
    """Apply shared per-call limits and validate untrusted provider output."""
    size = context_chars(context)
    if size > settings.max_context_chars:
        logger.warning(
            "context_overflow",
            extra={
                "role": role,
                "required_chars": size,
                "max_context_chars": settings.max_context_chars,
            },
        )
        raise AppError("context_too_large", 413)
    try:
        async with asyncio.timeout(settings.model_timeout_s):
            result = await provider.complete(role, context, schema)
        if isinstance(result, BaseModel):
            result = result.model_dump()
        return schema.model_validate(result)
    except ValidationError as exc:
        raise AppError("invalid_model_output", 502) from exc
