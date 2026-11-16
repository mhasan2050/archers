from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator

from archers.domain import Role


class StrictDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginRequest(StrictDTO):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=1, max_length=1024)


class CreateUser(StrictDTO):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr = Field(min_length=15, max_length=1024)
    role: Role


class UpdateUser(StrictDTO):
    role: Role | None = None
    disabled: bool | None = Field(default=None, strict=True)
    password: SecretStr | None = Field(default=None, min_length=15, max_length=1024)

    @model_validator(mode="after")
    def not_empty(self) -> "UpdateUser":
        if not self.model_fields_set or any(
            getattr(self, f) is None for f in self.model_fields_set
        ):
            raise ValueError("Update requires non-null fields.")
        return self


class UserDTO(StrictDTO):
    id: UUID
    username: str
    role: Role
    disabled: bool
    revision: int = Field(ge=1)


class SessionInfo(StrictDTO):
    user: UserDTO
    permissions: list[str]
    csrf_token: str
    expires_at: datetime


class UserPage(StrictDTO):
    items: list[UserDTO]
    next_cursor: str | None


Format = Literal["pdf", "docx", "pptx", "xlsx", "markdown", "text", "html", "csv"]


class ExportPolicy(StrictDTO):
    allow_external_generation: bool = Field(default=False, strict=True)
    allow_external_embeddings: bool = Field(default=False, strict=True)


class ScanPolicy(StrictDTO):
    poll_seconds: int = Field(ge=1, strict=True)
    stable_seconds: int = Field(ge=1, strict=True)
    deletion_grace_seconds: int = Field(ge=1, strict=True)
    completion_mode: Literal[
        "atomic_rename", "ready_manifest", "heuristic_double_hash", "remote_revision"
    ]


class FilesystemConfig(StrictDTO):
    kind: Literal["filesystem"]
    mount_alias: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    sentinel_id: UUID


class S3Config(StrictDTO):
    kind: Literal["s3"]
    endpoint_alias: str = Field(min_length=1)
    bucket: str = Field(min_length=1)
    prefix: str
    credential_ref: str = Field(min_length=1)


class GraphConfig(StrictDTO):
    kind: Literal["sharepoint"]
    directory_tenant_id: str = Field(min_length=1)
    site_or_drive_id: str = Field(min_length=1)
    folder_id: str = Field(min_length=1)
    credential_ref: str = Field(min_length=1)


class DriveConfig(StrictDTO):
    kind: Literal["google_drive"]
    drive_id: str = Field(min_length=1)
    folder_id: str = Field(min_length=1)
    credential_ref: str = Field(min_length=1)


Connector = FilesystemConfig | S3Config | GraphConfig | DriveConfig


class CreateSource(StrictDTO):
    connector: Connector = Field(discriminator="kind")
    formats: list[Format] = Field(min_length=1)
    enabled: bool = Field(strict=True)
    scan_policy: ScanPolicy
    export_policy: ExportPolicy = Field(default_factory=ExportPolicy)

    @model_validator(mode="after")
    def unique_formats(self) -> "CreateSource":
        if len(set(self.formats)) != len(self.formats):
            raise ValueError("Formats must be unique.")
        return self


class UpdateSource(StrictDTO):
    enabled: bool | None = Field(default=None, strict=True)
    scan_policy: ScanPolicy | None = None
    formats: list[Format] | None = Field(default=None, min_length=1)
    export_policy: ExportPolicy | None = None

    @model_validator(mode="after")
    def nonempty_unique(self) -> "UpdateSource":
        if not self.model_fields_set or any(
            getattr(self, f) is None for f in self.model_fields_set
        ):
            raise ValueError("Update requires non-null fields.")
        if self.formats and len(set(self.formats)) != len(self.formats):
            raise ValueError("Formats must be unique.")
        return self


class SourceDTO(CreateSource):
    id: UUID
    revision: int = Field(ge=1)


class SourcePage(StrictDTO):
    items: list[SourceDTO]
    next_cursor: str | None


class Health(StrictDTO):
    status: Literal["ok"] = "ok"
