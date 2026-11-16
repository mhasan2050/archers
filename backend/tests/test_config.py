import pytest
from pydantic import ValidationError

from archers.config import Settings


def valid_config() -> dict[str, object]:
    return {
        "database_url": "postgresql+psycopg://unused",
        "operator_token": "o" * 48,
        "signing_key": "k" * 48,
    }


def test_secrets_redacted_and_external_modes_rejected() -> None:
    settings = Settings.model_validate(valid_config())
    assert "o" * 48 not in repr(settings)
    with pytest.raises(ValidationError):
        Settings.model_validate({**valid_config(), "network_mode": "external_models"})
    with pytest.raises(ValidationError):
        Settings.model_validate({**valid_config(), "arbitrary_plugin": "execute"})
    with pytest.raises(ValidationError):
        Settings.model_validate({**valid_config(), "database_url": "sqlite:///production.db"})


def test_nonloopback_http_denied() -> None:
    with pytest.raises(ValidationError):
        Settings.model_validate({**valid_config(), "origins": ["http://192.168.1.20:8080"]})
    Settings.model_validate(
        {**valid_config(), "origins": ["https://rag.example"], "secure_cookie": True}
    )
