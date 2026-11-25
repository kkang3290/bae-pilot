import json

import psycopg
import pytest
from psycopg import sql


def test_demo_ignores_model_env_and_cleans_only_its_owned_schema(db_config, monkeypatch, capsys):
    monkeypatch.setenv("BAEPILOT_DATABASE_URL", db_config["database_url"])
    monkeypatch.setenv("BAEPILOT_DATABASE_SCHEMA", db_config["database_schema"])
    monkeypatch.setenv("BAEPILOT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    from bae_pilot.cli import main

    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "synthetic"
    assert result["database"] == "postgresql"
    assert set(result["checks"]) == {
        "automatic_reply",
        "complex_path",
        "duplicate_event",
        "human_handoff",
        "unknown_delivery",
    }
    assert all(result["checks"].values())
    with psycopg.connect(db_config["database_url"]) as connection:
        assert connection.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname=%s", (db_config["database_schema"],)
        ).fetchone() == (db_config["database_schema"],)
        assert (
            connection.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'baepilot_demo_%'"
            ).fetchall()
            == []
        )


def test_failed_demo_reports_error_and_still_cleans_its_schema(db_config, monkeypatch, capsys):
    from bae_pilot.agents.schemas import TalkResult
    from bae_pilot.cli import main
    from bae_pilot.models.fake import FakeProvider

    original = FakeProvider.complete

    async def no_reply(self, role, context, schema):
        if role == "talk":
            return TalkResult(action="no_reply")
        return await original(self, role, context, schema)

    monkeypatch.setattr(FakeProvider, "complete", no_reply)
    monkeypatch.setenv("BAEPILOT_DATABASE_URL", db_config["database_url"])
    assert main(["demo"]) == 2
    assert json.loads(capsys.readouterr().err)["error"]["code"] == "demo_check_failed"
    with psycopg.connect(db_config["database_url"]) as connection:
        assert (
            connection.execute(
                "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'baepilot_demo_%'"
            ).fetchall()
            == []
        )


def test_database_initialization_and_status(db_config, monkeypatch, capsys):
    from bae_pilot.cli import main

    monkeypatch.setenv("BAEPILOT_DATABASE_URL", db_config["database_url"])
    monkeypatch.setenv("BAEPILOT_DATABASE_SCHEMA", db_config["database_schema"])
    assert main(["init-db"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["controls"][0]["mode"] == "auto"
    with psycopg.connect(db_config["database_url"]) as connection:
        assert connection.execute(
            sql.SQL("SELECT version FROM {}.schema_version").format(
                sql.Identifier(db_config["database_schema"])
            )
        ).fetchone() == (1,)


def test_database_failure_does_not_print_credentials(monkeypatch, capsys):
    from bae_pilot.cli import main

    monkeypatch.setenv(
        "BAEPILOT_DATABASE_URL",
        "postgresql://test:private-password@127.0.0.1:1/db?connect_timeout=1",
    )
    assert main(["init-db"]) == 2
    output = capsys.readouterr()
    assert json.loads(output.err)["error"]["code"] == "database_unavailable"
    assert "private-password" not in output.err + output.out


def test_server_will_not_bind_public_plaintext_interface():
    from bae_pilot.cli import main

    with pytest.raises(SystemExit) as exc:
        main(["serve", "--host", "0.0.0.0"])
    assert exc.value.code == 2


def test_config_validation_error_does_not_print_secret_values(tmp_path, capsys):
    from bae_pilot.cli import main

    config = tmp_path / "invalid.toml"
    config.write_text('provider="invalid"\napi_token="private-token-do-not-print"\n')
    assert main(["--config", str(config), "init-db"]) == 2
    error = capsys.readouterr().err
    assert "validation_error" in error
    assert "private-token" not in error
