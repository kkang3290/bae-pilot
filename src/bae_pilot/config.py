"""Explicit configuration; API credentials never select a model implicitly."""

import os
import tomllib
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator

from bae_pilot.domain import Contract, ConversationKey, Identifier, Mode


class Account(Contract):
    channel: Identifier = "mock"
    account_id: Identifier = "demo-account"
    self_id: Identifier = "self"


class ConversationRule(ConversationKey):
    channel: Identifier = "mock"
    account_id: Identifier = "demo-account"
    conversation_id: Identifier = "demo-chat"
    mode: Mode = "auto"
    style: str = Field(default="自然、简短、认真倾听；不虚构经历或承诺。", max_length=2000)


class Settings(Contract):
    database_url: SecretStr = Field(
        default=SecretStr("postgresql://localhost:5432/baepilot"), repr=False
    )
    database_schema: str = Field(default="baepilot", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    provider: Literal["fake", "anthropic"] = "fake"
    model: str = "claude-opus-5-5"
    model_timeout_s: float = Field(default=30, gt=0, le=300)
    model_max_tokens: int = Field(default=2000, ge=256, le=16000)
    model_max_calls: int = Field(default=8, ge=1, le=12)
    refusal_fallback: bool = True
    api_token: SecretStr | None = Field(default=None, repr=False)
    debounce_s: float = Field(default=0.5, ge=0, le=30)
    max_wait_s: float = Field(default=2, ge=0, le=60)
    min_send_interval_s: float = Field(default=1, ge=0, le=3600)
    reply_ttl_s: float = Field(default=60, gt=0, le=3600)
    max_parallel: int = Field(default=4, ge=1, le=32)
    max_pending_jobs: int = Field(default=1000, ge=1, le=10000)
    max_job_attempts: int = Field(default=3, ge=1, le=5)
    max_reply_chars: int = Field(default=1200, ge=1, le=1200)
    context_message_limit: int = Field(default=40, ge=1, le=200)
    max_context_chars: int = Field(default=32000, ge=1000, le=200000)
    accounts: tuple[Account, ...] = (Account(),)
    conversations: tuple[ConversationRule, ...] = (ConversationRule(),)

    @field_validator("database_url")
    @classmethod
    def postgres_url(cls, value):
        parsed = urlsplit(value.get_secret_value())
        if parsed.scheme not in ("postgresql", "postgres") or not parsed.path.strip("/"):
            raise ValueError("a PostgreSQL URL with an explicit database is required")
        return value

    @field_validator("database_schema")
    @classmethod
    def application_schema(cls, value):
        if value in ("public", "information_schema") or value.startswith("pg_"):
            raise ValueError("a dedicated application schema is required")
        return value

    @model_validator(mode="after")
    def consistent(self):
        if self.max_wait_s < self.debounce_s:
            raise ValueError("max_wait_s must be >= debounce_s")
        accounts = {(a.channel, a.account_id) for a in self.accounts}
        if len(accounts) != len(self.accounts):
            raise ValueError("duplicate account")
        keys = [r.scope for r in self.conversations]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate conversation rule")
        if any((r.channel, r.account_id) not in accounts for r in self.conversations):
            raise ValueError("conversation refers to unregistered account")
        return self

    def rule_for(self, key: ConversationKey) -> ConversationRule | None:
        return next((r for r in self.conversations if r.scope == key.scope), None)

    def account_for(self, key: ConversationKey) -> Account | None:
        return next(
            (
                a
                for a in self.accounts
                if (a.channel, a.account_id) == (key.channel, key.account_id)
            ),
            None,
        )


def load_settings(path: str | Path | None = None) -> Settings:
    selected = path or os.environ.get("BAEPILOT_CONFIG")
    data = {}
    if selected:
        with Path(selected).open("rb") as stream:
            data = tomllib.load(stream)
    for env, field in [
        ("BAEPILOT_DATABASE_URL", "database_url"),
        ("BAEPILOT_DATABASE_SCHEMA", "database_schema"),
        ("BAEPILOT_PROVIDER", "provider"),
        ("BAEPILOT_API_TOKEN", "api_token"),
    ]:
        if env in os.environ:
            data[field] = os.environ[env]
    return Settings.model_validate(data)
