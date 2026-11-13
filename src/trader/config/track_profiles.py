"""Track profiles (ADR-0002).

The canonical policy's ``instruments`` block (ordinary equities, long only) describes the
original STOCKS track. The crypto and commodity addenda allow "explicitly named new profiles"
that may differ only in product type, direction, calendar and cadence. A profile therefore has
**no numeric risk fields** (the schema is closed), and the STOCKS profile can never widen
beyond long-only ordinary equities.
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator

from trader.config.policy import load_yaml_strict
from trader.domain.base import ConfigModel
from trader.domain.types import Direction, DomainError, ProductType, Track


class TrackProfile(ConfigModel):
    track: Track
    enabled: bool
    description: str
    allowed_product_types: tuple[ProductType, ...] = Field(min_length=1)
    replay_directions: tuple[Direction, ...] = Field(min_length=1)
    live_directions: tuple[Direction, ...] = ()
    calendar: Literal["XNYS", "UTC_24_7", "BROKER_CFD"]
    decision_bar_minutes: int = Field(gt=0)
    context_bar_minutes: int = Field(gt=0)
    loss_windows: tuple[Literal["NY_SESSION", "NY_WEEK", "UTC_DAY", "UTC_WEEK"], ...]

    @model_validator(mode="after")
    def _rules(self) -> TrackProfile:
        if self.context_bar_minutes % self.decision_bar_minutes:
            raise DomainError("context bars must be a whole multiple of decision bars")
        if not set(self.live_directions) <= set(self.replay_directions):
            raise DomainError("live directions must be a subset of researched directions")
        if self.track is Track.STOCKS:
            if self.allowed_product_types != (ProductType.ORDINARY_EQUITY,):
                raise DomainError("STOCKS profile is ordinary equities only")
            if self.replay_directions != (Direction.LONG,) or self.live_directions not in (
                (),
                (Direction.LONG,),
            ):
                raise DomainError("STOCKS profile is long only in every mode")
            if (self.decision_bar_minutes, self.context_bar_minutes) != (5, 15):
                raise DomainError("STOCKS cadence is 5-minute decisions with 15-minute context")
            if self.calendar != "XNYS":
                raise DomainError("STOCKS v1 uses the NYSE calendar")
        if self.track is Track.CRYPTO:
            if self.allowed_product_types != (ProductType.CRYPTO_SPOT,):
                raise DomainError("crypto track starts with spot only (derivatives are later)")
            if Direction.SHORT in self.replay_directions:
                raise DomainError("spot crypto is fully paid and long only")
        if self.track is Track.COMMODITIES and Direction.SHORT in self.live_directions:
            raise DomainError("short CFD exposure is research/demo only until certified")
        return self


def load_track_profiles(directory: Path) -> dict[Track, TrackProfile]:
    profiles: dict[Track, TrackProfile] = {}
    for path in sorted(directory.glob("*.yaml")):
        profile = TrackProfile.model_validate(load_yaml_strict(path))
        if profile.track in profiles:
            raise DomainError(f"duplicate profile for {profile.track}")
        profiles[profile.track] = profile
    missing = set(Track) - set(profiles)
    if missing:
        raise DomainError(f"missing track profiles: {sorted(missing)}")
    return profiles
