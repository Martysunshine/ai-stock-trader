"""The separately guarded live entry point (implementation_roadmap.md §3; risk_and_validation.md §8).

Every check below must pass, in order; the first failure ends the command with a specific
message and exit code. Nothing ever falls back to another mode. In this pass the guard always
refuses: no promotion gate has been run and no live adapter is certified.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import Field, model_validator

from trader.config.host import HostRole, HostRoleError, check_host_role
from trader.config.runtime_config import RuntimeConfig
from trader.domain.base import ConfigModel
from trader.domain.types import DomainError, Mode

#: Live adapters certified for real-money use. Deliberately empty in this pass.
CERTIFIED_LIVE_ADAPTERS: frozenset[str] = frozenset()


class GateStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"


class GateRecord(ConfigModel):
    status: GateStatus
    evidence_refs: tuple[str, ...] = ()
    evaluated_at: str | None = None
    notes: str = ""

    @model_validator(mode="after")
    def _evidence(self) -> GateRecord:
        if self.status is GateStatus.PASSED and (not self.evidence_refs or not self.evaluated_at):
            raise DomainError("a PASSED gate must cite evidence and an evaluation date")
        return self


class OwnerEnablement(ConfigModel):
    enabled: bool = False
    account_alias: str | None = None
    recorded_by: str | None = None
    recorded_at: str | None = None
    protection_policy: str | None = None


class PromotionManifest(ConfigModel):
    """research/promotion_manifest.json (roadmap §2). Never contains secrets."""

    manifest_version: int = Field(ge=1)
    git_commit: str | None = None
    policy_hash: str | None = None
    strategy_versions: tuple[str, ...] = ()
    model_hashes: tuple[str, ...] = ()
    capability_profile: str | None = None
    account_alias: str | None = None
    gates: dict[str, GateRecord]
    owner_enablement: OwnerEnablement = OwnerEnablement()
    outstanding_limitations: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _all_gates(self) -> PromotionManifest:
        required = {"G0", "G1", "G2", "G3", "G4"}
        missing = required - set(self.gates)
        if missing:
            raise DomainError(f"promotion manifest lacks gates {sorted(missing)}")
        return self


class GuardStep(StrEnum):
    ENABLE_FLAG = "1-enable-live-flag"
    HOST_ROLE = "2-host-role"
    CONFIG_MODE = "3-config-mode"
    ACCOUNT_ALLOWLIST = "4-account-allowlist"
    PROMOTION_MANIFEST = "5-promotion-manifest"
    CERTIFIED_ADAPTER = "6-certified-adapter"
    SECRETS = "7-secrets"


EXIT_CODES = {step: 10 + i for i, step in enumerate(GuardStep)}


@dataclass(frozen=True)
class GuardResult:
    allowed: bool
    failed_step: GuardStep | None
    message: str

    @property
    def exit_code(self) -> int:
        return 0 if self.allowed else EXIT_CODES[self.failed_step]  # type: ignore[index]


def check_live_start(
    *,
    enable_live_flag: bool,
    host_role: HostRole,
    runtime: RuntimeConfig,
    policy_hash: str,
    account_alias: str,
    manifest_path: Path,
    secret_present: bool,
) -> GuardResult:
    def fail(step: GuardStep, message: str) -> GuardResult:
        return GuardResult(False, step, message)

    if not enable_live_flag:
        return fail(GuardStep.ENABLE_FLAG, "live start requires the explicit --enable-live flag")
    try:
        check_host_role(host_role, Mode.LIVE)
    except HostRoleError as exc:
        return fail(GuardStep.HOST_ROLE, str(exc))
    if runtime.mode is not Mode.LIVE or not runtime.live_enabled:
        return fail(GuardStep.CONFIG_MODE, "configuration is not an enabled live configuration")
    account = next((a for a in runtime.accounts if a.alias == account_alias), None)
    if account is None or account.account_id not in runtime.live_account_allowlist:
        return fail(
            GuardStep.ACCOUNT_ALLOWLIST, f"account alias {account_alias!r} is not allowlisted"
        )
    if not manifest_path.exists():
        return fail(GuardStep.PROMOTION_MANIFEST, f"promotion manifest {manifest_path} not found")
    try:
        manifest = PromotionManifest.model_validate_json(manifest_path.read_text("utf-8"))
    except ValueError as exc:
        return fail(GuardStep.PROMOTION_MANIFEST, f"invalid promotion manifest: {exc}")
    not_passed = sorted(
        g for g, rec in manifest.gates.items() if rec.status is not GateStatus.PASSED
    )
    if not_passed:
        return fail(GuardStep.PROMOTION_MANIFEST, f"promotion gates not passed: {not_passed}")
    if manifest.policy_hash != policy_hash:
        return fail(
            GuardStep.PROMOTION_MANIFEST, "manifest policy hash differs from the loaded policy"
        )
    owner = manifest.owner_enablement
    if not owner.enabled or owner.account_alias != account_alias or not owner.recorded_by:
        return fail(GuardStep.PROMOTION_MANIFEST, "no recorded owner enablement for this account")
    if account.broker not in CERTIFIED_LIVE_ADAPTERS:
        return fail(
            GuardStep.CERTIFIED_ADAPTER,
            f"broker adapter {account.broker!r} is not certified for live trading with native protection",
        )
    if not secret_present:
        return fail(GuardStep.SECRETS, "live credentials are not present in the OS secret store")
    return GuardResult(True, None, "all live start checks passed")
