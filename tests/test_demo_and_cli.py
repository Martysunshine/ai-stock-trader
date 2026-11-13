"""End-to-end demonstrator, CLI guards, synthetic data determinism and generated docs."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.conftest import pg_url
from tests.test_decision_loop import PINNED_REPLAY_HASHES
from trader.cli.main import app
from trader.config.host import HostRole
from trader.config.runtime_config import load_config
from trader.data.recorded_session import load_session
from trader.data.synthetic import write_session
from trader.reporting.docs_build import build_docs
from trader.runtime.demo import run_demo
from trader.runtime.live_guard import GuardStep, PromotionManifest
from trader.runtime.scenarios import run_scenario

ROOT = Path(__file__).resolve().parents[1]
runner = CliRunner()


@pytest.mark.slow
def test_first_demonstrator_passes_every_acceptance_check(tmp_path: Path) -> None:
    summary = run_demo(ROOT, tmp_path / "demo")
    failed = [f"{c.name}: {c.detail}" for c in summary.checks if not c.passed]
    assert failed == []
    assert len(summary.checks) == 14  # S1 x7, S2, S3 x2, S4, S5 x3 (a 15th with --postgres)
    hashes = {
        name: (r.report.decision_hash, r.report.ledger_hash)
        for name, r in summary.results.items()
        if name in PINNED_REPLAY_HASHES
    }
    assert hashes == PINNED_REPLAY_HASHES  # replay behaviour is unchanged by the shared loop
    assert (tmp_path / "demo" / "summary.md").exists()
    report = (tmp_path / "demo" / "baseline" / "report.md").read_text(encoding="utf-8")
    assert "NOT evidence of profitability" in report


@pytest.mark.pg
@pytest.mark.slow
def test_postgres_replay_reproduces_sqlite_hashes(tmp_path: Path) -> None:
    lite = run_scenario("baseline", repo_root=ROOT, run_dir=tmp_path / "lite")
    schema = f"t_repro_{os.getpid()}"
    pg = run_scenario(
        "baseline", repo_root=ROOT, run_dir=tmp_path / "pg", db_url=pg_url(), db_schema=schema
    )
    from trader.storage.db import Database

    Database(pg_url(), schema=schema).drop_postgres_schema()
    assert pg.report.decision_hash == lite.report.decision_hash
    assert pg.report.ledger_hash == lite.report.ledger_hash


def test_synthetic_sessions_are_reproducible_and_tamper_evident(tmp_path: Path) -> None:
    for variant in ("base", "gap"):
        committed = load_session(ROOT / "research" / "sessions" / f"synthetic_{variant}_v1")
        regenerated = write_session(tmp_path / variant, variant)
        assert regenerated.events_sha256 == committed.manifest.events_sha256
    tampered = tmp_path / "tampered"
    shutil.copytree(ROOT / "research" / "sessions" / "synthetic_base_v1", tampered)
    events = tampered / "events.jsonl"
    events.write_bytes(events.read_bytes().replace(b'"101.2', b'"101.3', 1))
    with pytest.raises(ValueError, match="checksum"):
        load_session(tampered)


def test_generated_documentation_is_current() -> None:
    assert build_docs(ROOT, check=True) == []


def test_config_check_cli_succeeds_for_replay() -> None:
    result = runner.invoke(app, ["config-check", "--config", str(ROOT / "configs" / "replay.yaml")])
    assert result.exit_code == 0, result.output
    assert "policy hash" in result.output
    assert "planned_risk_per_trade" in result.output


def test_paper_command_refuses_without_prerequisites_and_never_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_env = load_config(
        ROOT / "configs" / "paper.yaml", host_role=HostRole.DEV
    ).runtime.database.url_env
    monkeypatch.delenv(db_env, raising=False)
    monkeypatch.setenv("TRADER_HOST_ROLE", "DEV")
    result = runner.invoke(app, ["paper", "run", "--config", str(ROOT / "configs" / "paper.yaml")])
    assert result.exit_code == 2 and db_env in result.output
    replay = runner.invoke(app, ["paper", "run", "--config", str(ROOT / "configs" / "replay.yaml")])
    assert replay.exit_code == 2 and "no fallback" in replay.output


def test_live_command_refuses_at_each_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    live_cfg = str(ROOT / "configs" / "live.example.yaml")
    no_flag = runner.invoke(app, ["live", "--config", live_cfg])
    assert "1-enable-live-flag" in no_flag.output
    monkeypatch.setenv("TRADER_HOST_ROLE", "DEV")
    wrong_host = runner.invoke(app, ["live", "--config", live_cfg, "--enable-live"])
    assert "2-host-role" in wrong_host.output
    monkeypatch.setenv("TRADER_HOST_ROLE", "LIVE")
    no_manifest = runner.invoke(app, ["live", "--config", live_cfg, "--enable-live"])
    assert GuardStep.PROMOTION_MANIFEST.value in no_manifest.output
    assert no_manifest.exit_code != 0


def test_promotion_manifest_never_prepopulates_passed_gates() -> None:
    manifest = PromotionManifest.model_validate_json(
        (ROOT / "research" / "promotion_manifest.json").read_text(encoding="utf-8")
    )
    assert all(g.status.value != "PASSED" for g in manifest.gates.values())
    assert manifest.gates["G2"].status.value == "FAILED" and manifest.gates["G2"].evidence_refs
    with pytest.raises(ValueError, match="evidence"):
        PromotionManifest.model_validate(
            {
                **json.loads(manifest.model_dump_json()),
                "gates": {
                    **{k: {"status": "NOT_RUN"} for k in ("G0", "G1", "G2", "G3")},
                    "G4": {"status": "PASSED"},
                },
            }
        )


def test_loss_pause_is_visible_to_a_separate_process(tmp_path: Path) -> None:
    """A pause persisted by one process is seen by a fresh `trader status` process."""
    from trader.ops.pause import PauseService
    from trader.storage.db import Database

    db_file = tmp_path / "p.db"
    db = Database(f"sqlite:///{db_file.as_posix()}")
    db.create_schema()
    from datetime import UTC, datetime

    with db.tx() as conn:
        PauseService().owner_pause(
            conn,
            portfolio_id="pf-main",
            reason="drill",
            by="test",
            now=datetime(2026, 11, 27, tzinfo=UTC),
        )
    db.dispose()
    out = subprocess.run(
        [
            sys.executable,
            "-m",
            "trader.cli.main",
            "status",
            "--db",
            f"sqlite:///{db_file.as_posix()}",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONUTF8": "1"},
        check=True,
    ).stdout
    assert "OWNER_REQUESTED" in out
