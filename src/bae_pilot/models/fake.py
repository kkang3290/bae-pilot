"""Deterministic offline development provider, not an emotional-intelligence benchmark."""

from bae_pilot.agents.schemas import (
    Evidence,
    MemoryItem,
    MemoryUpdate,
    ReviewResult,
    StrategyResult,
    TalkResult,
    UnderstandingResult,
)


class FakeProvider:
    async def complete(self, role, context, schema):
        peer = [
            m
            for m in context["messages"]
            if m["direction"] == "inbound" and m["message_type"] == "text"
        ]
        if role == "understanding":
            value = UnderstandingResult(
                facts=[
                    Evidence(
                        statement="对方表示：" + m["text"][:980], source_event_ids=[m["record_id"]]
                    )
                    for m in peer[-10:]
                ],
                unknowns=["对方未明确说明希望倾听还是获得建议"],
            )
        elif role == "strategy":
            value = StrategyResult(
                goal="先回应感受，再确认对方想聊什么", avoid=["虚构原因", "未经确认的新承诺"]
            )
        elif role == "talk":
            value = TalkResult(
                reply="我在，想聊什么？"
                if not context.get("strategy")
                else "我在认真听。你愿意说说，最让你在意的是哪一点吗？"
            )
        elif role == "review":
            value = ReviewResult(verdict="pass", reason="离线流程示例，不代表真实语义审核")
        elif role == "memory":
            value = (
                MemoryUpdate(
                    items=[
                        MemoryItem(
                            kind="summary",
                            content="对方最近表示：" + peer[-1]["text"][:1500],
                            source_event_ids=[peer[-1]["record_id"]],
                        )
                    ]
                )
                if peer
                else MemoryUpdate()
            )
        else:
            raise ValueError("unknown model role")
        return schema.model_validate(value.model_dump())

    async def close(self):
        return None
