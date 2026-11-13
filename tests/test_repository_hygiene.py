"""Repository hygiene: nothing the project needs is silently excluded from version control.

A too-broad ignore rule once matched ``src/trader/data/`` as well as the market-data folder
``/data/``; a fresh clone would then miss source code. This test asks git directly.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TRACKED_TREES = ("src", "tests", "configs", "docs", "research", "deploy")


def test_no_source_config_or_document_is_gitignored() -> None:
    git = shutil.which("git")
    if git is None or not (ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    files = [
        p.relative_to(ROOT).as_posix()
        for tree in TRACKED_TREES
        if (ROOT / tree).exists()
        for p in (ROOT / tree).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts
    ]
    result = subprocess.run(
        [git, "check-ignore", "--stdin"],
        input="\n".join(files),
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.stdout.strip() == "", f"ignored project files:\n{result.stdout}"
    market_data = subprocess.run(
        [git, "check-ignore", "data/history/x.parquet", "runs/x/report.md", ".env"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert len(market_data.stdout.split()) == 3  # data, outputs and secrets stay out of git
