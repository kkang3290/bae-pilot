"""The only service allowed to invoke external sends."""

import asyncio

from bae_pilot.domain import AppError, ConversationKey, DeliveryResult, SendIntent


class DispatchService:
    def __init__(self, runtime):
        self.runtime = runtime

    async def send(self, send_id):
        r = self.runtime
        row = await r.store.delivery(send_id)
        key = ConversationKey.from_scope(row["scope"])
        connector = r.connectors.get(key.channel)
        account = r.settings.account_for(key)
        authorized = (
            account is not None
            and r.settings.rule_for(key) is not None
            and connector is not None
            and connector.capabilities.available
            and connector.capabilities.send_text
        )
        # The lock orders claims against controls; do not hold it over network I/O.
        async with r.control_gate:
            claimed = await r.store.claim_delivery(send_id, authorized, r.clock())
        if not claimed:
            return
        intent = SendIntent(
            send_id=send_id, key=key, text=row["text"], source_revision=row["source_revision"]
        )
        try:
            async with asyncio.timeout(r.settings.model_timeout_s):
                result = DeliveryResult.model_validate(await connector.send(intent))
        except Exception:
            result = DeliveryResult(status="unknown", reason="send_unconfirmed")
        await r.store.record_delivery(
            send_id, result, account.self_id, r.clock(), r.settings.max_pending_jobs
        )

    async def reconcile(self, send_id, confirmed_status=None):
        r = self.runtime
        row = await r.store.delivery(send_id)
        if row["status"] != "unknown":
            return row
        key = ConversationKey.from_scope(row["scope"])
        account = r.settings.account_for(key)
        if account is None:
            raise AppError("account_not_authorized", 403)
        if confirmed_status:
            if confirmed_status not in ("sent", "failed"):
                raise AppError("invalid_confirmation", 422)
            result = DeliveryResult(status=confirmed_status, reason="operator_confirmed")
        else:
            connector = r.connectors.get(key.channel)
            if connector is None or not connector.capabilities.query_delivery:
                raise AppError("delivery_query_unavailable", 501)
            try:
                async with asyncio.timeout(r.settings.model_timeout_s):
                    result = DeliveryResult.model_validate(await connector.query(send_id))
            except Exception:
                result = DeliveryResult(status="unknown", reason="query_unconfirmed")
        async with r.control_gate:
            result = await r.store.record_delivery(
                send_id,
                result,
                account.self_id,
                r.clock(),
                r.settings.max_pending_jobs,
                reconciliation=True,
            )
        r.wake.set()
        return result
