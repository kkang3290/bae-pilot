"""Deliberately unavailable: implementing a real WeChat bridge is a later milestone."""

from bae_pilot.domain import AppError, Capabilities


class WeChatConnector:
    capabilities = Capabilities(
        channel="wechat", available=False, reason="connector_not_implemented"
    )

    async def send(self, intent):
        raise AppError("connector_not_implemented", 501)

    async def query(self, send_id):
        raise AppError("connector_not_implemented", 501)
