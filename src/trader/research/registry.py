"""Append-only experiment registry (risk_and_validation.md §5; roadmap §2).

``research/experiment_registry.jsonl`` holds one canonical-JSON event per line, each linked to
the previous one by SHA-256, so an edited or deleted line is detected on load. Events:

* ``REGISTER`` - a frozen trial: hypothesis, universe and its selection method, horizon, rule
  specification and hash, features, entry/exit timing, cost basis, parameters, split dates,
  metrics, the declared selection correction and the dataset content hashes. Registration
  must precede any run; a run refuses a trial whose current specification no longer matches.
* ``RESULT`` - a completed run of a registered trial (result hash and summary). Failures and
  rejections are recorded exactly like successes.
* ``NOTE`` - informal strategy changes or observations, which also count as research history.
* ``BUDGET_DECISION`` - a recorded decision to change the trial budget (initially ten
  configurations across the project; expanding it needs a stated reason and reviewer).
* ``HOLDOUT_UNLOCK`` - opening a study's locked final test period. Once unlocked, that period is
  development data for the study forever; the registry keeps that fact.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field

from trader.domain.base import ConfigModel
from trader.domain.hashing import canonical_json, sha256_hex
from trader.domain.types import Track, iso_utc

INITIAL_TRIAL_BUDGET = 10
GENESIS = "0" * 64


class RegistryError(RuntimeError):
    pass


class TrialRegistration(ConfigModel):
    trial_id: str = Field(pattern=r"^T-\d{4}$")
    study_id: str
    family: str
    track: Track
    hypothesis: str = Field(min_length=20)
    universe: tuple[str, ...] = Field(min_length=1)
    universe_selection: str
    horizon: str
    rule: dict[str, Any]
    features: tuple[str, ...]
    feature_version: str
    entry_exit_timing: str
    cost_basis: dict[str, Any]
    parameters: dict[str, str]
    splits: dict[str, Any]
    metrics: tuple[str, ...]
    selection_correction: str
    datasets: tuple[dict[str, str], ...]

    @property
    def content_hash(self) -> str:
        return sha256_hex(canonical_json(self.model_dump(mode="json")))


class ExperimentRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.events: list[dict[str, Any]] = []
        if path.exists():
            self._load()

    # ------------------------------------------------------------------ persistence
    def _load(self) -> None:
        prev = GENESIS
        for line_no, line in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not line.strip():
                continue
            event = json.loads(line)
            body = {k: v for k, v in event.items() if k != "record_hash"}
            if body.get("prev_hash") != prev:
                raise RegistryError(f"registry chain broken at line {line_no}")
            if sha256_hex(canonical_json(body)) != event.get("record_hash"):
                raise RegistryError(f"registry record altered at line {line_no}")
            if body.get("seq") != len(self.events) + 1:
                raise RegistryError(f"registry sequence gap at line {line_no}")
            prev = event["record_hash"]
            self.events.append(event)

    def _append(self, kind: str, payload: Mapping[str, Any], now: datetime) -> dict[str, Any]:
        prev = self.events[-1]["record_hash"] if self.events else GENESIS
        body = {
            "seq": len(self.events) + 1,
            "event": kind,
            "at": iso_utc(now),
            "payload": json.loads(canonical_json(dict(payload))),
            "prev_hash": prev,
        }
        record = {**body, "record_hash": sha256_hex(canonical_json(body))}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write(canonical_json(record) + "\n")
        self.events.append(record)
        return record

    # ------------------------------------------------------------------ queries
    def registrations(self) -> dict[str, TrialRegistration]:
        out: dict[str, TrialRegistration] = {}
        for event in self.events:
            if event["event"] == "REGISTER":
                trial = TrialRegistration.model_validate(event["payload"]["trial"])
                out[trial.trial_id] = trial
        return out

    def budget(self) -> int:
        budget = INITIAL_TRIAL_BUDGET
        for event in self.events:
            if event["event"] == "BUDGET_DECISION":
                budget = int(event["payload"]["new_budget"])
        return budget

    def results(self, trial_id: str | None = None) -> list[dict[str, Any]]:
        return [
            e
            for e in self.events
            if e["event"] == "RESULT" and (trial_id is None or e["payload"]["trial_id"] == trial_id)
        ]

    def holdout_unlocked(self, study_id: str) -> bool:
        return any(
            e["event"] == "HOLDOUT_UNLOCK" and e["payload"]["study_id"] == study_id
            for e in self.events
        )

    def require_registered(self, trial: TrialRegistration) -> None:
        existing = self.registrations().get(trial.trial_id)
        if existing is None:
            raise RegistryError(f"{trial.trial_id} is not registered: register before running")
        if existing.content_hash != trial.content_hash:
            raise RegistryError(
                f"{trial.trial_id} differs from its registration (spec, data or cost basis "
                "changed); register a new trial instead"
            )

    # ------------------------------------------------------------------ commands
    def register(self, trial: TrialRegistration, now: datetime) -> bool:
        """Register a trial; returns False if the identical trial is already registered."""
        existing = self.registrations().get(trial.trial_id)
        if existing is not None:
            if existing.content_hash == trial.content_hash:
                return False
            raise RegistryError(f"{trial.trial_id} is already registered with different content")
        count = len(self.registrations())
        if count >= self.budget():
            raise RegistryError(
                f"trial budget exhausted ({count}/{self.budget()}): review the evidence and data "
                "adequacy, then record a BUDGET_DECISION before registering more trials"
            )
        self._append("REGISTER", {"trial": trial.model_dump(mode="json")}, now)
        return True

    def record_result(
        self,
        trial_id: str,
        *,
        run_id: str,
        result_hash: str,
        verdict: str,
        summary: Mapping[str, Any],
        now: datetime,
    ) -> bool:
        if trial_id not in self.registrations():
            raise RegistryError(f"cannot record a result for unregistered {trial_id}")
        for e in self.results(trial_id):
            if e["payload"]["result_hash"] == result_hash and e["payload"]["run_id"] == run_id:
                return False
        self._append(
            "RESULT",
            {
                "trial_id": trial_id,
                "run_id": run_id,
                "result_hash": result_hash,
                "verdict": verdict,
                "summary": dict(summary),
            },
            now,
        )
        return True

    def note(self, text: str, *, by: str, now: datetime) -> None:
        if len(text.strip()) < 10:
            raise RegistryError("a research note needs a meaningful description")
        self._append("NOTE", {"text": text, "by": by}, now)

    def decide_budget(self, new_budget: int, *, reason: str, by: str, now: datetime) -> None:
        if new_budget < len(self.registrations()):
            raise RegistryError("the budget cannot be set below the number of registered trials")
        if len(reason.strip()) < 20:
            raise RegistryError("a budget decision needs a written reason")
        self._append("BUDGET_DECISION", {"new_budget": new_budget, "reason": reason, "by": by}, now)

    def unlock_holdout(self, study_id: str, *, reason: str, by: str, now: datetime) -> None:
        if self.holdout_unlocked(study_id):
            raise RegistryError(f"the holdout of {study_id} was already unlocked")
        if len(reason.strip()) < 20:
            raise RegistryError("unlocking a holdout needs a written reason")
        self._append("HOLDOUT_UNLOCK", {"study_id": study_id, "reason": reason, "by": by}, now)
