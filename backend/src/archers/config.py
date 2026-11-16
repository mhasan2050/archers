import os
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)

    database_url: SecretStr
    operator_token: SecretStr
    signing_key: SecretStr
    network_mode: Literal["airgap"] = "airgap"
    origins: list[str] = Field(default_factory=lambda: ["http://localhost:8080"])
    secure_cookie: bool = False
    session_hours: int = Field(default=8, ge=1, le=24)
    approved_mounts: dict[str, str] = Field(default_factory=dict)
    network_mounts: list[str] = Field(default_factory=list)
    mount_health_files: dict[str, str] = Field(default_factory=dict)
    model_path: Path = Path("/models/embedding")
    qdrant_url: str = "http://qdrant:6333"
    scan_max_entries: int = Field(default=10000, ge=1, le=100000)
    scan_max_bytes: int = Field(default=256 * 1024 * 1024, ge=1024, le=1024 * 1024 * 1024)
    parser_timeout_seconds: int = Field(default=90, ge=1, le=300)
    max_json_bytes: int = Field(default=65536, ge=1024, le=1048576)
    artifact_root: Path = Path("/data")
    max_upload_bytes: int = Field(default=25 * 1024 * 1024, ge=1, le=100 * 1024 * 1024)
    job_lease_seconds: int = Field(default=120, ge=60, le=600)
    max_pending_ingestion_jobs: int = Field(default=100, ge=1, le=10000)

    @model_validator(mode="after")
    def check_security(self) -> Self:
        if not self.database_url.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("Production persistence requires PostgreSQL with psycopg.")
        if not self.artifact_root.is_absolute():
            raise ValueError("Artifact root must be an absolute private path.")
        if len(self.operator_token.get_secret_value()) < 32:
            raise ValueError("Operator token must contain at least 32 characters.")
        if len(self.signing_key.get_secret_value()) < 32:
            raise ValueError("Signing key must contain at least 32 characters.")
        if self.signing_key == self.operator_token:
            raise ValueError("Operator token and signing key must be independent.")
        if not self.origins:
            raise ValueError("At least one trusted origin is required.")
        for origin in self.origins:
            parsed = urlsplit(origin)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username
                or parsed.password
                or parsed.path
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("Origins must be exact HTTP(S) origins without paths.")
            if parsed.scheme == "http" and (
                parsed.hostname not in {"localhost", "127.0.0.1", "::1"} or self.secure_cookie
            ):
                raise ValueError("Insecure cookies are allowed only for loopback HTTP.")
            if parsed.scheme == "https" and not self.secure_cookie:
                raise ValueError("HTTPS requires Secure cookies.")
        for alias, mount in self.approved_mounts.items():
            if not alias or not all(c.isascii() and (c.isalnum() or c in "_-") for c in alias):
                raise ValueError("Mount aliases must be alphanumeric identifiers.")
            if not Path(mount).is_absolute():
                raise ValueError("Approved mounts must be absolute operator paths.")
        if not self.model_path.is_absolute():
            raise ValueError("Model path must be absolute.")
        qdrant = urlsplit(self.qdrant_url)
        if (
            qdrant.scheme not in {"http", "https"} or not qdrant.hostname
            or qdrant.username or qdrant.password or qdrant.query or qdrant.fragment
            or qdrant.path not in {"", "/"}
        ):
            raise ValueError("Qdrant requires an operator-provisioned HTTP(S) origin.")
        if not set(self.network_mounts).issubset(self.approved_mounts):
            raise ValueError("Network mounts must reference approved aliases.")
        if not set(self.network_mounts).issubset(self.mount_health_files):
            raise ValueError("Each network mount requires a host health record.")
        if any(not Path(p).is_absolute() for p in self.mount_health_files.values()):
            raise ValueError("Mount health files must be absolute operator paths.")
        return self

    @property
    def hosts(self) -> set[str]:
        return {str(urlsplit(origin).hostname) for origin in self.origins}


def load_settings() -> Settings:
    config_file = os.environ.get("ARCHERS_CONFIG_FILE")
    values: dict[str, object] = {}
    if config_file:
        with Path(config_file).open() as file:
            parsed = yaml.safe_load(file)
        if not isinstance(parsed, dict) or not all(isinstance(key, str) for key in parsed):
            raise ValueError("Configuration must be a YAML mapping.")
        values = parsed
    for name in ("database_url", "operator_token", "signing_key"):
        secret_file = os.environ.get(f"ARCHERS_{name.upper()}_FILE")
        if secret_file:
            values[name] = Path(secret_file).read_text().strip()
    return Settings.model_validate(values)
