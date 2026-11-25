import pytest
from pydantic import ValidationError


def test_event_fingerprint_is_stable_but_detects_content_conflicts():
    from bae_pilot.domain import IncomingEvent

    raw = dict(
        account_id="demo-account",
        conversation_id="demo-chat",
        event_id="one",
        sender_id="peer",
        text="你好",
    )
    first = IncomingEvent(**raw)
    assert first.payload_hash == IncomingEvent(**raw).payload_hash
    assert first.payload_hash != IncomingEvent(**{**raw, "text": "变了"}).payload_hash
    assert first.key != IncomingEvent(**{**raw, "account_id": "other"}).key


@pytest.mark.parametrize(
    "update",
    [
        {"text": ""},
        {"event_id": "../escape"},
        {"text": "x" * 4001},
        {"direction": "outbound"},
        {"unexpected": True},
    ],
)
def test_invalid_event_cannot_enter_runtime(update):
    from bae_pilot.domain import IncomingEvent

    raw = dict(
        account_id="demo-account",
        conversation_id="demo-chat",
        event_id="one",
        sender_id="peer",
        text="你好",
    )
    with pytest.raises(ValidationError):
        IncomingEvent(**{**raw, **update})


def test_configuration_only_authorizes_exact_mock_conversation():
    from bae_pilot.config import Settings
    from bae_pilot.domain import ConversationKey

    settings = Settings()
    allowed = ConversationKey(
        channel="mock", account_id="demo-account", conversation_id="demo-chat"
    )
    assert settings.rule_for(allowed).mode == "auto"
    assert settings.rule_for(allowed.model_copy(update={"account_id": "other"})) is None
    assert settings.provider == "fake"


def test_configuration_rejects_unknown_fields_and_invalid_windows():
    from bae_pilot.config import Settings

    with pytest.raises(ValidationError):
        Settings(debounce_s=3, max_wait_s=1)
    with pytest.raises(ValidationError):
        Settings(provider="unknown")
    with pytest.raises(ValidationError):
        Settings(typo=True)


@pytest.mark.parametrize(
    "update",
    [
        {"database_url": "https://localhost/baepilot"},
        {"database_url": "postgresql://localhost/"},
        {"database_schema": "public"},
        {"database_schema": "pg_catalog"},
        {"database_schema": "bad; DROP SCHEMA public"},
    ],
)
def test_database_configuration_rejects_unsafe_targets(update):
    from bae_pilot.config import Settings

    with pytest.raises(ValidationError):
        Settings(**update)


def test_database_credentials_are_hidden_and_environment_overrides_toml(tmp_path, monkeypatch):
    from bae_pilot.config import load_settings

    config = tmp_path / "database.toml"
    config.write_text(
        'database_url="postgresql://file_user:file-secret@localhost/file_db"\n'
        'database_schema="from_file"\n'
    )
    monkeypatch.setenv("BAEPILOT_DATABASE_URL", "postgresql://env_user:env-secret@localhost/env_db")
    monkeypatch.setenv("BAEPILOT_DATABASE_SCHEMA", "from_env")
    settings = load_settings(config)
    assert settings.database_url.get_secret_value() == (
        "postgresql://env_user:env-secret@localhost/env_db"
    )
    assert settings.database_schema == "from_env"
    assert "env-secret" not in repr(settings)
    assert "file-secret" not in repr(settings)
    assert settings.provider == "fake"


def test_secrets_and_provider_opt_in_are_separate(monkeypatch):
    from bae_pilot.config import load_settings

    monkeypatch.setenv("ANTHROPIC_API_KEY", "private-test-value")
    monkeypatch.setenv("BAEPILOT_API_TOKEN", "private-management-value")
    settings = load_settings()
    assert settings.provider == "fake"
    assert "private" not in repr(settings)
    assert settings.api_token.get_secret_value() == "private-management-value"
