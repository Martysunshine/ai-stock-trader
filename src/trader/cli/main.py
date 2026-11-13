"""``trader`` command-line interface (implementation_roadmap.md §3).

Replay is the default. ``paper`` and ``live`` are guarded entry points that never fall back to
another mode. No command moves funds, and nothing here can enable live trading by itself.
"""

from __future__ import annotations

import getpass
import json
import os
import signal
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import FrameType
from typing import TYPE_CHECKING, Annotated, Any, NoReturn

import typer

from trader.accounting.fees import load_fee_schedule
from trader.accounting.ledger import Ledger
from trader.brokers.catalog import load_capabilities, load_instruments
from trader.config.host import current_host_role
from trader.config.policy import dollar_table
from trader.config.runtime_config import load_config
from trader.data.recorded_session import load_session
from trader.domain.types import Mode
from trader.ops.owner_actions import current_loss_breaches, resume_loss_breaches
from trader.ops.pause import PauseService
from trader.risk.engine import required_entry_capability
from trader.runtime.live_guard import check_live_start
from trader.storage.db import Database

if TYPE_CHECKING:
    from trader.config.runtime_config import ResolvedConfig
    from trader.data.sources.alpaca_history import AlpacaCredentials
    from trader.runtime.paper import PaperEnvironment
    from trader.strategies.edge import EdgeEstimator

#: Printed when ``paper run --practice`` starts, so the log can never be mistaken for evidence.
PRACTICE_BANNER = (
    "PRACTICE MODE (ADR-0040): candidates are taken when their planned target covers all "
    "round-trip costs; decisions carry evidence PRACTICE and never count for G2/G3"
)

app = typer.Typer(add_completion=False, no_args_is_help=True, help=__doc__)
docs_app = typer.Typer(help="Generated documentation sections.")
data_app = typer.Typer(help="Recorded/synthetic session and market-data tools.")
research_app = typer.Typer(help="Registered research studies and the experiment registry (M2).")
secrets_app = typer.Typer(
    help="Broker/data keys in the OS keyring (hidden entry; values are never printed or stored "
    "in the repository)."
)
paper_app = typer.Typer(
    no_args_is_help=True,
    help="Paper trading on the broker's paper account (M4). Never live, never a fallback.",
)
app.add_typer(docs_app, name="docs")
app.add_typer(data_app, name="data")
app.add_typer(research_app, name="research")
app.add_typer(secrets_app, name="secrets")
market_app = typer.Typer(
    no_args_is_help=True,
    help="Dashboard market feed (ADR-0045): watchlist prices into data/market/ for the console. "
    "Market data only; no order authority.",
)
app.add_typer(paper_app, name="paper")
journal_app = typer.Typer(
    no_args_is_help=True,
    help="Practice journal of the assistant's Capital.com DEMO trades (ADR-0044); never live.",
)
app.add_typer(journal_app, name="journal")
app.add_typer(market_app, name="market")
intel_app = typer.Typer(
    no_args_is_help=True,
    help="Intel feed (ADR-0047): news, insider filings and pre-market activity into data/intel/. "
    "Information only; no order authority, and nothing that decides a trade reads it.",
)
app.add_typer(intel_app, name="intel")

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CONFIG = REPO_ROOT / "configs" / "replay.yaml"
PAPER_CONFIG = REPO_ROOT / "configs" / "paper.yaml"

ConfigOpt = Annotated[Path, typer.Option("--config", help="Mode configuration file")]
DbOpt = Annotated[
    str, typer.Option("--db", help="Database URL (sqlite:///... or postgresql+psycopg://...)")
]
AliasOpt = Annotated[
    str | None,
    typer.Option("--account-alias", help="Account alias (default: the only configured account)"),
]


def _echo(msg: str) -> None:
    typer.echo(msg)


def _fail(msg: str, code: int) -> None:
    typer.echo(f"ERROR: {msg}", err=True)
    raise typer.Exit(code)


def _refuse(msg: str, code: int = 2) -> NoReturn:
    typer.echo(f"ERROR: {msg}", err=True)
    raise typer.Exit(code)


def _now() -> datetime:
    return datetime.now(UTC)


# --------------------------------------------------------------------------- config


@app.command("config-check")
def config_check(config: ConfigOpt = DEFAULT_CONFIG) -> None:
    """Validate a configuration, its policy, track profiles, capabilities, fees and instruments."""
    try:
        cfg = load_config(config)
    except Exception as exc:
        _fail(str(exc), 2)
        return
    rt = cfg.runtime
    _echo(f"config        : {config}")
    _echo(f"mode          : {rt.mode.value} (live_enabled={rt.live_enabled})")
    _echo(f"host role     : {cfg.host_role.value}")
    _echo(f"policy        : {cfg.policy.source}")
    _echo(f"policy hash   : {cfg.policy.policy_hash}")
    capital = cfg.policy.policy.initial_trading_capital
    _echo(f"dollar table at USD {capital} (risk_and_validation.md section 2):")
    for key, value in dollar_table(cfg.policy.policy).items():
        _echo(f"  {key:<30} {value.quantize(Decimal('0.001'))}")
    _echo("tracks:")
    for track, profile in cfg.tracks.items():
        _echo(
            f"  {track.value:<12} enabled={profile.enabled} cadence={profile.decision_bar_minutes}m/"
            f"{profile.context_bar_minutes}m calendar={profile.calendar}"
        )
    problems = 0
    for acct in rt.accounts:
        caps = load_capabilities(cfg.resolve(acct.capability_profile))
        fees = load_fee_schedule(cfg.resolve(acct.fee_schedule))
        specs = load_instruments(cfg.resolve(acct.instruments))
        _echo(
            f"account {acct.alias} ({acct.account_id}) track={acct.track.value} broker={acct.broker}"
        )
        _echo(f"  capability profile {caps.profile_version} rule profile={caps.rule_profile.value}")
        _echo(
            f"  fee schedule {fees.schedule_id} known={fees.known} evidence={fees.config.evidence.value}"
        )
        for iid, spec in specs.items():
            cert = spec.certify()
            needed = required_entry_capability(spec)
            usable = caps.usable(needed)
            flag = "ok" if cert.certified and usable and fees.known else "BLOCKED"
            if flag == "BLOCKED":
                problems += 1
            reasons = (
                list(cert.problems)
                + ([] if usable else [f"{needed} not usable"])
                + ([] if fees.known else ["fees unknown"])
            )
            _echo(f"    {iid:<20} {flag:<8} {'; '.join(reasons)}")
    _echo(
        "configuration valid"
        + (f" ({problems} instrument(s) blocked: no trade)" if problems else "")
    )


# --------------------------------------------------------------------------- replay


@app.command()
def replay(
    scenario: Annotated[str, typer.Option(help="baseline|timeout|crash|costly|loss")] = "baseline",
    run_dir: Annotated[Path | None, typer.Option(help="Output directory")] = None,
    db: Annotated[str | None, typer.Option(help="Optional PostgreSQL URL")] = None,
    config: ConfigOpt = DEFAULT_CONFIG,
) -> None:
    """Run one demonstrator scenario in deterministic replay."""
    from trader.runtime.scenarios import SCENARIOS, run_scenario

    if scenario not in SCENARIOS:
        _fail(f"unknown scenario {scenario}; choose from {sorted(SCENARIOS)}", 2)
    out = run_dir or REPO_ROOT / "runs" / f"replay-{scenario}"
    schema = None
    if db and db.startswith("postgresql"):
        schema = f"replay_{scenario}"
    result = run_scenario(
        scenario, repo_root=REPO_ROOT, run_dir=out, config_path=config, db_url=db, db_schema=schema
    )
    _echo(result.report.to_markdown())
    _echo(f"report written to {out}")


@app.command()
def demo(
    out: Annotated[Path, typer.Option(help="Output directory")] = REPO_ROOT / "runs" / "demo",
    postgres: Annotated[
        bool, typer.Option(help="Also reproduce the baseline on PostgreSQL")
    ] = False,
) -> None:
    """Run the full first demonstrator (instructions.md section 7) and its acceptance checks."""
    from trader.runtime.demo import run_demo

    url = os.environ.get("TRADER_TEST_DATABASE_URL") if postgres else None
    if postgres and not url:
        _fail("TRADER_TEST_DATABASE_URL is not set (see .env.example)", 2)
    summary = run_demo(REPO_ROOT, out, postgres_url=url)
    _echo(summary.to_markdown())
    _echo(f"summary written to {out / 'summary.md'}")
    if not summary.passed:
        raise typer.Exit(1)


@app.command()
def report(
    run_dir: Annotated[Path, typer.Option(help="Run directory containing report.md")],
) -> None:
    """Print a run report."""
    path = run_dir / "report.md"
    if not path.exists():
        _fail(f"no report at {path}", 2)
    _echo(path.read_text(encoding="utf-8"))


@app.command()
def evaluate(
    run_dir: Annotated[Path, typer.Option(help="Run directory")],
    compare: Annotated[
        Path | None, typer.Option(help="Second run directory to compare hashes")
    ] = None,
) -> None:
    """Integrity evaluation of a replay run (statistical evaluation arrives with M2)."""
    data = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
    rep = data["report"]
    ok = rep["active_reservations"] == 0
    _echo(
        f"active reservations: {rep['active_reservations']}  open positions: {rep['open_positions']}"
    )
    _echo(f"decision hash {rep['decision_hash']}\nledger hash   {rep['ledger_hash']}")
    if compare is not None:
        other = json.loads((compare / "report.json").read_text(encoding="utf-8"))["report"]
        same = (
            other["decision_hash"] == rep["decision_hash"]
            and other["ledger_hash"] == rep["ledger_hash"]
        )
        _echo("reproducible: " + ("YES" if same else "NO"))
        ok = ok and same
    _echo("statistical evaluation (block bootstrap, walk-forward): not available until M2")
    if not ok:
        raise typer.Exit(1)


# --------------------------------------------------------------------------- operations


@app.command()
def status(db: DbOpt) -> None:
    """Show active pauses, positions and live orders recorded in a database."""
    from sqlalchemy import select

    from trader.domain.orders import LIVE_STATES
    from trader.storage.schema import accounts, order_intents

    database = Database(db)
    pauses = PauseService()
    ledger = Ledger(database)
    with database.tx() as conn:
        flags = pauses.all_active(conn)
        accts = conn.execute(select(accounts.c.account_id)).scalars().all()
        positions = ledger.open_positions(conn, list(accts))
        live = conn.execute(
            select(
                order_intents.c.client_order_id, order_intents.c.purpose, order_intents.c.state
            ).where(order_intents.c.state.in_(tuple(s.value for s in LIVE_STATES)))
        ).all()
    _echo(f"active pauses ({len(flags)}):")
    for f in flags:
        _echo(
            f"  {f.category.value}:{f.cause.value} [{f.scope.value} {f.scope_id}] since {f.raised_at} - {f.details}"
        )
    _echo(f"open positions ({len(positions)}):")
    for p in positions:
        _echo(f"  {p.account_id} {p.instrument_id} qty={p.quantity}")
    _echo(f"live orders ({len(live)}):")
    for cid, purpose, state in live:
        _echo(f"  {cid} {purpose} {state}")
    database.dispose()


@app.command()
def pause(
    db: DbOpt,
    reason: Annotated[str, typer.Option(help="Why entries are paused")],
    portfolio: Annotated[str, typer.Option(help="Portfolio ID")] = "pf-main",
) -> None:
    """Pause new exposure (risk-reducing exits stay available)."""
    database = Database(db)
    with database.tx() as conn:
        flag = PauseService().owner_pause(
            conn, portfolio_id=portfolio, reason=reason, by=getpass.getuser(), now=_now()
        )
    _echo(f"paused: {flag}")


@app.command()
def resume(
    db: DbOpt,
    config: ConfigOpt = DEFAULT_CONFIG,
    ack_loss_breach: Annotated[
        bool, typer.Option(help="Clear loss-breach pauses after review")
    ] = False,
    note: Annotated[
        str, typer.Option(help="Written review note (required for loss breaches)")
    ] = "",
    portfolio: Annotated[str, typer.Option(help="Portfolio ID")] = "pf-main",
) -> None:
    """Resume after an owner pause; loss breaches need --ack-loss-breach and a review note."""
    database = Database(db)
    pauses = PauseService()
    now = _now()
    with database.tx() as conn:
        n = pauses.owner_resume(conn, portfolio_id=portfolio, by=getpass.getuser(), now=now)
    _echo(f"owner pauses cleared: {n}")
    if not ack_loss_breach:
        return
    cfg = load_config(config)
    acct = cfg.runtime.accounts[0]
    fees = load_fee_schedule(cfg.resolve(acct.fee_schedule))
    profile = cfg.tracks[acct.track]
    still = current_loss_breaches(
        db=database,
        ledger=Ledger(database),
        policy=cfg.policy,
        portfolio_id=portfolio,
        account_id=acct.account_id,
        currency="USD",
        fees=fees,
        marks={},
        now=now,
        ny_windows=tuple(w for w in profile.loss_windows if w.startswith("NY_")),
    )
    outcome = resume_loss_breaches(
        db=database,
        pauses=pauses,
        policy=cfg.policy,
        portfolio_id=portfolio,
        reviewer=getpass.getuser(),
        note=note,
        still_breached=still,
        figures={"still_breached": ",".join(sorted(still))},
        now=now,
    )
    for flag in outcome.cleared:
        _echo(f"cleared after review: {flag}")
    for refusal in outcome.refused:
        _echo(f"REFUSED: {refusal}")
    if outcome.refused:
        raise typer.Exit(1)


@app.command()
def reconcile(config: ConfigOpt = DEFAULT_CONFIG, account_alias: AliasOpt = None) -> None:
    """Reconcile the ledger with the broker (paper: through the adapter).

    Replay runs reconcile automatically; a running paper worker reconciles every 60 seconds.
    """
    cfg = _load(config)
    if cfg.runtime.mode is Mode.REPLAY:
        _echo(
            "replay runs reconcile at startup, after unknown outcomes and at the session close; "
            "see the run report. Nothing to do offline."
        )
        return
    if cfg.runtime.mode is not Mode.PAPER:
        _refuse(f"{cfg.runtime.mode.value} reconciliation is not available (no certified adapter)")
    from trader.ops.leadership import LeadershipUnavailable
    from trader.runtime.paper import reconcile_paper

    with _paper_environment(cfg, account_alias, require_baseline=True) as (env, _credentials):
        try:
            result = reconcile_paper(env)
        except LeadershipUnavailable as exc:
            _refuse(f"{exc}: the running paper worker reconciles every 60 seconds itself")
    _echo(f"reconciliation {result.run_id}: {result.status}")
    for problem in result.problems:
        _echo(f"  problem: {problem}")
    for note in result.notes:
        _echo(f"  note: {note}")
    if result.cleared_flags:
        _echo(f"  cleared health pauses: {len(result.cleared_flags)}")
    if result.status != "PASSED":
        raise typer.Exit(1)


@app.command("close-positions")
def close_positions(
    db: Annotated[
        str | None,
        typer.Option("--db", help="Database URL to report from (replay/offline: no broker)"),
    ] = None,
    config: Annotated[
        Path | None,
        typer.Option("--config", help="A paper configuration: liquidate through the adapter"),
    ] = None,
    account_alias: AliasOpt = None,
    timeout_seconds: Annotated[
        int, typer.Option(min=1, help="Paper: how long to wait for confirmed fills")
    ] = 120,
) -> None:
    """Close recorded positions; never claims a closure without confirmed fills.

    Paper (--config): reducing market sells through the adapter, then wait for confirmed fills.
    Offline (--db): report holdings only.
    """
    if config is not None:
        cfg = _load(config)
        if cfg.runtime.mode is not Mode.PAPER:
            _refuse(
                f"close-positions --config accepts a paper configuration, not "
                f"{cfg.runtime.mode.value}; use --db to report a replay database"
            )
        from trader.ops.leadership import LeadershipUnavailable
        from trader.runtime.paper import close_paper_positions

        with _paper_environment(cfg, account_alias, require_baseline=True) as (env, _creds):
            try:
                outcome = close_paper_positions(env, timeout_seconds=timeout_seconds)
            except LeadershipUnavailable as exc:
                _refuse(f"{exc}: stop the running paper worker first (Ctrl+C)")
        closed = sorted(set(outcome.requested) - set(outcome.remaining))
        for key in closed:
            _echo(f"CLOSED {key} qty={outcome.requested[key]} (confirmed fills)")
        for key, qty in sorted(outcome.remaining.items()):
            _echo(f"PENDING LIQUIDATION {key} qty={qty}")
        _echo(f"reconciliation: {outcome.reconciliation}")
        if not outcome.requested:
            _echo("no open positions recorded: nothing to close")
        if outcome.remaining:
            _fail("positions remain open until the broker confirms fills", 1)
        return
    if db is None:
        _refuse("pass --db <url> (report) or --config <paper.yaml> (liquidate through the broker)")
    from sqlalchemy import select

    from trader.storage.schema import accounts

    database = Database(db)
    ledger = Ledger(database)
    with database.tx() as conn:
        accts = conn.execute(select(accounts.c.account_id)).scalars().all()
        positions = ledger.open_positions(conn, list(accts))
    if not positions:
        _echo("no open positions recorded: nothing to close")
        return
    for p in positions:
        _echo(f"PENDING LIQUIDATION {p.account_id} {p.instrument_id} qty={p.quantity}")
    _fail("no broker connection in this mode: positions remain open until fills are confirmed", 1)


@app.command("ingest-history")
def ingest_history(
    manifest: Annotated[
        Path, typer.Option(help="research/data_manifest.json, or a recorded session manifest.json")
    ] = REPO_ROOT / "research" / "data_manifest.json",
    dataset: Annotated[
        list[str] | None, typer.Option("--dataset", help="Only these history datasets")
    ] = None,
    verify_only: Annotated[
        bool, typer.Option(help="Only re-check checksums of acquired datasets")
    ] = False,
    refresh: Annotated[bool, typer.Option(help="Re-acquire datasets already ACQUIRED")] = False,
) -> None:
    """Acquire, validate and checksum history datasets listed in the data manifest.

    Public crypto sources need no account. Alpaca stock history needs the owner's keys in
    APCA_API_KEY_ID / APCA_API_SECRET_KEY (never stored); without them the dataset is BLOCKED.
    Capital.com two-sided history reads DEMO prices with the demo keys (ADR-0052).
    """
    if manifest.name == "manifest.json":
        session = load_session(manifest.parent)
        m = session.manifest
        _echo(f"{m.name}: {m.event_count} events, feed {m.feed}, synthetic={m.synthetic}, ok")
        for limitation in m.known_limitations:
            _echo(f"  limitation: {limitation}")
        return
    from trader.config.secrets import SecretsError, load_alpaca
    from trader.data.ingest import (
        IngestContext,
        RestoreOutcome,
        ingest,
        verify_dataset,
        verify_or_restore,
    )
    from trader.data.manifest import load_manifest, save_manifest
    from trader.data.sources.http import UrllibTransport

    def credentials_from_env() -> AlpacaCredentials | None:
        """Session environment variables first, then the OS keyring (ADR-0026)."""
        try:
            return load_alpaca()
        except SecretsError as exc:
            _fail(str(exc), 2)
            raise  # pragma: no cover - _fail raises

    capital_reader: list[Any] = []

    def capital_prices(ds_source: str) -> Any:
        """A logged-in reader of Capital.com DEMO prices, built once and only for a dataset that
        needs it; ``None`` (the dataset is then BLOCKED) when the demo keys are not stored."""
        if ds_source != "capital_demo_prices":
            return None
        if not capital_reader:
            from trader.config.secrets import require_capital_demo
            from trader.data.sources.capital_prices import CapitalPrices
            from trader.ops.clock import SystemClock

            try:
                require_capital_demo()
            except SecretsError:
                capital_reader.append(None)
            else:
                capital_reader.append(
                    CapitalPrices(
                        UrllibTransport(min_interval_seconds=0.5),
                        require_capital_demo,
                        clock=SystemClock(),
                    )
                )
        return capital_reader[0]

    data_manifest = load_manifest(manifest)
    wanted = set(dataset or [])
    unknown = wanted - {d.name for d in data_manifest.history}
    if unknown:
        _fail(f"unknown datasets: {sorted(unknown)}", 2)
    failures = 0
    for ds in data_manifest.history:
        if wanted and ds.name not in wanted:
            continue
        if verify_only and ds.status != "ACQUIRED":
            _echo(f"{ds.name}: {ds.status} {ds.blocker}".rstrip())  # a blocker, not a failure
            continue
        if verify_only or (ds.status == "ACQUIRED" and not refresh):
            outcome = (
                RestoreOutcome("OK", "; ".join(verify_dataset(ds, REPO_ROOT)) or "checksums ok")
                if verify_only
                else verify_or_restore(
                    ds,
                    IngestContext(
                        repo_root=REPO_ROOT,
                        transport=UrllibTransport(),
                        now=_now(),
                        alpaca=credentials_from_env(),
                        capital=capital_prices(ds.source),
                    ),
                )
            )
            if verify_only and outcome.message != "checksums ok":
                outcome = RestoreOutcome("FAILED", outcome.message)
            failures += outcome.failed
            _echo(f"{ds.name}: {outcome.message}")
            continue
        ctx = IngestContext(
            repo_root=REPO_ROOT,
            transport=UrllibTransport(),
            now=_now(),
            alpaca=credentials_from_env(),
            capital=capital_prices(ds.source),
        )
        updated = ingest(ds, ctx)
        data_manifest = data_manifest.replace(updated)
        save_manifest(manifest, data_manifest)
        if updated.status == "BLOCKED":
            _echo(f"{ds.name}: BLOCKED - {updated.blocker}")
            continue
        for f in updated.files:
            q = f.quality
            _echo(
                f"{ds.name}/{f.instrument_id}: {f.rows} bars, coverage {q['coverage']}, "
                f"missing {q['missing_intervals']}, content {f.content_sha256[:12]}"
            )
    if failures:
        _fail(f"{failures} dataset(s) failed verification", 1)


# --------------------------------------------------------------------------- paper (M4)


def _load(config: Path) -> ResolvedConfig:
    try:
        return load_config(config)
    except Exception as exc:  # invalid file, host role, policy: all refuse with a message
        _refuse(str(exc))


def _paper_config(config: Path) -> ResolvedConfig:
    cfg = _load(config)
    if cfg.runtime.mode is not Mode.PAPER:
        _refuse(
            f"paper commands only accept a paper configuration, not {cfg.runtime.mode.value} "
            "(no fallback)"
        )
    return cfg


def _practice_edge(cfg: ResolvedConfig) -> EdgeEstimator:
    """The practice-mode estimator for a paper configuration (ADR-0040); never live."""
    from trader.strategies.edge import PracticeEdgeEstimator

    if cfg.runtime.mode is not Mode.PAPER or cfg.runtime.live_enabled:
        _refuse("practice mode needs a paper configuration with live trading disabled")
    return PracticeEdgeEstimator(Mode.PAPER)


@contextmanager
def _paper_environment(
    cfg: ResolvedConfig,
    alias: str | None,
    *,
    require_baseline: bool,
    edge: EdgeEstimator | None = None,
) -> Iterator[tuple[PaperEnvironment, AlpacaCredentials]]:
    """Database, keys, adapter and environment for a paper command, refusing with exit 2."""
    from trader.config.secrets import DEFAULT_ALPACA_SERVICE, SecretsError, require_alpaca
    from trader.domain.types import DomainError
    from trader.ops.clock import SystemClock
    from trader.runtime.broker_factory import AdapterUnavailable, alpaca_adapter_class
    from trader.runtime.broker_factory import paper_adapter as make_adapter
    from trader.runtime.paper import (
        adapter_since,
        build_paper_environment,
        paper_account,
        paper_database_url,
    )

    try:
        acct = paper_account(cfg, alias)
        url = paper_database_url(cfg)
        credentials = require_alpaca(acct.secret_ref or DEFAULT_ALPACA_SERVICE)
        alpaca_adapter_class()  # fail before touching the database when the adapter is absent
    except (DomainError, SecretsError, AdapterUnavailable) as exc:
        _refuse(str(exc))
    clock = SystemClock()
    database = Database(url)
    try:
        since = adapter_since(database, acct.account_id, now=clock.now())
        adapter = make_adapter(cfg, acct, credentials=credentials, clock=clock, since=since)
        env = build_paper_environment(
            cfg,
            clock=clock,
            adapter=adapter,
            database=database,
            account_alias=acct.alias,
            require_baseline=require_baseline,
            edge=edge,
        )
    except Exception as exc:  # database, migration, adapter or account refusal: nothing started
        database.dispose()
        detail = str(exc) if isinstance(exc, DomainError) else f"{type(exc).__name__}: {exc}"
        _refuse(f"paper start refused: {detail}")
    try:
        yield env, credentials
    finally:
        env.close()


@contextmanager
def _stop_on_interrupt(request_stop: Callable[[str], None]) -> Iterator[None]:
    """First Ctrl+C: graceful stop (no new entries, final reconcile). Second: abort."""
    previous = signal.getsignal(signal.SIGINT)

    def handler(signum: int, frame: FrameType | None) -> None:
        request_stop("Ctrl+C")
        _echo("stopping: no new entries; cancelling working entries, then a final reconciliation")
        signal.signal(signal.SIGINT, previous)

    signal.signal(signal.SIGINT, handler)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@paper_app.command("run")
def paper_run(
    config: ConfigOpt = PAPER_CONFIG,
    account_alias: AliasOpt = None,
    sessions: Annotated[
        int | None,
        typer.Option(min=1, help="Stop after N completed sessions (default: until Ctrl+C)"),
    ] = None,
    practice: Annotated[
        bool,
        typer.Option(
            "--practice",
            help="Practice mode (ADR-0040): take strategy candidates whose planned target "
            "covers all round-trip costs, labelled PRACTICE; never evidence",
        ),
    ] = False,
) -> None:
    """Run the paper bot on the broker's paper account until Ctrl+C or --sessions.

    Each completed bar: features -> strategy -> risk -> approval -> outbox -> gateway. Needs host
    role DEV/PAPER, PostgreSQL (TRADER_DATABASE_URL) and paper keys in the OS keyring.
    """
    from trader.data.sources.http import UrllibTransport
    from trader.ops.leadership import LeadershipUnavailable
    from trader.runtime.paper import PaperRunner
    from trader.runtime.paper_data import AlpacaMarketData, DelayedSipSpreads

    cfg = _paper_config(config)
    edge = _practice_edge(cfg) if practice else None
    with _paper_environment(cfg, account_alias, require_baseline=False, edge=edge) as (
        env,
        credentials,
    ):
        transport = UrllibTransport()
        runner = PaperRunner(
            env,
            market_data=AlpacaMarketData(transport, credentials),
            log=_echo,
            # The IEX quote is one venue; the paper broker fills at the consolidated price, so
            # the entry spread check uses the delayed consolidated spread (ADR-0061).
            spread_reference=DelayedSipSpreads(transport, credentials),
        )
        runner.log("paper bot starting; Ctrl+C stops gracefully")
        runner.log("entry spread: the market's (consolidated quotes, 16 min delayed, median)")
        if edge is not None:
            runner.log(PRACTICE_BANNER)
        with _stop_on_interrupt(runner.request_stop):
            try:
                summary = runner.run(sessions=sessions)
            except LeadershipUnavailable as exc:
                _refuse(f"{exc}: only one paper worker may lead an account")
    for line in summary.lines(runner.loop.stats):
        _echo(line)
    if summary.final_reconciliation not in (None, "PASSED"):
        raise typer.Exit(1)


@paper_app.command("check")
def paper_check(config: ConfigOpt = PAPER_CONFIG, account_alias: AliasOpt = None) -> None:
    """Check the paper setup without connecting to anything.

    Configuration, host role, database URL, keys (presence only) and the adapter.
    """
    from trader.config.secrets import DEFAULT_ALPACA_SERVICE, SecretsError, require_alpaca
    from trader.domain.types import DomainError
    from trader.runtime.broker_factory import AdapterUnavailable, alpaca_adapter_class
    from trader.runtime.paper import paper_account, paper_database_url

    cfg = _paper_config(config)
    _echo(f"config        : {config} (mode {cfg.runtime.mode.value}, host {cfg.host_role.value})")
    problems: list[str] = []
    try:
        acct = paper_account(cfg, account_alias)
        _echo(f"account       : {acct.alias} ({acct.account_id}) broker {acct.broker}")
    except DomainError as exc:
        _refuse(str(exc))
    checks: list[tuple[str, Callable[[], object]]] = [
        ("database", lambda: paper_database_url(cfg)),
        ("paper keys", lambda: require_alpaca(acct.secret_ref or DEFAULT_ALPACA_SERVICE)),
        ("adapter", alpaca_adapter_class),
    ]
    for name, check in checks:
        try:
            check()
            _echo(f"{name:<14}: ok")
        except (DomainError, SecretsError, AdapterUnavailable) as exc:
            problems.append(name)
            _echo(f"{name:<14}: {exc}")
    if problems:
        _refuse(f"paper setup incomplete: {', '.join(problems)}")
    _echo("paper setup complete: `trader paper run` may start")


@paper_app.command("day")
def paper_day(
    config: ConfigOpt = PAPER_CONFIG,
    account_alias: AliasOpt = None,
    day: Annotated[
        str | None,
        typer.Option(
            "--date", help="Session date YYYY-MM-DD (default: the session of the latest fill)"
        ),
    ] = None,
) -> None:
    """Report one session of the paper account from its recorded fills. Reads only.

    Round trips closed that session with gross result, fees booked and fees the schedule would
    charge but the ledger does not hold, net result, and losing sessions in a row. No broker
    connection and no keys are needed; it is safe while the bot runs.
    """
    from datetime import date

    from sqlalchemy import text

    from trader.data.calendars import ExchangeCalendar, Utc247Calendar
    from trader.domain.types import DomainError
    from trader.reporting.day_report import (
        day_report,
        latest_session_with_fills,
        render_day_report,
    )
    from trader.runtime.paper import paper_account, paper_database_url

    cfg = _paper_config(config)
    try:
        acct = paper_account(cfg, account_alias)
        url = paper_database_url(cfg)
        session_date = None if day is None else date.fromisoformat(day)
    except DomainError as exc:
        _refuse(str(exc))
    except ValueError:
        _refuse(f"--date must be YYYY-MM-DD, not {day!r}")
    calendar: ExchangeCalendar | Utc247Calendar = (
        ExchangeCalendar("XNYS") if cfg.tracks[acct.track].calendar == "XNYS" else Utc247Calendar()
    )
    schedule = load_fee_schedule(cfg.resolve(acct.fee_schedule))
    database = Database(url)
    try:
        with database.tx() as conn:
            if database.is_postgres:
                conn.execute(text("SET TRANSACTION READ ONLY"))
            if session_date is None:
                session_date = latest_session_with_fills(
                    conn, acct.account_id, calendar.session_date_of
                )
                if session_date is None:
                    _echo(f"account {acct.account_id} has no fills yet")
                    return
            report = day_report(
                conn,
                account_id=acct.account_id,
                session_date=session_date,
                session_date_of=calendar.session_date_of,
                schedule=schedule,
            )
    finally:
        database.dispose()
    for line in render_day_report(report):
        _echo(line)


def _repo_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return path.name


@paper_app.command("certify")
def paper_certify(
    config: ConfigOpt = PAPER_CONFIG,
    account_alias: AliasOpt = None,
    yes: Annotated[
        bool, typer.Option("--yes", help="Place the paper orders the printed plan lists")
    ] = False,
    symbol: Annotated[
        str, typer.Option(help="A configured, fractionable stock to certify with")
    ] = "AAPL",
    output_dir: Annotated[
        Path, typer.Option("--output-dir", help="Where the report is written")
    ] = REPO_ROOT / "runs" / "paper" / "certification",
) -> None:
    """Demonstrate the Alpaca paper capabilities the profile still lists as UNKNOWN (M4).

    Prints the plan and sends nothing without --yes. With --yes: at most 6 paper orders, each
    bounded to USD 5 notional, all through risk, approval and the submission gateway; refused
    while the market is closed. Writes runs/paper/certification/<UTC timestamp>.json (no
    secrets); `trader paper certify-profile --report <file>` proposes a profile from it.
    """
    from collections.abc import Mapping
    from typing import cast

    from trader.config.secrets import DEFAULT_ALPACA_SERVICE, SecretsError, require_alpaca
    from trader.data.sources.http import UrllibTransport
    from trader.domain.certification import CertificationBudget
    from trader.domain.instruments import AnySpec
    from trader.domain.types import DomainError
    from trader.ops.clock import SystemClock
    from trader.runtime.broker_factory import AdapterUnavailable, alpaca_adapter_class
    from trader.runtime.broker_factory import paper_adapter as make_paper_adapter
    from trader.runtime.certification import (
        CertificationBroker,
        CertificationPlan,
        certify_paper_account,
        make_certification_environment,
        write_report,
    )
    from trader.runtime.paper import adapter_since, paper_account, paper_database_url
    from trader.runtime.paper_data import AlpacaMarketData

    cfg = _paper_config(config)
    try:
        plan = CertificationPlan(symbol=symbol.strip().upper())
        acct = paper_account(cfg, account_alias)
        profile = load_capabilities(cfg.resolve(acct.capability_profile))
    except DomainError as exc:
        _refuse(str(exc))
    for line in plan.lines(account=acct.account_id, profile_version=profile.profile_version):
        _echo(line)
    if not yes:
        _echo("nothing was sent: re-run with --yes to place these paper orders")
        return
    try:
        url = paper_database_url(cfg)
        credentials = require_alpaca(acct.secret_ref or DEFAULT_ALPACA_SERVICE)
        alpaca_adapter_class()
    except (DomainError, SecretsError, AdapterUnavailable) as exc:
        _refuse(str(exc))
    clock = SystemClock()
    database = Database(url)
    try:
        since = adapter_since(database, acct.account_id, now=clock.now())
    except Exception as exc:  # database or migration: nothing started
        database.dispose()
        _refuse(f"paper certification refused: {type(exc).__name__}: {exc}")

    def make_adapter(
        specs: Mapping[str, AnySpec] | None, budget: CertificationBudget | None
    ) -> CertificationBroker:
        adapter = make_paper_adapter(
            cfg,
            acct,
            credentials=credentials,
            clock=clock,
            since=since,
            instruments=specs,
            certification=budget,
        )
        return cast(CertificationBroker, adapter)

    try:
        report = certify_paper_account(
            cfg,
            clock=clock,
            market_data=AlpacaMarketData(UrllibTransport(), credentials),
            make_adapter=make_adapter,
            make_environment=make_certification_environment(
                cfg, clock=clock, database=database, account_alias=acct.alias
            ),
            plan=plan,
            account_alias=acct.alias,
            log=_echo,
        )
    finally:
        database.dispose()
    path = write_report(report, output_dir)
    for line in report.lines():
        _echo(line)
    _echo(f"report written: {path}")
    if report.status == "REFUSED":
        raise typer.Exit(2)
    _echo(f"next: trader paper certify-profile --report {_repo_relative(path)}")
    if report.status != "COMPLETED":
        raise typer.Exit(1)


@paper_app.command("certify-profile")
def paper_certify_profile(
    report: Annotated[Path, typer.Option("--report", help="A report from `trader paper certify`")],
    base: Annotated[
        Path, typer.Option("--base", help="The capability profile the run used")
    ] = REPO_ROOT / "configs" / "capabilities" / "alpaca_documented.yaml",
    out: Annotated[
        Path, typer.Option("--out", help="Where the proposed profile is written")
    ] = REPO_ROOT / "configs" / "capabilities" / "alpaca_paper_certified.yaml",
    force: Annotated[bool, typer.Option("--force", help="Overwrite an existing file")] = False,
) -> None:
    """Write a PROPOSED paper-certified capability profile from a certification report.

    Only what the report demonstrates is upgraded (evidence PAPER_DEMONSTRATED); everything
    else keeps the base value. Review it, point configs/paper.yaml at it, run `trader docs build`.
    """
    from trader.domain.types import DomainError
    from trader.runtime.certification import load_report
    from trader.runtime.certified_profile import proposal_summary, propose_certified_profile

    try:
        loaded = load_report(report)
        base_caps = load_capabilities(base)
        proposal = propose_certified_profile(loaded, base_caps, report_ref=_repo_relative(report))
    except (DomainError, ValueError, OSError) as exc:
        _refuse(str(exc))
    if out.exists() and not force:
        _refuse(f"{out} already exists; pass --force to overwrite it")
    text = proposal.yaml_text(
        report_ref=_repo_relative(report),
        base_ref=f"{base_caps.profile_version} ({_repo_relative(base)})",
        report_hash=loaded.report_hash,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8", newline="\n")
    load_capabilities(out)  # the written file must load exactly as the runtime loads it
    for line in proposal_summary(proposal):
        _echo(line)
    _echo(f"written: {out}. Review it, then run `trader docs build`.")


@app.command()
def live(
    config: ConfigOpt = REPO_ROOT / "configs" / "live.example.yaml",
    account_alias: Annotated[str, typer.Option("--account-alias")] = "live-pilot",
    enable_live: Annotated[
        bool, typer.Option("--enable-live", help="Deliberate live enablement")
    ] = False,
    manifest: Annotated[Path, typer.Option(help="Promotion manifest")] = REPO_ROOT
    / "research"
    / "promotion_manifest.json",
) -> None:
    """Guarded live entry point. Refuses unless every promotion requirement is met."""
    from trader.config.policy import load_policy, load_yaml_strict
    from trader.config.runtime_config import RuntimeConfig

    raw = load_yaml_strict(config)
    runtime = RuntimeConfig.model_validate(raw)
    policy = load_policy(
        config.parent / runtime.policy_file, mode=runtime.mode, live_enabled=runtime.live_enabled
    )
    result = check_live_start(
        enable_live_flag=enable_live,
        host_role=current_host_role(),
        runtime=runtime,
        policy_hash=policy.policy_hash,
        account_alias=account_alias,
        manifest_path=manifest,
        secret_present=False,
    )
    if not result.allowed:
        _fail(
            f"live start refused at {result.failed_step.value if result.failed_step else '?'}: {result.message}",
            result.exit_code,
        )
    _fail("unreachable: no certified live adapter exists in this build", 99)


# --------------------------------------------------------------------------- docs/data


@docs_app.command("build")
def docs_build(
    check: Annotated[bool, typer.Option(help="Fail if generated sections are stale")] = False,
) -> None:
    """Regenerate computed tables in docs/cost_feasibility.md and docs/broker_capabilities.md."""
    from trader.reporting.docs_build import build_docs

    stale = build_docs(REPO_ROOT, check=check)
    if check and stale:
        _fail(f"generated sections are stale: {stale} (run `trader docs build`)", 1)
    _echo("up to date" if not stale else f"updated: {stale}")


@data_app.command("synth")
def data_synth(variant: Annotated[str, typer.Option(help="base|gap")] = "base") -> None:
    """Regenerate a SYNTHETIC replay session (deterministic)."""
    from trader.data.synthetic import write_session

    m = write_session(REPO_ROOT / "research" / "sessions" / f"synthetic_{variant}_v1", variant)
    _echo(f"wrote {m.name}: {m.event_count} events sha256 {m.events_sha256}")


@data_app.command("kraken-specs")
def data_kraken_specs(
    dataset: Annotated[
        str, typer.Option(help="Manifest dataset holding Kraken AssetPairs metadata")
    ] = "kraken_btc_eth_usd_15m_recent_v1",
    out: Annotated[Path, typer.Option(help="Instrument YAML to write")] = REPO_ROOT
    / "configs"
    / "instruments"
    / "kraken_spot_usd.yaml",
) -> None:
    """Write Kraken spot instrument specs from public AssetPairs metadata (research use)."""
    import yaml

    from trader.data.manifest import load_manifest
    from trader.data.sources.public_crypto import kraken_pair_to_spec

    ds = load_manifest(REPO_ROOT / "research" / "data_manifest.json").dataset(dataset)
    pairs = ds.source_metadata.get("asset_pairs")
    if not pairs or ds.retrieved_at is None:
        _fail(f"{dataset} has no AssetPairs metadata; run ingest-history first", 2)
        return
    specs = [kraken_pair_to_spec(k, v, as_of=ds.retrieved_at) for k, v in sorted(pairs.items())]
    header = (
        "# GENERATED by `trader data kraken-specs` from Kraken public AssetPairs metadata recorded in\n"
        f"# research/data_manifest.json ({dataset}). Increments and minimums are DOCUMENTED, but\n"
        "# tradable=false: no eligible account has been certified. Re-fetch before any live use.\n"
    )
    out.write_text(
        header + yaml.safe_dump(specs, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
        newline="\n",
    )
    _echo(f"wrote {len(specs)} specs to {out.relative_to(REPO_ROOT).as_posix()}")


# --------------------------------------------------------------------------- market feed
@market_app.command("refresh")
def market_refresh(
    group: Annotated[
        list[str] | None, typer.Option("--group", help="Only this group (repeatable)")
    ] = None,
) -> None:
    """Fetch every watchlist group once into data/market/ (exit 1 if a group failed)."""
    from trader.market.feed import FeedError, run_refresh

    try:
        code = run_refresh(groups=group, echo=_echo)
    except FeedError as exc:
        _refuse(str(exc))
    raise typer.Exit(code)


@market_app.command("watch")
def market_watch(
    every: Annotated[
        int, typer.Option("--every", min=1, max=60, help="Minutes between refreshes")
    ] = 5,
) -> None:
    """Refresh now and then every N minutes until stopped (Ctrl+C)."""
    from trader.market.feed import run_watch

    raise typer.Exit(run_watch(every_minutes=every, echo=_echo))


# --------------------------------------------------------------------------- intel feed
@intel_app.command("refresh")
def intel_refresh(
    source: Annotated[
        list[str] | None,
        typer.Option("--source", help="Run exactly this source now, due or not (repeatable)"),
    ] = None,
) -> None:
    """Run every due source once into data/intel/ (exit 1 if a source failed)."""
    from trader.intel.feed import FeedError, run_refresh

    try:
        code = run_refresh(only=source, echo=_echo)
    except FeedError as exc:
        _refuse(str(exc))
    raise typer.Exit(code)


@intel_app.command("watch")
def intel_watch() -> None:
    """Refresh now and then at each source's cadence until stopped (Ctrl+C)."""
    from trader.intel.feed import run_watch as run_intel_watch

    raise typer.Exit(run_intel_watch(echo=_echo))


@intel_app.command("brief")
def intel_brief(
    symbols: Annotated[
        str | None,
        typer.Option("--symbols", help="Comma-separated symbols or ids, e.g. NVDA,BTC,GOLD"),
    ] = None,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=30, help="Headlines per instrument")
    ] = 5,
) -> None:
    """Print what the feed knows now, per instrument: headlines as quoted, untrusted data."""
    from trader.intel.brief import BriefError, run_brief

    try:
        code = run_brief(symbols=symbols, limit=limit, echo=_echo)
    except BriefError as exc:
        _refuse(str(exc))
    raise typer.Exit(code)


@intel_app.command("label")
def intel_label(
    target: Annotated[str, typer.Option("--target", help="Instrument id, e.g. stocks:NVDA")],
    day: Annotated[str, typer.Option("--date", help="Session date YYYY-MM-DD (today or ahead)")],
    catalyst: Annotated[
        str,
        typer.Option(
            "--catalyst",
            help="earnings, analyst, corporate, product, legal, sector, macro, none or unclear",
        ),
    ],
    direction: Annotated[str, typer.Option("--direction", help="up, down or unclear")],
    confidence: Annotated[str, typer.Option("--confidence", help="low, medium or high")],
    evidence: Annotated[
        list[str],
        typer.Option("--evidence", help="A reference the brief printed (repeatable)"),
    ],
    labeller: Annotated[str, typer.Option("--labeller", help="Who judged, e.g. claude-fable-5")],
    note: Annotated[str, typer.Option("--note", help="One line: what the label rests on")] = "",
) -> None:
    """Record why an instrument is moving, before its session (ADR-0051).

    A catalyst label is a judgement of what `trader intel brief` shows, stored with the time it
    was first held. Information only: nothing that decides a trade reads it.
    """
    from trader.intel.labels import LabelError, run_label

    try:
        code = run_label(
            target=target,
            session=day,
            catalyst=catalyst,
            direction=direction,
            confidence=confidence,
            evidence=evidence,
            labeller=labeller,
            note=note,
            echo=_echo,
        )
    except LabelError as exc:
        _refuse(str(exc))
    raise typer.Exit(code)


@intel_app.command("labels")
def intel_labels(
    day: Annotated[
        str | None, typer.Option("--date", help="Only this session (YYYY-MM-DD)")
    ] = None,
) -> None:
    """List the stored catalyst labels, the latest revision of each."""
    from trader.intel.labels import LabelError, run_labels

    try:
        code = run_labels(session=day, echo=_echo)
    except LabelError as exc:
        _refuse(str(exc))
    raise typer.Exit(code)


# --------------------------------------------------------------------------- research

StudyOpt = Annotated[Path, typer.Option(help="Study configuration")]
ModelStudyOpt = Annotated[Path, typer.Option(help="Model study configuration (kind: model_study)")]
RegistryOpt = Annotated[Path, typer.Option(help="Experiment registry (JSONL)")]
DEFAULT_STUDY = REPO_ROOT / "configs" / "research" / "crypto_baseline_v1.yaml"
DEFAULT_MODEL_STUDY = REPO_ROOT / "configs" / "research" / "crypto_filter_v1.yaml"
DEFAULT_REGISTRY = REPO_ROOT / "research" / "experiment_registry.jsonl"


def default_report_path(study_id: str) -> Path:
    """``research/reports/<study_id>_report.md``, or the roadmap artifact's name for the first
    crypto baseline (``baseline_report.md``) and the first model study (``model_report.md``)."""
    from trader.research.markdown import report_file_name

    return REPO_ROOT / "research" / "reports" / report_file_name(study_id)


def _manifest() -> object:
    from trader.data.manifest import load_manifest

    return load_manifest(REPO_ROOT / "research" / "data_manifest.json")


def _is_model_study(study: Path) -> bool:
    from trader.config.policy import load_yaml_strict
    from trader.research.model_study import is_model_study

    return is_model_study(load_yaml_strict(study))


def _is_cfd_study(study: Path) -> bool:
    from trader.research.cfd_study import is_cfd_study

    return is_cfd_study(study)


def _cfd_study_context(study: Path) -> object:
    from trader.data.manifest import DataManifest
    from trader.research.cfd_study import CfdStudyError, load_cfd_study, open_cfd_study

    manifest = _manifest()
    assert isinstance(manifest, DataManifest)
    try:
        return open_cfd_study(REPO_ROOT, load_cfd_study(study), manifest)
    except (CfdStudyError, KeyError) as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises


def _is_cfd_filter_study(study: Path) -> bool:
    from trader.research.cfd_filter_study import is_cfd_filter_study

    return is_cfd_filter_study(study)


def _cfd_filter_study_context(study: Path) -> object:
    from trader.data.manifest import DataManifest
    from trader.research.cfd_filter_study import (
        CfdFilterStudyError,
        load_cfd_filter_study,
        open_cfd_filter_study,
    )

    manifest = _manifest()
    assert isinstance(manifest, DataManifest)
    try:
        return open_cfd_filter_study(REPO_ROOT, load_cfd_filter_study(study), manifest)
    except (CfdFilterStudyError, KeyError) as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises


def _study_context(study: Path) -> object:
    from trader.data.manifest import DataManifest
    from trader.research.study import load_study, open_study

    if _is_model_study(study):
        _fail(f"{study.name} is a model study; use `trader research models`", 2)
    if _is_cfd_study(study):
        _fail(f"{study.name} is a CFD study; use `trader research cfd`", 2)
    if _is_cfd_filter_study(study):
        _fail(f"{study.name} is a CFD filter study; use `trader research cfd-filter`", 2)
    manifest = _manifest()
    assert isinstance(manifest, DataManifest)
    return open_study(REPO_ROOT, load_study(study), manifest)


def _model_study_context(study: Path) -> object:
    from trader.data.manifest import DataManifest
    from trader.research.model_study import ModelStudyError, load_model_study, open_model_study

    manifest = _manifest()
    assert isinstance(manifest, DataManifest)
    try:
        return open_model_study(REPO_ROOT, load_model_study(study), manifest)
    except ModelStudyError as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises


def _parse_utc(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        _fail(f"{value!r}: give a UTC time with an offset, e.g. 2025-01-01T00:00:00Z", 2)
    return parsed.astimezone(UTC)


@research_app.command("register")
def research_register(
    study: StudyOpt = DEFAULT_STUDY, registry: RegistryOpt = DEFAULT_REGISTRY
) -> None:
    """Register every trial of a study (before any run). Counts against the trial budget.

    Accepts a baseline study, a model study (``kind: model_study``), a CFD study
    (``kind: cfd_study``, ADR-0052) or a CFD filter study (``kind: cfd_filter_study``); a model
    or filter trial's registration binds the base rule, the filter recipe and the training
    library versions.
    """
    from trader.research.cfd_filter_study import (
        CfdFilterStudyContext,
        build_cfd_filter_registration,
    )
    from trader.research.cfd_study import CfdStudyContext, build_cfd_registration
    from trader.research.model_study import (
        ModelStudyContext,
        ModelStudyError,
        build_model_registration,
    )
    from trader.research.registry import ExperimentRegistry, TrialRegistration
    from trader.research.study import StudyContext, UnknownCostBasisError, build_registration

    registrations: list[TrialRegistration]
    try:  # every trial is checked before the first one is written
        if _is_cfd_study(study):
            cctx = _cfd_study_context(study)
            assert isinstance(cctx, CfdStudyContext)
            registrations = [build_cfd_registration(cctx, t) for t in cctx.config.trials]
        elif _is_cfd_filter_study(study):
            fctx = _cfd_filter_study_context(study)
            assert isinstance(fctx, CfdFilterStudyContext)
            registrations = [build_cfd_filter_registration(fctx, t) for t in fctx.config.trials]
        elif _is_model_study(study):
            mctx = _model_study_context(study)
            assert isinstance(mctx, ModelStudyContext)
            registrations = [build_model_registration(mctx, t) for t in mctx.config.trials]
        else:
            ctx = _study_context(study)
            assert isinstance(ctx, StudyContext)
            registrations = [build_registration(ctx, trial) for trial in ctx.config.trials]
    except (UnknownCostBasisError, ModelStudyError) as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises
    reg = ExperimentRegistry(registry)
    for registration in registrations:
        added = reg.register(registration, _now())
        _echo(
            f"{registration.trial_id}: "
            f"{'registered' if added else 'already registered (identical)'}"
        )
    _echo(f"budget: {len(reg.registrations())}/{reg.budget()} configurations registered")


@research_app.command("baseline")
def research_baseline(
    study: StudyOpt = DEFAULT_STUDY,
    registry: RegistryOpt = DEFAULT_REGISTRY,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Report path (default: research/reports/<study_id>_report.md; "
            "research/reports/baseline_report.md for crypto_baseline_v1)"
        ),
    ] = None,
) -> None:
    """Run a registered baseline study on its development period and write the report."""
    from trader.data.manifest import load_manifest
    from trader.research.registry import ExperimentRegistry
    from trader.research.report import render_baseline_report
    from trader.research.study import (
        StudyContext,
        UnknownCostBasisError,
        require_known_costs,
        result_digest,
        run_baseline_study,
    )

    ctx = _study_context(study)
    assert isinstance(ctx, StudyContext)
    try:
        require_known_costs(ctx)
    except UnknownCostBasisError as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises
    out = out if out is not None else default_report_path(ctx.config.study_id)
    reg = ExperimentRegistry(registry)
    result = run_baseline_study(ctx, reg, progress=lambda m: _echo(f"  running {m}"))
    digest = result_digest(result)
    run_dir = REPO_ROOT / "runs" / "research" / ctx.config.study_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = load_manifest(REPO_ROOT / "research" / "data_manifest.json")
    stocks = manifest.dataset("alpaca_us_equities_5m_v1")
    report = render_baseline_report(
        result,
        generated_at=_now().strftime("%Y-%m-%d %H:%M UTC"),
        digest=digest,
        stock_status={
            "dataset": stocks.name,
            "status": stocks.status,
            "detail": stocks.blocker or stocks.notes,
        },
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(report, encoding="utf-8", newline="\n")
    run_id = f"{ctx.config.study_id}-{digest[:12]}"
    for tid, trial in result["trials"].items():
        base = trial["views"]["base"]
        verdict = "NEGATIVE_NET" if base["net_pnl_usd"] <= 0 else "POSITIVE_UNCORRECTED"
        reg.record_result(
            tid,
            run_id=run_id,
            result_hash=base["result_hash"],
            verdict=verdict,
            summary={
                "trades": str(base["trades"]),
                "net_pnl_usd": f"{base['net_pnl_usd']:.4f}",
                "stressed_net_pnl_usd": f"{trial['views']['stressed']['net_pnl_usd']:.4f}",
                "zero_fee_net_pnl_usd": f"{trial['views']['diagnostic']['net_pnl_usd']:.4f}",
                "g2_study_status": result["g2"]["status"],
            },
            now=_now(),
        )
        _echo(f"{tid}: trades {base['trades']}, net {base['net_pnl_usd']:.2f} USD -> {verdict}")
    wf = result["walk_forward"]["summary"]
    _echo(
        f"walk-forward: {wf['trades']} trades, net {wf['net_pnl_usd']:.2f} USD over "
        f"{wf['sessions']} sessions; G2 {result['g2']['status']}"
    )
    _echo(
        f"report: {out.relative_to(REPO_ROOT).as_posix()}  results: runs/research/{ctx.config.study_id}/results.json"
    )


@research_app.command("cfd")
def research_cfd(
    study: StudyOpt,
    registry: RegistryOpt = DEFAULT_REGISTRY,
    out: Annotated[
        Path | None,
        typer.Option(help="Report path (default: research/reports/<study_id>_report.md)"),
    ] = None,
) -> None:
    """Run a registered CFD study (``kind: cfd_study``, ADR-0052) on the broker's own quotes and
    write its results and report."""
    from trader.research.cfd_report import render_cfd_report
    from trader.research.cfd_study import CfdStudyContext, promotion, run_cfd_study, verdict
    from trader.research.registry import ExperimentRegistry, RegistryError
    from trader.research.study import result_digest

    if not _is_cfd_study(study):
        _fail(f"{study.name} is not a CFD study (kind: cfd_study)", 2)
    ctx = _cfd_study_context(study)
    assert isinstance(ctx, CfdStudyContext)
    reg = ExperimentRegistry(registry)
    try:
        result = run_cfd_study(ctx, reg, progress=lambda m: _echo(f"  running {m}"))
    except RegistryError as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises
    digest = result_digest(result)
    run_dir = REPO_ROOT / "runs" / "research" / ctx.config.study_id
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "results.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    out = out if out is not None else default_report_path(ctx.config.study_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    report = render_cfd_report(
        result, generated_at=_now().strftime("%Y-%m-%d %H:%M UTC"), digest=digest
    )
    out.write_text(report, encoding="utf-8", newline="\n")
    run_id = f"{ctx.config.study_id}-{digest[:12]}"
    for tid, trial in result["trials"].items():
        base = trial["views"]["base"]
        outcome = verdict(result, tid)
        reg.record_result(
            tid,
            run_id=run_id,
            result_hash=base["result_hash"],
            verdict=outcome,
            summary={
                "trades": str(base["trades"]),
                "net_pnl_usd": f"{base['net_pnl_usd']:.4f}",
                "stressed_net_pnl_usd": f"{trial['views']['stressed']['net_pnl_usd']:.4f}",
                "mid_no_financing_net_pnl_usd": f"{trial['views']['diagnostic']['net_pnl_usd']:.4f}",
                "financing_usd": f"{base['financing_usd']:.4f}",
                "walk_forward_net_pnl_usd": f"{result['walk_forward']['summary']['net_pnl_usd']:.4f}",
                "g2_study_status": result["g2"]["status"],
                "practice": "TRADE" if promotion(result, tid)["trade_in_practice"] else "NO_TRADE",
            },
            now=_now(),
        )
        _echo(f"{tid}: trades {base['trades']}, net {base['net_pnl_usd']:.2f} USD -> {outcome}")
    wf = result["walk_forward"]["summary"]
    _echo(
        f"walk-forward: {wf['trades']} trades, net {wf['net_pnl_usd']:.2f} USD over "
        f"{wf['sessions']} sessions; G2 {result['g2']['status']}"
    )
    _echo(
        f"report: {out.relative_to(REPO_ROOT).as_posix()}  results: runs/research/{ctx.config.study_id}/results.json"
    )


@research_app.command("cfd-filter")
def research_cfd_filter(
    study: StudyOpt,
    registry: RegistryOpt = DEFAULT_REGISTRY,
    out: Annotated[
        Path | None,
        typer.Option(help="Report path (default: research/reports/<study_id>_report.md)"),
    ] = None,
) -> None:
    """Run a registered CFD filter study (``kind: cfd_filter_study``, T-0026): train a veto
    filter per fold on a frozen CFD rule, replay every test window for the rule alone, the rule
    with the filter and both controls in three cost views, and write
    runs/research/<study_id>/results.json, the bundles, the report and one RESULT event per
    trial."""
    from trader.research.cfd_filter_report import render_cfd_filter_report
    from trader.research.cfd_filter_study import (
        CfdFilterStudyContext,
        record_filter_results,
        run_cfd_filter_study,
        save_filter_outputs,
    )
    from trader.research.registry import ExperimentRegistry, RegistryError
    from trader.research.study import result_digest

    if not _is_cfd_filter_study(study):
        _fail(f"{study.name} is not a CFD filter study (kind: cfd_filter_study)", 2)
    ctx = _cfd_filter_study_context(study)
    assert isinstance(ctx, CfdFilterStudyContext)
    reg = ExperimentRegistry(registry)
    try:
        run = run_cfd_filter_study(ctx, reg, progress=lambda m: _echo(f"  running {m}"))
    except RegistryError as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises
    result = run.result
    digest = result_digest(result)
    results_path = save_filter_outputs(run, REPO_ROOT / "runs" / "research" / ctx.config.study_id)
    out = out if out is not None else default_report_path(ctx.config.study_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_cfd_filter_report(
            result, generated_at=_now().strftime("%Y-%m-%d %H:%M UTC"), digest=digest
        ),
        encoding="utf-8",
        newline="\n",
    )
    for tid, outcome in record_filter_results(reg, result, digest=digest, now=_now()):
        trial = result["trials"][tid]
        rule = trial["variants"]["rule_only"]["views"]["base"]
        _echo(
            f"{tid}: filter net {trial['views']['base']['net_pnl_usd']:.2f} USD vs rule alone "
            f"{rule['net_pnl_usd']:.2f} USD; works: {trial['comparison']['works']}; "
            f"practice: {trial['promotion']['trade_in_practice']} -> {outcome}"
        )
    wf = result["walk_forward"]["summary"]
    _echo(
        f"walk-forward: {wf['trades']} trades, net {wf['net_pnl_usd']:.2f} USD over "
        f"{wf['sessions']} sessions; G2 {result['g2']['status']}"
    )
    _echo(f"report: {out.as_posix()}  results: {results_path.as_posix()}")


@research_app.command("models")
def research_models(
    study: ModelStudyOpt = DEFAULT_MODEL_STUDY,
    registry: RegistryOpt = DEFAULT_REGISTRY,
    out: Annotated[
        Path | None,
        typer.Option(
            help="Report path (default: research/reports/<study_id>_report.md; "
            "research/reports/model_report.md for crypto_filter_v1)"
        ),
    ] = None,
) -> None:
    """Run a registered model study (M3) on its development period and write the report.

    Trains one candidate filter per walk-forward fold, replays every test window for the rule,
    the rule with the filter and both controls, writes runs/research/<study_id>/model_results.json
    and the bundles, the report, and one RESULT event per trial in the registry.
    """
    from trader.research.model_report import render_model_report
    from trader.research.model_study import (
        ModelStudyContext,
        ModelStudyError,
        record_model_results,
        run_model_study,
        save_model_outputs,
    )
    from trader.research.registry import ExperimentRegistry, RegistryError
    from trader.research.study import UnknownCostBasisError, result_digest

    ctx = _model_study_context(study)
    assert isinstance(ctx, ModelStudyContext)
    reg = ExperimentRegistry(registry)
    try:
        run = run_model_study(ctx, reg, progress=lambda m: _echo(f"  running {m}"))
    except (RegistryError, UnknownCostBasisError, ModelStudyError) as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises
    result = run.result
    digest = result_digest(result)
    run_dir = REPO_ROOT / "runs" / "research" / ctx.config.study_id
    results_path = save_model_outputs(run, run_dir)
    out = out if out is not None else default_report_path(ctx.config.study_id)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        render_model_report(
            result, generated_at=_now().strftime("%Y-%m-%d %H:%M UTC"), digest=digest
        ),
        encoding="utf-8",
        newline="\n",
    )
    for tid, verdict in record_model_results(reg, result, digest=digest, now=_now()):
        variants = result["trials"][tid]["variants"]
        _echo(
            f"{tid}: filter net {variants['model_filter']['net_pnl_usd']:.2f} USD vs rule "
            f"{variants['rule_only']['net_pnl_usd']:.2f} USD -> {verdict}"
        )
    wf = result["walk_forward"]["summary"]
    _echo(
        f"walk-forward: {wf['trades']} trades, net {wf['net_pnl_usd']:.2f} USD over "
        f"{wf['sessions']} sessions; G2 {result['g2']['status']}"
    )
    _echo(f"report: {out.as_posix()}  results: {results_path.as_posix()}")


@research_app.command("labels")
def research_labels(
    study: ModelStudyOpt,
    out: Annotated[Path, typer.Option(help="Parquet file to write (keep it under data/)")],
    trial: Annotated[
        str | None, typer.Option(help="Trial ID (default: the study's first trial)")
    ] = None,
    start: Annotated[
        str | None, typer.Option(help="Window start, UTC ISO time (default: development start)")
    ] = None,
    end: Annotated[
        str | None, typer.Option(help="Window end, UTC ISO time (default: holdout start)")
    ] = None,
    registry: RegistryOpt = DEFAULT_REGISTRY,
) -> None:
    """Exploration helper: label every candidate of a registered model trial's rule.

    The window must lie in the development period; the locked final test period is refused
    before anything is read. Labels carry outcomes, so the trial must be registered first.
    """
    from trader.research.model_study import (
        ModelStudyContext,
        ModelStudyError,
        build_model_registration,
        build_trial_labels,
    )
    from trader.research.registry import ExperimentRegistry, RegistryError
    from trader.research.splits import HoldoutLockedError, guard_window
    from trader.research.study import UnknownCostBasisError

    ctx = _model_study_context(study)
    assert isinstance(ctx, ModelStudyContext)
    plan = ctx.base.config.splits
    lo = _parse_utc(start) if start else plan.development_start
    hi = _parse_utc(end) if end else plan.holdout_start
    try:
        guard_window(plan, lo, hi, unlocked=False)
        chosen = ctx.trial(trial) if trial else ctx.config.trials[0]
        ExperimentRegistry(registry).require_registered(build_model_registration(ctx, chosen))
        labels = build_trial_labels(ctx, chosen, start=lo, end=hi)
    except (HoldoutLockedError, RegistryError, ModelStudyError, UnknownCostBasisError) as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises
    labels.save(out)
    wins = sum(1 for r in labels.rows if r.win)
    _echo(
        f"{chosen.trial_id}: wrote {len(labels.rows)} labelled candidates ({wins} wins, features "
        f"{labels.feature_version}) to {out.as_posix()}"
    )


@research_app.command("status")
def research_status(registry: RegistryOpt = DEFAULT_REGISTRY) -> None:
    """List registered trials, their latest results and the remaining budget."""
    from trader.research.registry import ExperimentRegistry

    reg = ExperimentRegistry(registry)
    for tid, trial in sorted(reg.registrations().items()):
        results = reg.results(tid)
        last = results[-1]["payload"] if results else None
        outcome = f"{last['verdict']} net {last['summary']['net_pnl_usd']}" if last else "not run"
        _echo(f"{tid} {trial.study_id} {trial.family}: {outcome}")
    _echo(
        f"budget: {len(reg.registrations())}/{reg.budget()}; chain verified ({len(reg.events)} events)"
    )


@research_app.command("note")
def research_note(
    text: Annotated[str, typer.Option(help="Informal change or observation to record")],
    registry: RegistryOpt = DEFAULT_REGISTRY,
) -> None:
    """Record an informal strategy change or observation in the registry."""
    from trader.research.registry import ExperimentRegistry

    ExperimentRegistry(registry).note(text, by=getpass.getuser(), now=_now())
    _echo("noted")


@research_app.command("decide-budget")
def research_decide_budget(
    new_budget: Annotated[int, typer.Option(help="New total number of registrable trials")],
    reason: Annotated[str, typer.Option(help="Written reason after reviewing the evidence")],
    registry: RegistryOpt = DEFAULT_REGISTRY,
) -> None:
    """Owner decision: change the experiment budget (recorded, never silent)."""
    from trader.research.registry import ExperimentRegistry

    reg = ExperimentRegistry(registry)
    reg.decide_budget(new_budget, reason=reason, by=getpass.getuser(), now=_now())
    _echo(f"budget is now {reg.budget()} ({len(reg.registrations())} registered)")


@research_app.command("unlock-holdout")
def research_unlock_holdout(
    study_id: Annotated[str, typer.Option(help="Study whose final test period is opened")],
    reason: Annotated[str, typer.Option(help="Why the locked period is being used now")],
    registry: RegistryOpt = DEFAULT_REGISTRY,
) -> None:
    """Owner decision: open a study's locked final test period. It is development data afterwards."""
    from trader.research.registry import ExperimentRegistry

    ExperimentRegistry(registry).unlock_holdout(
        study_id, reason=reason, by=getpass.getuser(), now=_now()
    )
    _echo(f"{study_id}: final test period unlocked and recorded; it can never be locked again")


# --------------------------------------------------------------------------- secrets (ADR-0026/0032/0036)
SecretName = Annotated[
    str, typer.Argument(help="Secret name: alpaca-paper (key pair) or capital-demo (plain)")
]
ProfileOpt = Annotated[
    str, typer.Option("--profile", "-p", help="Profile name (lowercase, digits, '-' or '_')")
]


def _service(name: str) -> str:
    from trader.config.secrets import SecretsError, service_name

    try:
        return service_name(name)
    except SecretsError as exc:
        _fail(str(exc), 2)
        raise  # pragma: no cover - _fail raises


def _alpaca_only(service: str, action: str) -> None:
    """Profiles, the keys folder and --verify exist only for the Alpaca key pair (ADR-0036)."""
    from trader.config.secrets import SecretsError, require_alpaca_kind

    try:
        require_alpaca_kind(service, action)
    except SecretsError as exc:
        _fail(str(exc), 2)


@secrets_app.command("set")
def secrets_set(name: SecretName, profile: ProfileOpt = "default") -> None:
    """Store keys in the OS keyring. Input is hidden; nothing is echoed.

    alpaca-paper: stores a key pair as a profile and makes it active. A plain service such as
    capital-demo: prompts for each of its fields; it has no profiles.
    """
    from trader.config.secrets import SecretsError, spec_for, store_alpaca, store_plain

    service = _service(name)
    spec = spec_for(service)
    if spec.kind == "plain":
        if profile != "default":
            _alpaca_only(service, "--profile")
        _echo(f"Storing keys for {service} in the OS keyring. Input is hidden.")
        values = {f.user: getpass.getpass(f"{f.prompt}: ") for f in spec.fields}
        try:
            store_plain(service, values)
        except SecretsError as exc:
            _fail(str(exc), 2)
        _echo(f"{service}: stored {len(spec.fields)} fields. Check: trader secrets status {name}")
        return
    _echo(f"Storing keys for {service} (profile {profile}) in the OS keyring. Input is hidden.")
    key_id = getpass.getpass("API key ID: ")
    secret = getpass.getpass("API secret key: ")
    try:
        warning = store_alpaca(service, key_id, secret, profile=profile)
    except SecretsError as exc:
        _fail(str(exc), 2)
    if warning:
        _echo(warning)
    _echo(f"{service}: stored as profile {profile} (active). Check: trader secrets status --verify")


@secrets_app.command("status")
def secrets_status(
    name: SecretName = "alpaca-paper",
    verify: Annotated[
        bool, typer.Option("--verify", help="Also call Alpaca's paper account and data APIs")
    ] = False,
) -> None:
    """Show whether keys are present (never their values); optionally verify them with Alpaca."""
    from trader.config.secrets import SecretsError, plain_sources, require_alpaca, spec_for, status

    service = _service(name)
    if spec_for(service).kind == "plain":
        if verify:
            _alpaca_only(service, "--verify")
        try:
            sources = plain_sources(service)
        except SecretsError as exc:
            _fail(str(exc), 2)
        _echo(
            f"{service}: "
            + ", ".join(f"{f.user} {f'from {src}' if src else 'missing'}" for f, src in sources)
        )
        missing = [f.env for f, src in sources if src is None]
        if missing:
            _fail(
                f"owner action: add {', '.join(f'{m}=...' for m in missing)} to the repository "
                f".env, or run `uv run trader secrets set {name}` (hidden prompt)",
                1,
            )
        _echo(f"{name}: OK")
        return
    try:
        st = status(service)
    except SecretsError as exc:
        _fail(str(exc), 2)
    _echo(
        f"{service}: key_id {'present' if st.key_id_present else 'missing'}, "
        f"secret {'present' if st.secret_present else 'missing'}, "
        f"profile {st.active_profile or '-'}"
        + (" (session environment variables override the keyring)" if st.env_override else "")
    )
    if not st.ok:
        _fail(
            "owner action: run `uv run trader secrets set alpaca-paper`, or drop a key file into "
            "the keys folder (`uv run trader secrets folder`) and run `uv run trader secrets import`",
            1,
        )
    if not verify:
        _echo(f"{name}: OK")
        return
    from trader.data.sources.http import TransportError, UrllibTransport

    headers = require_alpaca(service).headers()
    transport = UrllibTransport(max_attempts=2)
    try:
        account = transport.get_json("https://paper-api.alpaca.markets/v2/account", None, headers)
        _echo(f"paper account: reachable, status {account.get('status', 'unknown')}")
        bars = transport.get_json(
            "https://data.alpaca.markets/v2/stocks/bars",
            {"symbols": "AAPL", "timeframe": "1Day", "limit": "1", "feed": "iex"},
            headers,
        )
        _echo(f"market data (IEX): reachable, {len(bars.get('bars', {}).get('AAPL', []))} bar(s)")
    except TransportError as exc:
        _fail(f"verification failed: {exc}", 1)
    _echo(f"{name}: OK (verified)")


@secrets_app.command("list")
def secrets_list(name: SecretName = "alpaca-paper") -> None:
    """List stored profiles (names only) and which one is active."""
    from trader.config.secrets import SecretsError, profiles

    service = _service(name)
    _alpaca_only(service, "profiles")
    try:
        names, active = profiles(service)
    except SecretsError as exc:
        _fail(str(exc), 2)
    if not names:
        _echo(f"{service}: no stored profiles")
        return
    for n in names:
        _echo(f"{'*' if n == active else ' '} {n}")


@secrets_app.command("use")
def secrets_use(name: SecretName, profile: Annotated[str, typer.Argument()]) -> None:
    """Switch the active key pair to a stored profile."""
    from trader.config.secrets import SecretsError, use_profile

    service = _service(name)
    _alpaca_only(service, "profiles")
    try:
        use_profile(service, profile)
    except SecretsError as exc:
        _fail(str(exc), 2)
    _echo(f"{service}: active profile is now {profile}")


@secrets_app.command("folder")
def secrets_folder() -> None:
    """Create (if needed) and show the keys inbox folder, outside the repository."""
    from trader.config.secrets import ensure_keys_dir

    _echo(f"keys folder: {ensure_keys_dir()}")
    _echo("Put <profile>.env files there (see README.txt), then run `trader secrets import`.")


@secrets_app.command("import")
def secrets_import(
    name: SecretName = "alpaca-paper",
    keep: Annotated[
        bool, typer.Option("--keep", help="Leave the key files in the folder after importing")
    ] = False,
    use: Annotated[
        str | None, typer.Option("--use", help="Profile to make active after importing")
    ] = None,
    folder: Annotated[
        Path | None, typer.Option("--dir", help="Keys folder (default: trader secrets folder)")
    ] = None,
) -> None:
    """Move every <profile>.env from the keys folder into the OS keyring (files deleted)."""
    from trader.config.secrets import SecretsError, ensure_keys_dir, import_key_files, profiles

    service = _service(name)
    _alpaca_only(service, "the keys folder")
    directory = ensure_keys_dir(folder)
    try:
        imported = import_key_files(service, directory, keep=keep, use=use)
        _, active = profiles(service)
    except SecretsError as exc:
        _fail(str(exc), 2)
    if not imported:
        _echo(f"no <profile>.env files in {directory}")
    for item in imported:
        state = "file deleted" if item.deleted else "file kept"
        _echo(f"imported profile {item.profile} from {item.file} ({state})")
        if item.warning:
            _echo(f"  {item.warning}")
    _echo(f"{service}: active profile {active or '-'}")


@secrets_app.command(
    "exec", context_settings={"allow_extra_args": True, "ignore_unknown_options": True}
)
def secrets_exec(ctx: typer.Context, name: SecretName) -> None:
    """Run a local tool with the stored keys in its environment: trader secrets exec NAME -- CMD.

    Used to launch MCP servers without keys in any config file (ADR-0032, ADR-0036). For Alpaca,
    paper trading is forced on; a plain service gets its forced safety settings (for capital-demo:
    demo only, trading off). The OS trust store is passed as SSL_CERT_FILE, and nothing is printed
    to stdout (an MCP server speaks JSON-RPC there). `.mcp.json` calls the same code through the
    lighter `python -m trader.config.secrets_exec` (ADR-0038).
    """
    from trader.config.secrets import SecretsError
    from trader.config.secrets_exec import ExecUsageError, run

    try:
        code = run(name, ctx.args)
    except (ExecUsageError, SecretsError) as exc:
        _refuse(str(exc), 2)
    raise typer.Exit(code)


@secrets_app.command("delete")
def secrets_delete(
    name: SecretName,
    profile: Annotated[
        str | None, typer.Option("--profile", "-p", help="Remove this stored profile")
    ] = None,
) -> None:
    """Clear the active key pair, or remove one stored profile with --profile."""
    from trader.config.secrets import SecretsError, delete

    service = _service(name)
    try:
        removed = delete(service, profile)
    except SecretsError as exc:
        _fail(str(exc), 2)
    _echo(f"{service}: removed {removed} entr{'y' if removed == 1 else 'ies'}")


# --------------------------------------------------------------------------- practice journal
VenueOpt = Annotated[
    str,
    typer.Option("--venue", help="Practice sleeve: capital-demo (CFDs) or crypto-cfd (crypto)"),
]


@journal_app.command("add")
def journal_add(
    kind: Annotated[str, typer.Argument(help="PLAN, OPEN, AMEND, CLOSE, CANCEL, NOTE or REVIEW")],
    payload: Annotated[str, typer.Option("--json", help="The event fields as a JSON object")],
    venue: VenueOpt = "capital-demo",
    actor: Annotated[str, typer.Option(help="Who acted: claude or owner")] = "claude",
    evidence: Annotated[
        list[str] | None,
        typer.Option(
            "--evidence",
            help="PLAN only, repeatable: an intel reference from `trader intel brief`, e.g. "
            "news:alpaca_news:63087291 or snap:stocks:NVDA@2026-11-10T15:01:07Z",
        ),
    ] = None,
) -> None:
    """Validate and append one event to a sleeve's practice journal (hash-chained)."""
    from trader.ops.clock import SystemClock
    from trader.practice.journal import JournalError, PracticeJournal

    try:
        fields = json.loads(payload)
        if not isinstance(fields, dict):
            raise JournalError("--json must be a JSON object")
        if evidence:
            if kind.upper() != "PLAN":
                raise JournalError("--evidence belongs to a PLAN")
            fields["evidence"] = [*fields.get("evidence", []), *evidence]
        journal = PracticeJournal(venue=venue)
        record = journal.append(kind.upper(), fields, SystemClock().now(), actor=actor)
    except (JournalError, ValueError) as exc:
        _refuse(f"journal entry refused: {exc}")
    p = record["payload"]
    _echo(f"journal #{record['seq']} {record['event']} {p.get('trade_id', '')} recorded")
    if record["event"] == "PLAN":
        _echo(f"planned risk USD {p['risk_usd']} on sleeve equity USD {p['sleeve_equity_usd']}")
    _write_sleeves()


@journal_app.command("verify")
def journal_verify(venue: VenueOpt = "capital-demo") -> None:
    """Check a journal's hash chain and print its totals."""
    from trader.practice.journal import JournalError, PracticeJournal

    try:
        j = PracticeJournal(venue=venue)
    except JournalError as exc:
        _refuse(str(exc))
    kinds: dict[str, int] = {}
    for e in j.events:
        kinds[e["event"]] = kinds.get(e["event"], 0) + 1
    _echo(f"journal {j.path.name}: {len(j.events)} events, chain OK; {kinds or 'empty'}")


@journal_app.command("open")
def journal_open(venue: VenueOpt = "capital-demo") -> None:
    """List the practice trades a journal shows as open."""
    from trader.practice.journal import PracticeJournal

    j = PracticeJournal(venue=venue)
    open_ids = j.open_trades()
    _echo(f"open practice trades: {len(open_ids)} of {j.rules.max_open_trades}")
    from trader.domain.types import iso_utc
    from trader.ops.clock import SystemClock

    now = SystemClock().now()
    for trade_id in open_ids:
        p = (j.plan(trade_id) or {"payload": {}})["payload"]
        terms = j.in_force(trade_id)
        due = j.due_at(trade_id)
        time_stop = "" if due is None else f" time stop {iso_utc(due)}"
        _echo(
            f"  {trade_id}: {p.get('direction')} {p.get('size')} {p.get('epic')} "
            f"entry {p.get('entry')} stop {terms['stop']} target {terms['target'] or '-'}"
            f"{time_stop}{' OVERDUE' if due is not None and due <= now else ''}"
        )


def _write_sleeves() -> dict[str, Any]:
    """runs/practice/sleeves.json: each practice sleeve's equity, for the console (ADR-0046)."""
    from trader.ops.clock import SystemClock
    from trader.practice.watch import write_sleeves

    return write_sleeves(SystemClock().now())


@journal_app.command("summary")
def journal_summary() -> None:
    """Print every practice sleeve's equity and write runs/practice/sleeves.json."""
    for name, row in _write_sleeves()["sleeves"].items():
        _echo(
            f"{name:<13} start USD {row['start_usd']} equity USD {row['equity_usd']} "
            f"closed {row['closed_trades']} (won {row['winning_trades']}) open {row['open_trades']}"
        )


@journal_app.command("scan")
def journal_scan(
    market_root: Annotated[
        Path | None,
        typer.Option(help="The market feed's folder (default: data/market)"),
    ] = None,
) -> None:
    """Read the index dip setup on the last completed daily bars and check every open practice
    trade against its stop, target and time stop (ADR-0049). Prints the readings and, on a
    signal, the trade that fits the sleeve's risk limit, and writes runs/practice/scan.json for
    the console. Places and journals nothing."""
    from trader.ops.clock import SystemClock
    from trader.practice import setups
    from trader.practice.watch import run_scan

    try:
        doc = run_scan(market_root, SystemClock().now())
    except (setups.SetupError, ValueError) as exc:
        _refuse(f"scan refused: {exc}")
    for line in setups.render(doc):
        _echo(line)


@journal_app.command("watch")
def journal_watch(
    every: Annotated[int, typer.Option(min=1, max=240, help="Minutes between scans")] = 15,
    market_root: Annotated[
        Path | None,
        typer.Option(help="The market feed's folder (default: data/market)"),
    ] = None,
) -> None:
    """Keep runs/practice/scan.json and sleeves.json current for the console: the practice scan
    every few minutes, until stopped (`npm run dev` starts it as `python -m trader.practice
    watch`). Places and journals nothing."""
    from trader.practice.watch import run_watch

    run_watch(every_minutes=every, market_root=market_root, echo=_echo)


def main() -> None:  # pragma: no cover - console entry
    app()


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
