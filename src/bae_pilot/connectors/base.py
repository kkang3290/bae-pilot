"""Typed channel interface; receiving adapters submit normalized IncomingEvent objects."""

from typing import Protocol

from bae_pilot.domain import Capabilities, DeliveryResult, SendIntent


class Connector(Protocol):
    capabilities: Capabilities

    async def send(self, intent: SendIntent) -> DeliveryResult: ...

    async def query(self, send_id: str) -> DeliveryResult: ...
