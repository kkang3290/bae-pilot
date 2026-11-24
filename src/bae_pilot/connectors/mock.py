"""Local development transport with durable, independently queryable receipts."""

import time

from bae_pilot.domain import Capabilities, DeliveryResult


class MockConnector:
    capabilities = Capabilities(
        channel="mock",
        available=True,
        receives_events=True,
        send_text=True,
        query_delivery=True,
        idempotent_send=True,
    )

    def __init__(self, store, clock=time.time):
        self.store = store
        self.clock = clock

    async def send(self, intent):
        external_id = await self.store.mock_send(intent, self.clock())
        return DeliveryResult(status="sent", external_message_id=external_id)

    async def query(self, send_id):
        row = await self.store.mock_receipt(send_id)
        return (
            DeliveryResult(status="sent", external_message_id=row["external_message_id"])
            if row
            else DeliveryResult(status="unknown", reason="receipt_not_found")
        )
