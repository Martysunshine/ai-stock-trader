"""Authority boundaries (architecture.md §2, instructions.md §3, §5).

* Only ``execution/_gateway.py`` calls ``.submit(`` / ``.cancel(`` on a broker adapter.
* Only ``risk/approval.py`` constructs an ``ApprovedPlan``.
* Strategies, features and models cannot import brokers, execution, the allocator or ops.
* The dashboard's market feed (``trader.market``) cannot import brokers, execution, risk, runtime
  or storage (ADR-0045). The same holds for the intel feed (``trader.intel``), and nothing that
  decides or trades may import the intel feed (ADR-0047).
* No LLM/HTTP client packages are dependencies of the execution process; the one broker SDK is
  pinned and confined to its adapter package (ADR-0025).
"""

from __future__ import annotations

import ast
import tomllib
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "trader"


def _modules() -> list[tuple[Path, ast.Module]]:
    return [(p, ast.parse(p.read_text(encoding="utf-8"))) for p in sorted(SRC.rglob("*.py"))]


def _rel(p: Path) -> str:
    return p.relative_to(SRC).as_posix()


def test_only_the_gateway_submits_or_cancels_broker_orders() -> None:
    offenders = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr in ("submit", "cancel") and _rel(path) not in (
                    "execution/_gateway.py",
                ):
                    offenders.append(f"{_rel(path)}:{node.lineno}")
    assert offenders == []


def test_only_the_risk_approval_module_constructs_approved_plans() -> None:
    offenders = []
    for path, tree in _modules():
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "ApprovedPlan":
                if _rel(path) != "risk/approval.py":
                    offenders.append(f"{_rel(path)}:{node.lineno}")
    assert offenders == []


def test_strategies_features_and_models_cannot_reach_order_authority() -> None:
    forbidden = (
        "trader.brokers",
        "trader.execution",
        "trader.risk.allocator",
        "trader.ops",
        "trader.storage",
    )
    offenders = []
    for path, tree in _modules():
        if not _rel(path).startswith(("strategies/", "features/", "models/")):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{_rel(path)} imports {n}" for n in names if n.startswith(forbidden)]
    assert offenders == []


def test_broker_adapters_are_imported_only_by_wiring_and_execution() -> None:
    allowed = ("brokers/", "execution/", "runtime/", "cli/", "reporting/feasibility.py")
    offenders = []
    for path, tree in _modules():
        if _rel(path).startswith(allowed):
            continue
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("trader.brokers")
            ):
                if node.module != "trader.brokers.catalog":
                    offenders.append(f"{_rel(path)} imports {node.module}")
    assert offenders == []


#: ADR-0025: the official Alpaca SDK is the one allowed broker SDK, pinned to a reviewed release.
ALLOWED_BROKER_SDK = "alpaca-py==0.44.0"


def test_no_llm_or_funding_capability_in_runtime_dependencies() -> None:
    project = tomllib.loads((SRC.parents[1] / "pyproject.toml").read_text(encoding="utf-8"))
    runtime = [d.lower().replace(" ", "") for d in project["project"]["dependencies"]]
    sdk = [d for d in runtime if d.startswith("alpaca")]
    assert sdk in ([], [ALLOWED_BROKER_SDK]), f"broker SDK must be exactly {ALLOWED_BROKER_SDK}"
    deps = " ".join(d for d in runtime if d != ALLOWED_BROKER_SDK)
    for banned in (
        "openai",
        "anthropic",
        "langchain",
        "transformers",
        "requests",
        "httpx",
        "alpaca",
    ):
        assert banned not in deps
    adapter_api = (SRC / "brokers" / "base.py").read_text(encoding="utf-8").lower()
    for word in ("withdraw", "deposit", "transfer", "funding("):
        assert word not in adapter_api.split('"""', 2)[-1]


def test_wall_clock_and_random_are_confined() -> None:
    offenders = []
    for path, tree in _modules():
        if _rel(path) in (
            "ops/clock.py",
            "data/synthetic.py",
            "data/synthetic_history.py",
            "cli/main.py",
        ):
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in ("now", "utcnow", "today"):
                if isinstance(node.value, ast.Name) and node.value.id in ("datetime", "date"):
                    offenders.append(f"{_rel(path)}:{node.lineno}")
            if isinstance(node, ast.Import) and any(a.name == "random" for a in node.names):
                offenders.append(f"{_rel(path)} imports random")
    assert offenders == []


def test_research_and_ingestion_cannot_reach_order_authority() -> None:
    """Research output is evidence for review; it can never place, approve or route an order."""
    forbidden = (
        "trader.brokers.replay",
        "trader.brokers.base",
        "trader.execution",
        "trader.risk.allocator",
        "trader.risk.approval",
        "trader.ops",
        "trader.storage",
        "trader.runtime",
    )
    offenders = []
    for path, tree in _modules():
        if not _rel(path).startswith(("research/", "data/")):
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [f"{_rel(path)} imports {n}" for n in names if n.startswith(forbidden)]
    assert offenders == []


def test_the_market_feed_cannot_reach_order_authority() -> None:
    """ADR-0045: the dashboard's market feed is a data tool. It reads prices and writes
    data/market/ only, so it may not import brokers, execution, risk, runtime or storage."""
    forbidden = (
        "trader.brokers",
        "trader.execution",
        "trader.risk",
        "trader.runtime",
        "trader.storage",
    )
    offenders = []
    checked = 0
    for path, tree in _modules():
        if not _rel(path).startswith("market/"):
            continue
        checked += 1
        offenders += [
            f"{_rel(path)} imports {n}" for n in _imports(tree) if n.startswith(forbidden)
        ]
    assert checked >= 4 and offenders == []


def test_the_intel_feed_cannot_reach_order_authority() -> None:
    """ADR-0047: the intel feed is a data tool. It reads news, filings and pre-market bars and
    writes data/intel/ only, so it may not import brokers, execution, risk, runtime or storage."""
    forbidden = (
        "trader.brokers",
        "trader.execution",
        "trader.risk",
        "trader.runtime",
        "trader.storage",
    )
    offenders = []
    checked = 0
    for path, tree in _modules():
        if not _rel(path).startswith("intel/"):
            continue
        checked += 1
        offenders += [
            f"{_rel(path)} imports {n}" for n in _imports(tree) if n.startswith(forbidden)
        ]
    assert checked >= 6 and offenders == []


def test_nothing_that_decides_or_trades_reads_the_intel_feed() -> None:
    """ADR-0047: collect, show, then test. Until a registered trial passes and a new decision
    record says otherwise, only the feed itself and the command line import ``trader.intel``:
    no strategy, feature, model, risk rule, runtime loop, research study or report."""
    offenders = [
        f"{_rel(path)} imports {name}"
        for path, tree in _modules()
        if not _rel(path).startswith(("intel/", "cli/"))
        for name in _imports(tree)
        if name == "trader.intel" or name.startswith("trader.intel.")
    ]
    assert offenders == []


def test_network_access_is_confined_to_the_market_data_transport() -> None:
    offenders = []
    for path, tree in _modules():
        if _rel(path) == "data/sources/http.py":
            continue
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            offenders += [
                f"{_rel(path)} imports {n}"
                for n in names
                if n.split(".")[0] in ("urllib", "http", "socket", "ssl")
            ]
    assert offenders == []


def _imports(tree: ast.Module) -> list[str]:
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            names.append(node.module)
    return names


def test_third_party_sdks_are_confined() -> None:
    """ADR-0025/0026/0027: the broker SDK lives only in its adapter package, the keyring only in
    the secrets module, and training libraries only in research (never in the runtime or models/,
    whose bundles run on numpy alone)."""
    rules = {
        "alpaca": ("brokers/alpaca/",),
        "keyring": ("config/secrets.py",),
        "truststore": ("brokers/alpaca/",),
        "sklearn": ("research/",),
        "lightgbm": ("research/",),
        "scipy": ("research/",),
    }
    offenders = []
    for path, tree in _modules():
        rel = _rel(path)
        for name in _imports(tree):
            top = name.split(".")[0]
            if top in rules and not rel.startswith(rules[top]):
                offenders.append(f"{rel} imports {name}")
    assert offenders == []


def test_models_do_not_depend_on_research_code() -> None:
    offenders = [
        f"{_rel(path)} imports {name}"
        for path, tree in _modules()
        if _rel(path).startswith("models/")
        for name in _imports(tree)
        if name.startswith("trader.research")
    ]
    assert offenders == []
