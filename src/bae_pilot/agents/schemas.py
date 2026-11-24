"""Role-specific structured outputs. None can name recipients or grant permissions."""

from typing import Literal

from pydantic import Field, model_validator

from bae_pilot.domain import Contract


class Evidence(Contract):
    statement: str = Field(min_length=1, max_length=1000)
    source_event_ids: list[int] = Field(min_length=1, max_length=40)


class UnderstandingResult(Contract):
    facts: list[Evidence] = Field(default_factory=list, max_length=40)
    interpretations: list[str] = Field(default_factory=list, max_length=10)
    unknowns: list[str] = Field(default_factory=list, max_length=10)
    needs_handoff: bool = False


class StrategyResult(Contract):
    goal: str = Field(min_length=1, max_length=1000)
    tone: str = Field(default="真诚自然", max_length=300)
    avoid: list[str] = Field(default_factory=list, max_length=10)


class TalkResult(Contract):
    action: Literal["reply", "no_reply", "handoff"] = "reply"
    reply: str = Field(default="", max_length=1200)
    requires_deep: bool = False

    @model_validator(mode="after")
    def valid_reply(self):
        if self.action == "reply" and not self.requires_deep and not self.reply.strip():
            raise ValueError("reply action requires nonempty text")
        return self


class ReviewResult(Contract):
    verdict: Literal["pass", "revise", "handoff"]
    reason: str = Field(default="", max_length=1000)


class MemoryItem(Contract):
    kind: Literal["statement", "summary", "inference"]
    content: str = Field(min_length=1, max_length=2000)
    source_event_ids: list[int] = Field(min_length=1, max_length=40)


class MemoryUpdate(Contract):
    items: list[MemoryItem] = Field(default_factory=list, max_length=20)


class GraphResult(Contract):
    status: Literal["reply", "no_reply", "handoff"]
    reply: str = ""
    trace: list[str]
    model_calls: int
    reason: str = ""
