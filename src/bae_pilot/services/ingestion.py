"""Normalize only authenticated/bound channel input, never model instructions."""

from bae_pilot.domain import AppError


class IngestionService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def accept(self, event, bound_channel=None):
        runtime, key = self.runtime, event.key
        if bound_channel is not None and event.channel != bound_channel:
            raise AppError("invalid_channel", 422)
        account = runtime.settings.account_for(key)
        if account is None:
            raise AppError("account_not_authorized", 403)
        connector = runtime.connectors.get(key.channel)
        if connector is None or not connector.capabilities.available:
            raise AppError("connector_not_implemented", 501)
        if (event.direction == "outbound") != (event.sender_id == account.self_id):
            raise AppError("invalid_sender_direction", 422)
        rule = runtime.settings.rule_for(key)
        async with runtime.control_gate:
            result = await runtime.store.ingest(
                event,
                initial_mode=rule.mode if rule else "paused",
                authorized=rule is not None,
                now=runtime.clock(),
                debounce=runtime.settings.debounce_s,
                max_wait=runtime.settings.max_wait_s,
                capacity=runtime.settings.max_pending_jobs,
            )
        runtime.wake.set()
        return result
