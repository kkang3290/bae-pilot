"""Evidence-bearing memory derived only from persisted messages, never draft text."""

from bae_pilot.agents.schemas import MemoryUpdate
from bae_pilot.domain import AppError
from bae_pilot.models.base import complete_role, model_messages


class MemoryService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def update(self, key, version):
        r = self.runtime
        messages = await r.store.memory_context(key, version, r.settings.context_message_limit)
        messages = model_messages(messages)
        output = await complete_role(
            r.provider, r.settings, "memory", {"messages": messages}, MemoryUpdate
        )
        visible_ids = {m["record_id"] for m in messages}
        if any(not set(item.source_event_ids) <= visible_ids for item in output.items):
            raise AppError("invalid_memory_evidence", 502)
        await r.store.save_memories(key, version, output.items, r.clock())
