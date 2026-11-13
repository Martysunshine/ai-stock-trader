"""Mode-specific runtime configuration (instructions.md §6.1).

Defaults to replay. A configuration file contains no secrets: database URLs and broker keys are
referenced by environment-variable *name* or OS keyring entry, never stored inline.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from trader.config.host import HostRole, check_host_role, current_host_role
from trader.config.policy import LoadedPolicy, load_policy, load_yaml_strict
from trader.config.track_profiles import TrackProfile, load_track_profiles
from trader.domain.base import ConfigModel
from trader.domain.types import DomainError, Mode, Track

_SECRETISH = ("secret", "password", "passwd", "api_key", "apikey", "token", "private_key")


class DatabaseConfig(ConfigModel):
    backend: Literal["sqlite", "postgresql"]
    path: str | None = None
    url_env: str | None = None

    @model_validator(mode="after")
    def _shape(self) -> DatabaseConfig:
        if self.backend == "sqlite" and not self.path:
            raise DomainError("sqlite database needs a path")
        if self.backend == "postgresql" and not self.url_env:
            raise DomainError("postgresql database needs url_env (the URL is never stored inline)")
        return self

    def url(self, base_dir: Path) -> str:
        if self.backend == "sqlite":
            assert self.path is not None
            p = Path(self.path)
            if not p.is_absolute():
                p = base_dir / p
            return f"sqlite:///{p.as_posix()}"
        assert self.url_env is not None
        value = os.environ.get(self.url_env)
        if not value:
            raise DomainError(f"environment variable {self.url_env} is not set")
        return value


class AccountConfig(ConfigModel):
    alias: str = Field(pattern=r"^[a-z0-9-]+$")
    account_id: str = Field(pattern=r"^[a-z0-9-]+$")
    track: Track
    broker: str
    capability_profile: str
    fee_schedule: str
    instruments: str
    secret_ref: str | None = Field(
        default=None, description="OS keyring service name for credentials; never the key itself"
    )
    accrue_modelled_fees: bool = Field(
        default=False,
        description="Paper only (ADR-0050): book the fee schedule's charge on each fill the "
        "broker reports without one, as a pending accrual. The broker books its fees only the "
        "following night (ADR-0054), so without this a session's result leaves out its fees.",
    )


class RuntimeConfig(ConfigModel):
    config_version: Literal[1]
    mode: Mode
    live_enabled: bool = False
    policy_file: str
    tracks_dir: str
    portfolio_id: str = Field(pattern=r"^[a-z0-9-]+$")
    accounts: tuple[AccountConfig, ...] = Field(min_length=1)
    database: DatabaseConfig
    live_account_allowlist: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _rules(self) -> RuntimeConfig:
        if self.live_enabled and self.mode is not Mode.LIVE:
            raise DomainError("live_enabled is only permitted in a live configuration")
        if self.mode is Mode.LIVE and not self.live_account_allowlist:
            raise DomainError("a live configuration needs an explicit account allowlist")
        if self.mode is not Mode.LIVE and self.live_account_allowlist:
            raise DomainError("only live configurations may carry a live account allowlist")
        if self.database.backend == "sqlite" and self.mode is not Mode.REPLAY:
            raise DomainError("SQLite is permitted for replay only (ADR-0001); use PostgreSQL")
        policy_name = self.policy_file.replace("\\", "/").rsplit("/", 1)[-1]
        if policy_name.startswith("paper_") and self.mode is not Mode.PAPER:
            raise DomainError(
                f"{self.policy_file} is a paper practice policy (ADR-0043); "
                f"{self.mode.value} must use the shared policy"
            )
        if self.mode is Mode.LIVE and policy_name != "base.yaml":
            raise DomainError("a live configuration must use the shared policy policy/base.yaml")
        aliases = [a.alias for a in self.accounts]
        if len(set(aliases)) != len(aliases):
            raise DomainError("duplicate account alias")
        for account in self.accounts:
            if account.accrue_modelled_fees and self.mode is not Mode.PAPER:
                raise DomainError(
                    f"account {account.alias}: accrue_modelled_fees is a paper setting "
                    f"(ADR-0050), not permitted in {self.mode.value}"
                )
            if self.mode is not Mode.LIVE and "live" in account.broker:
                raise DomainError(f"{self.mode.value} configuration references a live broker")
            if self.mode is Mode.LIVE and account.account_id not in self.live_account_allowlist:
                raise DomainError(f"account {account.account_id} is not allowlisted for live")
        return self

    def account(self, alias: str) -> AccountConfig:
        for account in self.accounts:
            if account.alias == alias:
                return account
        raise DomainError(f"unknown account alias {alias!r}")


@dataclass(frozen=True, slots=True)
class ResolvedConfig:
    path: Path
    config_dir: Path
    runtime: RuntimeConfig
    policy: LoadedPolicy
    tracks: dict[Track, TrackProfile]
    host_role: HostRole

    def resolve(self, relative: str) -> Path:
        return self.config_dir / relative


def _scan_for_secrets(node: object, path: str = "") -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            low = str(key).lower()
            if any(word in low for word in _SECRETISH) and key != "secret_ref":
                raise DomainError(f"configuration must not contain secrets ({path}.{key})")
            _scan_for_secrets(value, f"{path}.{key}")
    elif isinstance(node, list):
        for item in node:
            _scan_for_secrets(item, path)


def load_config(path: Path, *, host_role: HostRole | None = None) -> ResolvedConfig:
    raw = load_yaml_strict(path)
    _scan_for_secrets(raw)
    runtime = RuntimeConfig.model_validate(raw)
    role = current_host_role() if host_role is None else host_role
    check_host_role(role, runtime.mode)
    config_dir = path.parent
    policy = load_policy(
        config_dir / runtime.policy_file, mode=runtime.mode, live_enabled=runtime.live_enabled
    )
    tracks = load_track_profiles(config_dir / runtime.tracks_dir)
    for account in runtime.accounts:
        if not tracks[account.track].enabled:
            raise DomainError(f"account {account.alias} uses disabled track {account.track}")
    return ResolvedConfig(
        path=path,
        config_dir=config_dir,
        runtime=runtime,
        policy=policy,
        tracks=tracks,
        host_role=role,
    )
