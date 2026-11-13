"""SYNTHETIC fixtures for the model-study tests (no real market data is ever evaluated).

A seeded random-walk 15-minute history for ``COINBASE:BTC/USD`` is written through the normal
history store into a temporary workspace, with a manifest entry of its own, a base study shrunk
to one walk-forward fold (one month each of training, validation and test) and a model study
that references it. The data carries no information about any real instrument.
"""

from __future__ import annotations

import shutil
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import numpy as np
import yaml

from trader.data.history import HistoryBar
from trader.data.ingest import store_instrument
from trader.data.manifest import DataManifest, HistoryRequest, InstrumentRequest, load_manifest
from trader.data.quality import expected_starts_24_7
from trader.research.study import load_study

REPO = Path(__file__).resolve().parents[1]
START = datetime(2026, 1, 1, tzinfo=UTC)
HOLDOUT = datetime(2026, 4, 1, tzinfo=UTC)
END = datetime(2026, 5, 1, tzinfo=UTC)  # one month of holdout bars exists and must stay unread
DATASET = "synthetic_crypto_15m_test"
BASE_STUDY = "configs/research/synthetic_crypto_base.yaml"
MODEL_STUDY = "configs/research/synthetic_crypto_filter.yaml"
NOW = datetime(2026, 11, 8, 12, tzinfo=UTC)


def random_walk(start: datetime, end: datetime, seed: int, price: float) -> list[HistoryBar]:
    rng = np.random.default_rng(seed)
    out: list[HistoryBar] = []
    for ts in expected_starts_24_7(start, end, 15):
        o = price
        c = o * (1 + rng.normal(0.0, 0.004))
        h = max(o, c) * (1 + abs(rng.normal(0.0, 0.002)))
        low = min(o, c) * (1 - abs(rng.normal(0.0, 0.002)))
        out.append(
            HistoryBar(
                start=ts,
                minutes=15,
                open=Decimal(f"{o:.2f}"),
                high=Decimal(f"{h:.2f}"),
                low=Decimal(f"{low:.2f}"),
                close=Decimal(f"{c:.2f}"),
                volume=Decimal(f"{rng.uniform(5, 80):.4f}"),
            )
        )
        price = float(Decimal(f"{c:.2f}"))
    return out


def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(REPO / "configs", root / "configs")
    shutil.copytree(
        REPO / "src" / "trader" / "strategies" / "specs",
        root / "src" / "trader" / "strategies" / "specs",
    )
    return root


def scramble_after(bars: list[HistoryBar], cutoff: datetime, seed: int) -> list[HistoryBar]:
    """Replace every bar from ``cutoff`` on with an unrelated random walk (leakage probes)."""
    keep = [b for b in bars if b.start < cutoff]
    other = random_walk(cutoff, END, seed=seed, price=float(keep[-1].close) * 1.07)
    return keep + other


def synthetic_manifest(root: Path, *, scramble_from: datetime | None = None) -> DataManifest:
    """The committed manifest plus the synthetic dataset, written under ``root``.

    ``scramble_from`` replaces every bar from that time on with a different random walk.
    """
    bars = random_walk(START, END, seed=20261108, price=60000.0)
    if scramble_from is not None:
        bars = scramble_after(bars, scramble_from, seed=99)
    manifest = load_manifest(REPO / "research" / "data_manifest.json")
    template = manifest.dataset("coinbase_btc_eth_usd_15m_v1")
    request = HistoryRequest(
        instruments=(InstrumentRequest(instrument_id="COINBASE:BTC/USD", source_symbol="SYN"),),
        bar_minutes=15,
        start=START,
        end=END,
        calendar="UTC_24_7",
    )
    ds = template.model_copy(
        update={
            "name": DATASET,
            "kind": "SYNTHETIC_BARS",
            "source": "synthetic_history",
            "venue": "SYNTHETIC",
            "feed": "synthetic",
            "request": request,
            "path": f"data/history/{DATASET}",
            "notes": "SYNTHETIC random walk for tests; carries no market information",
        }
    )
    record = store_instrument(
        ds,
        root,
        request.instruments[0],
        bars,
        retrieved_at=END + timedelta(days=1),
        expected=expected_starts_24_7(START, END, 15),
        sessions=None,
        gap_semantics="SYNTHETIC",
    )
    ds = ds.model_copy(
        update={"status": "ACQUIRED", "retrieved_at": END + timedelta(days=1), "files": (record,)}
    )
    return manifest.model_copy(update={"history": (*manifest.history, ds)})


def write_base_study(root: Path) -> None:
    """``crypto_baseline_v1`` with only the data, universe and split dates moved (SYNTHETIC)."""
    raw: dict[str, Any] = load_study(
        REPO / "configs" / "research" / "crypto_baseline_v1.yaml"
    ).model_dump(mode="json")
    raw.update(
        {
            "study_id": "synthetic_crypto_base",
            "price_dataset": DATASET,
            "basis_datasets": [],
            "instruments": [
                {
                    "price_instrument_id": "COINBASE:BTC/USD",
                    "execution_instrument_id": "KRAKEN:BTC/USD",
                }
            ],
            "splits": {
                "development_start": "2026-01-01T00:00:00Z",
                "holdout_start": "2026-04-01T00:00:00Z",
                "holdout_end": "2026-05-01T00:00:00Z",
                "walk_forward": {
                    "train_months": 1,
                    "validation_months": 1,
                    "test_months": 1,
                    "step_months": 1,
                },
                "embargo_minutes": 360,
            },
            "bootstrap": {"replicates": 200, "mean_block_sessions": "5", "seed": 20261107},
        }
    )
    _write_yaml(root / BASE_STUDY, raw)


def model_study_raw(**trial_updates: Any) -> dict[str, Any]:
    """Two trials: a trainable logistic filter on Candidate A and a Candidate B filter whose
    only threshold (0.99) can never collect enough validation accepts (an untrainable fold)."""
    common = {
        "model_type": "logistic",
        "grid": [{"C": "0.1", "class_weight": "none"}, {"C": "1.0", "class_weight": "balanced"}],
        "calibration": "platt",
        "min_accepts": 5,
        "controls": {"random_seeds": 2, "turnover_matched": True},
        "seed": 7,
    }
    trial_a = {
        "trial_id": "T-0011",
        "family": "CANDIDATE_A",
        "spec_file": "src/trader/strategies/specs/candidate_a_crypto_v1.yaml",
        **common,
        "threshold_grid": ["0.05", "0.10", "0.15", "0.20", "0.25", "0.30"],
        "hypothesis": "synthetic pipeline check: a logistic filter on Candidate A candidates",
        **trial_updates,
    }
    trial_b = {
        "trial_id": "T-0012",
        "family": "CANDIDATE_B",
        "spec_file": "src/trader/strategies/specs/candidate_b_crypto_v1.yaml",
        **common,
        "threshold_grid": ["0.99"],
        "hypothesis": "synthetic pipeline check: a filter that can never be thresholded",
    }
    return {
        "kind": "model_study",
        "study_id": "synthetic_crypto_filter",
        "title": "SYNTHETIC model study (pipeline check, not evidence)",
        "description": "Seeded random walk; checks the M3 protocol end to end.",
        "base_study": BASE_STUDY,
        "trials": [trial_a, trial_b],
    }


def write_model_study(root: Path, raw: dict[str, Any] | None = None) -> Path:
    path = root / MODEL_STUDY
    _write_yaml(path, raw if raw is not None else model_study_raw())
    return path


def _write_yaml(path: Path, raw: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8", newline="\n")
