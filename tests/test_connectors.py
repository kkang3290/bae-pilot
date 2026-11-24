import pytest


async def test_wechat_is_explicitly_unavailable_and_never_reports_success():
    from bae_pilot.connectors.wechat import WeChatConnector
    from bae_pilot.domain import AppError, ConversationKey, SendIntent

    connector = WeChatConnector()
    assert not connector.capabilities.available
    intent = SendIntent(
        send_id="s1",
        key=ConversationKey(channel="wechat", account_id="a", conversation_id="b"),
        text="test",
        source_revision=0,
    )
    with pytest.raises(AppError, match="connector_not_implemented"):
        await connector.send(intent)
    with pytest.raises(AppError, match="connector_not_implemented"):
        await connector.query("s1")
