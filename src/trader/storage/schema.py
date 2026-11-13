"""Runtime database schema (architecture.md §5 plus allocator, leadership and loss tables).

Uniqueness constraints that the safety design depends on:

* ``(account_id, client_order_id)`` on ``order_intents`` and ``broker_orders`` – one intent per
  client order ID; never a second ID for the same trade.
* ``(broker, account_id, execution_id)`` on ``fills`` – a fill is posted at most once.
* ``(broker, account_id, event_id)`` on ``broker_order_events`` – duplicate events are ignored.
* partial unique index ``uq_one_active_entry_reservation`` – at most one pending entry per
  portfolio across every track and account (ADR-0010).

Broker order versions are 64-bit (``BigInteger``, migration 0002): the Alpaca adapter uses the
order's ``updated_at`` in epoch microseconds, which overflows a 32-bit integer.
"""

from __future__ import annotations

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    text,
)

from trader.storage.types import ExactDecimal, UtcDateTime

metadata = MetaData(
    naming_convention={
        "ix": "ix_%(table_name)s_%(column_0_N_name)s",
        "uq": "uq_%(table_name)s_%(column_0_N_name)s",
        "ck": "ck_%(table_name)s_%(constraint_name)s",
        "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
        "pk": "pk_%(table_name)s",
    }
)

ID = String(64)

portfolios = Table(
    "portfolios",
    metadata,
    Column("portfolio_id", ID, primary_key=True),
    Column("base_currency", String(3), nullable=False),
    Column("initial_capital", ExactDecimal, nullable=False),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("created_at", UtcDateTime, nullable=False),
)

accounts = Table(
    "accounts",
    metadata,
    Column("account_id", ID, primary_key=True),
    Column("portfolio_id", ID, ForeignKey("portfolios.portfolio_id"), nullable=False),
    Column("alias", String(64), nullable=False),
    Column("track", String(16), nullable=False),
    Column("broker", String(32), nullable=False),
    Column("mode", String(8), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("rule_profile", String(32), nullable=False),
    Column("legal_type", String(32), nullable=False),
    Column("allocated_capital", ExactDecimal, nullable=False),
    Column("broker_cash_baseline", ExactDecimal, nullable=True),
    Column("version", Integer, nullable=False, server_default="0"),
    Column("created_at", UtcDateTime, nullable=False),
)

instruments = Table(
    "instruments",
    metadata,
    Column("instrument_id", String(96), primary_key=True),
    Column("track", String(16), nullable=False),
    Column("product_type", String(32), nullable=False),
    Column("spec_hash", String(64), nullable=False),
    Column("spec_json", Text, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
)

capability_profiles = Table(
    "capability_profiles",
    metadata,
    Column("profile_version", String(96), primary_key=True),
    Column("broker", String(32), nullable=False),
    Column("profile_hash", String(64), nullable=False),
    Column("profile_json", Text, nullable=False),
    Column("recorded_at", UtcDateTime, nullable=False),
)

policy_versions = Table(
    "policy_versions",
    metadata,
    Column("policy_hash", String(64), primary_key=True),
    Column("canonical_json", Text, nullable=False),
    Column("source", Text, nullable=False),
    Column("first_seen_at", UtcDateTime, nullable=False),
)

model_versions = Table(
    "model_versions",
    metadata,
    Column("model_hash", String(96), primary_key=True),
    Column("kind", String(32), nullable=False),
    Column("evidence", String(32), nullable=False),
    Column("description", Text, nullable=False),
    Column("registered_at", UtcDateTime, nullable=False),
)

runs = Table(
    "runs",
    metadata,
    Column("run_id", ID, primary_key=True),
    Column("mode", String(8), nullable=False),
    Column("scenario", String(64), nullable=False),
    Column("manifest_hash", String(64), nullable=False),
    Column("policy_hash", String(64), nullable=False),
    Column("started_at", UtcDateTime, nullable=False),
    Column("finished_at", UtcDateTime, nullable=True),
    Column("status", String(16), nullable=False),
    Column("decision_hash", String(64), nullable=True),
    Column("ledger_hash", String(64), nullable=True),
)

events = Table(
    "events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("feed", String(32), nullable=False),
    Column("instrument_id", String(96), nullable=False),
    Column("kind", String(16), nullable=False),
    Column("sequence", Integer, nullable=False),
    Column("correction_version", Integer, nullable=False),
    Column("event_time", UtcDateTime, nullable=False),
    Column("receipt_time", UtcDateTime, nullable=False),
    Column("payload_json", Text, nullable=False),
    UniqueConstraint("feed", "instrument_id", "sequence", "correction_version"),
)

bars = Table(
    "bars",
    metadata,
    Column("instrument_id", String(96), primary_key=True),
    Column("feed", String(32), primary_key=True),
    Column("minutes", Integer, primary_key=True),
    Column("start", UtcDateTime, primary_key=True),
    Column("end", UtcDateTime, nullable=False),
    Column("available_time", UtcDateTime, nullable=False),
    Column("open", ExactDecimal, nullable=False),
    Column("high", ExactDecimal, nullable=False),
    Column("low", ExactDecimal, nullable=False),
    Column("close", ExactDecimal, nullable=False),
    Column("volume", ExactDecimal, nullable=False),
    Column("trade_count", Integer, nullable=False),
    Column("vwap", ExactDecimal, nullable=False),
)

candidates = Table(
    "candidates",
    metadata,
    Column("candidate_id", ID, primary_key=True),
    Column("strategy_id", String(64), nullable=False),
    Column("instrument_id", String(96), nullable=False),
    Column("decision_time", UtcDateTime, nullable=False),
    Column("payload_json", Text, nullable=False),
)

risk_decisions = Table(
    "risk_decisions",
    metadata,
    Column("decision_id", ID, primary_key=True),
    Column("seq", Integer, nullable=False, unique=True),
    Column("candidate_id", ID, nullable=False),
    Column("portfolio_id", ID, nullable=False),
    Column("account_id", ID, nullable=False),
    Column("approved", Boolean, nullable=False),
    Column("reasons", Text, nullable=False),
    Column("decided_at", UtcDateTime, nullable=False),
    Column("policy_hash", String(64), nullable=False),
    Column("model_hash", String(96), nullable=False),
    Column("payload_json", Text, nullable=False),
)

order_intents = Table(
    "order_intents",
    metadata,
    Column("intent_id", ID, primary_key=True),
    Column("seq", Integer, nullable=False, unique=True),
    Column("portfolio_id", ID, nullable=False),
    Column("account_id", ID, ForeignKey("accounts.account_id"), nullable=False),
    Column("client_order_id", String(40), nullable=False),
    Column("instrument_id", String(96), nullable=False),
    Column("track", String(16), nullable=False),
    Column("side", String(4), nullable=False),
    Column("purpose", String(32), nullable=False),
    Column("order_type", String(8), nullable=False),
    Column("quantity", ExactDecimal, nullable=False),
    Column("limit_price", ExactDecimal, nullable=True),
    Column("time_in_force", String(4), nullable=False),
    Column("state", String(24), nullable=False),
    Column("decision_id", ID, nullable=True),
    Column("candidate_id", ID, nullable=True),
    Column("parent_intent_id", ID, nullable=True),
    Column("intent_hash", String(64), nullable=False),
    Column("plan_json", Text, nullable=False),
    Column("plan_digest", String(64), nullable=False),
    Column("valid_until", UtcDateTime, nullable=False),
    Column("entry_ttl_until", UtcDateTime, nullable=True),
    Column("broker_order_id", String(96), nullable=True),
    Column("cum_filled_qty", ExactDecimal, nullable=False),
    Column("final_cum_qty", ExactDecimal, nullable=True),
    Column("last_broker_version", BigInteger, nullable=False, server_default="-1"),
    Column("lookup_attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", UtcDateTime, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
    UniqueConstraint("account_id", "client_order_id"),
)
Index("ix_order_intents_state", order_intents.c.account_id, order_intents.c.state)

submission_outbox = Table(
    "submission_outbox",
    metadata,
    Column("outbox_id", ID, primary_key=True),
    Column("intent_id", ID, ForeignKey("order_intents.intent_id"), nullable=False, unique=True),
    Column("status", String(16), nullable=False),
    Column("fencing_token", Integer, nullable=True),
    Column("attempts", Integer, nullable=False, server_default="0"),
    Column("created_at", UtcDateTime, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
)

broker_orders = Table(
    "broker_orders",
    metadata,
    Column("account_id", ID, primary_key=True),
    Column("broker_order_id", String(96), primary_key=True),
    Column("broker", String(32), nullable=False),
    Column("client_order_id", String(40), nullable=False),
    Column("status", String(24), nullable=False),
    Column("quantity", ExactDecimal, nullable=False),
    Column("filled_qty", ExactDecimal, nullable=False),
    Column("broker_version", BigInteger, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
    UniqueConstraint("account_id", "client_order_id"),
)

broker_order_events = Table(
    "broker_order_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("broker", String(32), nullable=False),
    Column("account_id", ID, nullable=False),
    Column("event_id", String(128), nullable=False),
    Column("broker_order_id", String(96), nullable=False),
    Column("client_order_id", String(40), nullable=False),
    Column("status", String(24), nullable=False),
    Column("cum_filled_qty", ExactDecimal, nullable=False),
    Column("broker_version", BigInteger, nullable=False),
    Column("event_time", UtcDateTime, nullable=False),
    Column("received_at", UtcDateTime, nullable=False),
    Column("disposition", String(32), nullable=False),
    Column("raw_json", Text, nullable=False),
    UniqueConstraint("broker", "account_id", "event_id"),
)

fills = Table(
    "fills",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("broker", String(32), nullable=False),
    Column("account_id", ID, nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column("intent_id", ID, nullable=True),
    Column("broker_order_id", String(96), nullable=False),
    Column("client_order_id", String(40), nullable=False),
    Column("instrument_id", String(96), nullable=False),
    Column("side", String(4), nullable=False),
    Column("quantity", ExactDecimal, nullable=False),
    Column("price", ExactDecimal, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("fee", ExactDecimal, nullable=False),
    Column("fee_currency", String(3), nullable=False),
    Column("fee_status", String(8), nullable=False),
    Column("exec_time", UtcDateTime, nullable=False),
    Column("receipt_time", UtcDateTime, nullable=False),
    Column("realized_gross", ExactDecimal, nullable=True),
    Column("realized_net", ExactDecimal, nullable=True),
    UniqueConstraint("broker", "account_id", "execution_id"),
)

positions = Table(
    "positions",
    metadata,
    Column("account_id", ID, primary_key=True),
    Column("instrument_id", String(96), primary_key=True),
    Column("owner_track", String(16), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("quantity", ExactDecimal, nullable=False),
    Column("cost_basis", ExactDecimal, nullable=False),
    Column("open_entry_fees", ExactDecimal, nullable=False),
    Column("realized_gross", ExactDecimal, nullable=False),
    Column("realized_net", ExactDecimal, nullable=False),
    Column("opened_at", UtcDateTime, nullable=True),
    Column("updated_at", UtcDateTime, nullable=False),
)

cash_lots = Table(
    "cash_lots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("account_id", ID, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("amount", ExactDecimal, nullable=False),
    Column("trade_date", Date, nullable=False),
    Column("settles_on", Date, nullable=False),
    Column("source", String(160), nullable=False),
)

ledger_entries = Table(
    "ledger_entries",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("seq", Integer, nullable=False),
    Column("account_id", ID, nullable=False),
    Column("entry_time", UtcDateTime, nullable=False),
    Column("kind", String(32), nullable=False),
    Column("currency", String(3), nullable=False),
    Column("cash_delta", ExactDecimal, nullable=False),
    Column("instrument_id", String(96), nullable=True),
    Column("quantity_delta", ExactDecimal, nullable=True),
    Column("price", ExactDecimal, nullable=True),
    Column("fee", ExactDecimal, nullable=True),
    Column("realized_net", ExactDecimal, nullable=True),
    Column("ref", String(160), nullable=False),
    UniqueConstraint("account_id", "seq"),
)

fee_accruals = Table(
    "fee_accruals",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("broker", String(32), nullable=False),
    Column("account_id", ID, nullable=False),
    Column("execution_id", String(128), nullable=False),
    Column("estimated", ExactDecimal, nullable=False),
    Column("final", ExactDecimal, nullable=True),
    Column("status", String(8), nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
    UniqueConstraint("broker", "account_id", "execution_id"),
)

risk_reservations = Table(
    "risk_reservations",
    metadata,
    Column("reservation_id", ID, primary_key=True),
    Column("portfolio_id", ID, nullable=False),
    Column("account_id", ID, nullable=False),
    Column("intent_id", ID, nullable=False, unique=True),
    Column("kind", String(8), nullable=False),
    Column("state", String(8), nullable=False),
    Column("instrument_id", String(96), nullable=False),
    Column("track", String(16), nullable=False),
    Column("exposure_tags", Text, nullable=False),
    Column("reserved_qty", ExactDecimal, nullable=False),
    Column("consumed_qty", ExactDecimal, nullable=False),
    Column("released_qty", ExactDecimal, nullable=False),
    Column("notional_usd", ExactDecimal, nullable=False),
    Column("fees_usd", ExactDecimal, nullable=False),
    Column("planned_loss_usd", ExactDecimal, nullable=False),
    Column("policy_hash", String(64), nullable=False),
    Column("created_at", UtcDateTime, nullable=False),
    Column("updated_at", UtcDateTime, nullable=False),
)
Index(
    "uq_one_active_entry_reservation",
    risk_reservations.c.portfolio_id,
    unique=True,
    postgresql_where=text("kind = 'ENTRY' AND state = 'ACTIVE'"),
    sqlite_where=text("kind = 'ENTRY' AND state = 'ACTIVE'"),
)

reservation_events = Table(
    "reservation_events",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("reservation_id", ID, nullable=False),
    Column("event_time", UtcDateTime, nullable=False),
    Column("kind", String(8), nullable=False),
    Column("quantity", ExactDecimal, nullable=False),
    Column("note", Text, nullable=False),
)

account_leases = Table(
    "account_leases",
    metadata,
    Column("account_id", ID, primary_key=True),
    Column("holder", String(96), nullable=False),
    Column("fencing_token", Integer, nullable=False),
    Column("acquired_at", UtcDateTime, nullable=False),
)

pause_state = Table(
    "pause_state",
    metadata,
    Column("flag_id", ID, primary_key=True),
    Column("scope", String(16), nullable=False),
    Column("scope_id", ID, nullable=False),
    Column("category", String(16), nullable=False),
    Column("cause", String(32), nullable=False),
    Column("raised_at", UtcDateTime, nullable=False),
    Column("details", Text, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("cleared_at", UtcDateTime, nullable=True),
    Column("cleared_by", String(96), nullable=True),
    Column("clear_evidence", String(96), nullable=True),
)
Index("ix_pause_state_active", pause_state.c.scope_id, pause_state.c.active)

owner_reviews = Table(
    "owner_reviews",
    metadata,
    Column("review_id", ID, primary_key=True),
    Column("flag_id", ID, nullable=False),
    Column("reviewer", String(96), nullable=False),
    Column("note", Text, nullable=False),
    Column("reviewed_at", UtcDateTime, nullable=False),
    Column("figures_json", Text, nullable=False),
    Column("policy_hash", String(64), nullable=False),
)

reconciliation_runs = Table(
    "reconciliation_runs",
    metadata,
    Column("run_id", ID, primary_key=True),
    Column("account_id", ID, nullable=False),
    Column("started_at", UtcDateTime, nullable=False),
    Column("finished_at", UtcDateTime, nullable=False),
    Column("status", String(8), nullable=False),
    Column("summary_json", Text, nullable=False),
)

loss_windows = Table(
    "loss_windows",
    metadata,
    Column("scope", String(16), primary_key=True),
    Column("scope_id", ID, primary_key=True),
    Column("kind", String(16), primary_key=True),
    Column("window_key", String(32), primary_key=True),
    Column("start_time", UtcDateTime, nullable=False),
    Column("start_equity", ExactDecimal, nullable=False),
    Column("start_flows", ExactDecimal, nullable=False),
    Column("breached_at", UtcDateTime, nullable=True),
)

equity_snapshots = Table(
    "equity_snapshots",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("scope", String(16), nullable=False),
    Column("scope_id", ID, nullable=False),
    Column("ts", UtcDateTime, nullable=False),
    Column("equity", ExactDecimal, nullable=False),
    Column("cum_external_flows", ExactDecimal, nullable=False),
    Column("high_water_mark", ExactDecimal, nullable=False),
)
Index("ix_equity_snapshots_scope_ts", equity_snapshots.c.scope_id, equity_snapshots.c.ts)

exit_plans = Table(
    "exit_plans",
    metadata,
    Column("entry_intent_id", ID, primary_key=True),
    Column("account_id", ID, nullable=False),
    Column("instrument_id", String(96), nullable=False),
    Column("stop_price", ExactDecimal, nullable=False),
    Column("target_price", ExactDecimal, nullable=True),
    Column("max_hold_until", UtcDateTime, nullable=False),
    Column("planned_loss_usd", ExactDecimal, nullable=False),
    Column("active", Boolean, nullable=False),
    Column("created_at", UtcDateTime, nullable=False),
)

alerts = Table(
    "alerts",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("raised_at", UtcDateTime, nullable=False),
    Column("severity", String(8), nullable=False),
    Column("code", String(48), nullable=False),
    Column("account_id", ID, nullable=True),
    Column("message", Text, nullable=False),
)

operating_expenses = Table(
    "operating_expenses",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("incurred_on", Date, nullable=False),
    Column("category", String(32), nullable=False),
    Column("amount", ExactDecimal, nullable=False),
    Column("currency", String(3), nullable=False),
    Column("memo", Text, nullable=False),
)

counters = Table(
    "counters",
    metadata,
    Column("name", String(64), primary_key=True),
    Column("value", Integer, nullable=False),
)
