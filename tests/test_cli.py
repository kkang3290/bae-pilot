import json

import pytest


def test_demo_ignores_external_model_env_and_leaves_no_database(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("BAEPILOT_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")
    from bae_pilot.cli import main

    assert main(["demo"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "offline"
    assert set(result["checks"]) == {
        "automatic_reply",
        "complex_path",
        "duplicate_event",
        "human_handoff",
        "unknown_delivery",
    }
    assert all(result["checks"].values())
    assert not list(tmp_path.iterdir())


def test_database_initialization_and_offline_status(tmp_path, monkeypatch, capsys):
    from bae_pilot.cli import main

    monkeypatch.setenv("BAEPILOT_DB_PATH", str(tmp_path / "cli.sqlite"))
    assert main(["init-db"]) == 0
    capsys.readouterr()
    assert main(["status"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["controls"][0]["mode"] == "auto"


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
