"""Validated, platform-neutral contracts. Identity never comes from model output."""

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

Identifier = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_.:@-]{1,128}$")]
Mode = Literal["auto", "paused", "handoff", "stopped"]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AppError(Exception):
    def __init__(self, code: str, status: int = 409):
        self.code = code
        self.status = status
        super().__init__(code)


class ConversationKey(Contract):
    channel: Identifier
    account_id: Identifier
    conversation_id: Identifier

    @property
    def scope(self) -> str:
        return json.dumps(
            [self.channel, self.account_id, self.conversation_id], separators=(",", ":")
        )

    @classmethod
    def from_scope(cls, scope: str) -> "ConversationKey":
        channel, account_id, conversation_id = json.loads(scope)
        return cls(channel=channel, account_id=account_id, conversation_id=conversation_id)


class IncomingEvent(Contract):
    schema_version: Literal[1] = 1
    channel: Identifier = "mock"
    account_id: Identifier
    conversation_id: Identifier
    event_id: Identifier
    sender_id: Identifier
    direction: Literal["inbound", "outbound"] = "inbound"
    origin: Literal["peer", "bot", "human", "unknown"] = "peer"
    message_type: Literal["text", "image", "voice"] = "text"
    conversation_type: Literal["direct", "group"] = "direct"
    text: str = Field(default="", max_length=4000)
    occurred_at: AwareDatetime | None = None
    client_send_id: Identifier | None = None

    @model_validator(mode="after")
    def valid_message(self):
        if self.message_type == "text" and not self.text.strip():
            raise ValueError("text messages cannot be empty")
        if (self.direction == "inbound") != (self.origin == "peer"):
            raise ValueError("direction and origin disagree")
        if self.origin == "bot" and not self.client_send_id:
            raise ValueError("bot echo needs client_send_id")
        return self

    @property
    def key(self) -> ConversationKey:
        return ConversationKey(
            channel=self.channel, account_id=self.account_id, conversation_id=self.conversation_id
        )

    @property
    def payload_hash(self) -> str:
        payload = json.dumps(self.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()


class Acceptance(Contract):
    record_id: int
    status: str
    duplicate: bool = False


class SendIntent(Contract):
    send_id: Identifier
    key: ConversationKey
    text: str = Field(min_length=1, max_length=1200)
    source_revision: int


class DeliveryResult(Contract):
    status: Literal["sent", "failed", "unknown"]
    external_message_id: str | None = None
    reason: str | None = None


class Capabilities(Contract):
    channel: Identifier
    available: bool
    reason: str | None = None
    receives_events: bool = False
    send_text: bool = False
    query_delivery: bool = False
    idempotent_send: bool = False
