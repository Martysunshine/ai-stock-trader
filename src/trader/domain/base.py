"""Base class for immutable, schema-versioned domain objects (architecture.md §3)."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict


class DomainModel(BaseModel):
    """Frozen, strict-shape model. Unknown fields are rejected, never silently dropped."""

    model_config = ConfigDict(
        frozen=True,
        extra="forbid",
        validate_default=True,
        ser_json_inf_nan="strings",
    )

    schema_version: int = 1


class ConfigModel(BaseModel):
    """Configuration shape: frozen and closed (no unknown keys)."""

    model_config = ConfigDict(frozen=True, extra="forbid", validate_default=True)
