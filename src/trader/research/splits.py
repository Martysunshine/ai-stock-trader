"""Chronological walk-forward splits and the locked final test (risk_and_validation.md §5).

Default schedule: twelve months train, three months validation, three months test, rolling
forward by three months inside the *development* period. The final test period after it stays
locked: research code refuses to load its bars unless the registry records an explicit unlock,
after which the period counts as development data for good.

Boundaries are calendar times, so every instrument's bars at the same time fall into the same
split. Labels overlap boundaries when a trade is still open: when a validation window is used
for selection, trades whose exit falls after the window end are purged, and an embargo equal to
the maximum holding horizon separates a validation window from the next test window.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from pydantic import Field, model_validator

from trader.domain.base import ConfigModel
from trader.domain.types import UtcDatetime, iso_utc


class WalkForward(ConfigModel):
    train_months: int = Field(default=12, ge=1)
    validation_months: int = Field(default=3, ge=1)
    test_months: int = Field(default=3, ge=1)
    step_months: int = Field(default=3, ge=1)


class SplitPlan(ConfigModel):
    development_start: UtcDatetime
    holdout_start: UtcDatetime
    holdout_end: UtcDatetime
    walk_forward: WalkForward = WalkForward()
    embargo_minutes: int = Field(ge=0)

    @model_validator(mode="after")
    def _order(self) -> SplitPlan:
        if not self.development_start < self.holdout_start < self.holdout_end:
            raise ValueError("development must precede the holdout")
        return self

    def describe(self) -> dict[str, str]:
        wf = self.walk_forward
        return {
            "development": f"{iso_utc(self.development_start)} .. {iso_utc(self.holdout_start)}",
            "locked_final_test": f"{iso_utc(self.holdout_start)} .. {iso_utc(self.holdout_end)}",
            "walk_forward": (
                f"train {wf.train_months}m / validation {wf.validation_months}m / test "
                f"{wf.test_months}m, step {wf.step_months}m"
            ),
            "embargo_minutes": str(self.embargo_minutes),
        }


@dataclass(frozen=True, slots=True)
class Fold:
    index: int
    train: tuple[datetime, datetime]
    validation: tuple[datetime, datetime]
    test: tuple[datetime, datetime]


def add_months(ts: datetime, months: int) -> datetime:
    month = ts.month - 1 + months
    year = ts.year + month // 12
    return ts.replace(year=year, month=month % 12 + 1, day=1)


def folds(plan: SplitPlan) -> list[Fold]:
    wf = plan.walk_forward
    start = plan.development_start.astimezone(UTC)
    if start.day != 1 or start.hour or start.minute:
        raise ValueError("walk-forward folds start on a month boundary")
    embargo = timedelta(minutes=plan.embargo_minutes)
    out: list[Fold] = []
    index = 0
    while True:
        train_end = add_months(start, wf.train_months)
        val_end = add_months(train_end, wf.validation_months)
        test_end = add_months(val_end, wf.test_months)
        if test_end > plan.holdout_start:
            return out
        index += 1
        out.append(
            Fold(
                index=index,
                train=(start, train_end),
                validation=(train_end, val_end - embargo),
                test=(val_end, test_end),
            )
        )
        start = add_months(start, wf.step_months)


class HoldoutLockedError(RuntimeError):
    pass


def guard_window(plan: SplitPlan, start: datetime, end: datetime, *, unlocked: bool) -> None:
    """Refuse a data window that overlaps the locked final test period."""
    if unlocked:
        return
    if start < plan.holdout_end and end > plan.holdout_start:
        raise HoldoutLockedError(
            f"window {iso_utc(start)}..{iso_utc(end)} overlaps the locked final test "
            f"{iso_utc(plan.holdout_start)}..{iso_utc(plan.holdout_end)}"
        )
