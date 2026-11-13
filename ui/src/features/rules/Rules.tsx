import { useMemo, type ReactNode } from 'react';
import { CheckCircle2, GitCompare, TriangleAlert } from 'lucide-react';
import { latestBy, moneySummary, type MoneySummary } from '../../../shared/derive';
import { useRules } from '../../app/api';
import { usePolicyLimits } from '../../app/policy';
import { useConsole } from '../../app/store';
import { day, pct, usd } from '../../design/format';
import { Card, Chip, CopyChip, CopyCommand, Empty, KV } from '../../design/primitives';
import p from '../pages.module.css';

type Fmt = 'usd' | 'pct' | 'int' | 'bool' | 'bps' | 'sec' | 'ms';

interface RuleDef {
  section: string;
  key: string;
  name: string;
  meaning: string;
  fmt: Fmt;
  usage?: (m: MoneySummary, v: number) => ReactNode;
}

const RULES: RuleDef[] = [
  { section: '', key: 'initial_trading_capital', name: 'Trading capital', meaning: 'The internal subledger the bot may trade. The paper account balance is never used.', fmt: 'usd' },
  { section: 'exposure', key: 'risk_per_trade_fraction', name: 'Risk per trade', meaning: 'Planned loss if the stop is hit, as a share of equity. Sets position size.', fmt: 'pct', usage: (m, v) => `≈ ${usd(Number(m.equity ?? m.initialCapital) * v, { precise: true })} per trade now` },
  { section: 'exposure', key: 'max_total_planned_risk_fraction', name: 'Total planned risk', meaning: 'Sum of planned losses across open positions.', fmt: 'pct' },
  { section: 'exposure', key: 'max_gross_exposure_fraction', name: 'Gross exposure cap', meaning: 'Most of equity that may be invested at once.', fmt: 'pct', usage: (m, v) => `≈ ${usd(Number(m.equity ?? m.initialCapital) * v)} · in use ${usd(Number(m.invested) + Number(m.reserved), { precise: true })}` },
  { section: 'exposure', key: 'max_symbol_exposure_fraction', name: 'Per-symbol cap', meaning: 'Most of equity in any one stock.', fmt: 'pct', usage: (m, v) => `≈ ${usd(Number(m.equity ?? m.initialCapital) * v)}` },
  { section: 'exposure', key: 'minimum_cash_reserve_fraction', name: 'Cash reserve (share)', meaning: 'Share of equity that always stays in cash.', fmt: 'pct' },
  { section: 'exposure', key: 'minimum_cash_reserve_absolute', name: 'Cash reserve (floor)', meaning: 'Cash that always stays uninvested, whichever is larger.', fmt: 'usd', usage: (m) => `required now ${usd(m.requiredReserve)}` },
  { section: 'exposure', key: 'max_open_positions', name: 'Open positions', meaning: 'Most stocks held at once.', fmt: 'int', usage: (m) => `${m.openPositions} open` },
  { section: 'exposure', key: 'max_pending_entry_orders', name: 'Pending entries', meaning: 'Most entry orders working at once.', fmt: 'int' },
  { section: 'exposure', key: 'max_new_entries_per_session', name: 'Entries per session', meaning: 'Most new positions opened per trading day.', fmt: 'int' },
  { section: 'loss_limits', key: 'daily_loss_fraction', name: 'Daily loss limit', meaning: 'Loss within a day (New York session and UTC day) that pauses new entries.', fmt: 'pct', usage: (m, v) => `≈ ${usd(Number(m.equity ?? m.initialCapital) * v, { precise: true })}` },
  { section: 'loss_limits', key: 'weekly_loss_fraction', name: 'Weekly loss limit', meaning: 'Loss within a week that pauses new entries.', fmt: 'pct', usage: (m, v) => `≈ ${usd(Number(m.equity ?? m.initialCapital) * v, { precise: true })}` },
  { section: 'loss_limits', key: 'max_high_water_drawdown_fraction', name: 'Drawdown limit', meaning: 'Fall from the best equity reached that pauses new entries.', fmt: 'pct', usage: (m) => (m.drawdownFraction ? `now ${pct(m.drawdownFraction, 2)}` : '') },
  { section: 'loss_limits', key: 'initial_capital_pause_floor', name: 'Capital floor', meaning: 'Cash-flow-adjusted equity at which everything pauses.', fmt: 'usd', usage: (m) => (m.floorDistance ? `${usd(m.floorDistance)} above` : '') },
  { section: 'loss_limits', key: 'automatic_resume_after_loss_breach', name: 'Auto-resume after loss', meaning: 'Whether a loss pause clears itself. Off means the owner must review and resume.', fmt: 'bool' },
  { section: 'quality', key: 'max_quote_age_seconds', name: 'Quote freshness', meaning: 'Oldest quote the bot will act on.', fmt: 'sec' },
  { section: 'quality', key: 'max_entry_spread_bps', name: 'Spread limit', meaning: 'Widest bid-ask spread allowed for an entry.', fmt: 'bps' },
  { section: 'quality', key: 'max_clock_skew_milliseconds', name: 'Clock skew', meaning: 'Largest allowed gap between the data clock and the local clock.', fmt: 'ms' },
  { section: 'quality', key: 'require_positive_conservative_net_edge', name: 'Require proven edge', meaning: 'Only trade when expected profit after conservative costs is positive (why candidates show “No proven edge”).', fmt: 'bool' },
  { section: 'execution', key: 'allow_blind_submission_retries', name: 'Blind retries', meaning: 'Resubmitting an order whose outcome is unknown. Off prevents duplicate trades.', fmt: 'bool' },
  { section: 'execution', key: 'preserve_risk_reducing_exits_when_paused', name: 'Exits while paused', meaning: 'Stops and exits keep working during a pause.', fmt: 'bool' },
  { section: 'instruments', key: 'long_only', name: 'Long only', meaning: 'The bot only buys; it never sells short.', fmt: 'bool' },
  { section: 'instruments', key: 'leverage_enabled', name: 'Leverage', meaning: 'Borrowing to trade.', fmt: 'bool' },
  { section: 'expenses', key: 'paid_data_enabled', name: 'Paid market data', meaning: 'Whether paid data subscriptions may be used.', fmt: 'bool' },
];

const SECTIONS: [string, string][] = [
  ['', 'Capital'],
  ['exposure', 'Exposure and sizing'],
  ['loss_limits', 'Loss limits'],
  ['quality', 'Data quality'],
  ['execution', 'Execution safety'],
  ['instruments', 'What it may trade'],
  ['expenses', 'Expenses'],
];

function fmt(v: unknown, f: Fmt): string {
  if (v === undefined || v === null) return '—';
  const n = Number(v);
  switch (f) {
    case 'usd':
      return usd(n);
    case 'pct':
      return pct(n, n < 0.01 ? 2 : n < 0.1 ? 1 : 0);
    case 'bool':
      return v === true || v === 'true' ? 'On' : 'Off';
    case 'bps':
      return `${n} bps`;
    case 'sec':
      return `${n} s`;
    case 'ms':
      return `${n} ms`;
    default:
      return String(n);
  }
}

const get = (obj: unknown, section: string, key: string) => {
  const o = obj as Record<string, Record<string, unknown>> | undefined;
  return section ? o?.[section]?.[key] : (o as Record<string, unknown> | undefined)?.[key];
};

function same(a: unknown, b: unknown): boolean {
  if (typeof a === 'boolean' || typeof b === 'boolean') return String(a) === String(b);
  const na = Number(a);
  const nb = Number(b);
  if (!Number.isNaN(na) && !Number.isNaN(nb)) return na === nb;
  return String(a) === String(b);
}

export default function Rules() {
  const rules = useRules();
  const tables = useConsole((s) => s.tables);
  const status = useConsole((s) => s.status);
  const tz = useConsole((s) => s.tz);
  const limits = usePolicyLimits();
  const m = useMemo(() => moneySummary(tables, limits), [tables, limits]);
  const file = rules.data?.policy;
  const running = latestBy(tables.policy_versions, (r) => r.first_seen_at);

  const drift = useMemo(() => {
    if (!file?.data || !running) return null;
    const out: { key: string; file: unknown; running: unknown }[] = [];
    for (const [section] of SECTIONS) {
      const keys = section ? Object.keys(((file.data as Record<string, Record<string, unknown>>)[section] ?? {}) as object) : ['initial_trading_capital', 'base_currency', 'live_enabled'];
      for (const k of keys) {
        const a = get(file.data, section, k);
        const b = get(running.canonical, section, k);
        if (b === undefined) continue;
        if (!same(a, b)) out.push({ key: section ? `${section}.${k}` : k, file: a, running: b });
      }
    }
    return out;
  }, [file, running]);

  const instrumentsData = rules.data?.instruments?.data as Record<string, unknown>[] | { instruments?: Record<string, unknown>[] } | undefined;
  const instruments: Record<string, unknown>[] = Array.isArray(instrumentsData) ? instrumentsData : (instrumentsData?.instruments ?? []);
  const strategy = rules.data?.strategy?.data as Record<string, unknown> | undefined;

  if (rules.isLoading) return null;

  return (
    <div className={p.stack}>
      <div className={p.head}>
        <div>
          <h1 className={p.title}>Rules</h1>
          <p className={p.subtitle}>
            Every limit the bot obeys, what it means in dollars today, and how to change it. The console never edits these: changes go
            through the config file (reviewed, then restart the bot), so there is always a record of who changed what.
          </p>
        </div>
      </div>

      <Card>
        <div className={p.inline} style={{ alignItems: 'flex-start', gap: 14 }}>
          <GitCompare size={20} color="var(--ink-2)" style={{ marginTop: 2 }} />
          <div style={{ flex: 1, minWidth: 0 }} className={p.stack}>
            <div style={{ fontWeight: 600 }}>
              {!running
                ? status?.kind === 'replay'
                  ? 'Replay sessions carry their own recorded policy.'
                  : 'Running policy unknown until the bot connects.'
                : drift && drift.length
                  ? `The file on disk differs from the policy the bot is running (${drift.length} value${drift.length === 1 ? '' : 's'}).`
                  : 'The bot is running exactly the policy in the file.'}
            </div>
            {running && (
              <div className={p.small + ' ' + p.muted}>
                Running policy {running.policy_hash.slice(0, 12)} · first seen {day(running.first_seen_at, tz)} · file modified {file ? day(file.modifiedAt, tz) : '—'}
              </div>
            )}
            {drift && drift.length > 0 && (
              <>
                <KV items={drift.map((d) => [<code key={d.key}>{d.key}</code>, `file ${String(d.file)} · running ${String(d.running)}`])} />
                <div className={p.small + ' ' + p.muted}>The new values take effect when the paper bot restarts:</div>
                <CopyCommand command="uv run trader paper run --config configs/paper.yaml" />
              </>
            )}
            {running && drift && drift.length === 0 && (
              <div className={p.inline + ' ' + p.small}>
                <CheckCircle2 size={14} color="var(--good)" /> No drift
              </div>
            )}
          </div>
        </div>
      </Card>

      <Card title="How to change a limit" sub="Three steps, so every change is reviewed and on record. Each rule below shows its key.">
        <div className={p.grid}>
          <section className={p.s4}>
            <div className={p.small} style={{ fontWeight: 600, marginBottom: 6 }}>1 · Edit the policy file</div>
            <CopyCommand command={`code ${(file?.path ?? 'configs/policy/base.yaml').replaceAll('/', '\\')}`} />
          </section>
          <section className={p.s4}>
            <div className={p.small} style={{ fontWeight: 600, marginBottom: 6 }}>2 · Review and commit the change</div>
            <CopyCommand command={`git diff ${file?.path ?? 'configs/policy/base.yaml'}`} />
          </section>
          <section className={p.s4}>
            <div className={p.small} style={{ fontWeight: 600, marginBottom: 6 }}>3 · Restart the paper bot (Ctrl+C, then)</div>
            <CopyCommand command="uv run trader paper run --config configs/paper.yaml" />
          </section>
        </div>
        <p className={p.small + ' ' + p.muted} style={{ marginTop: 12 }}>
          The drift check above turns amber the moment the file and the running bot disagree, and clears once the bot restarts with the new
          file.
        </p>
      </Card>

      {!file ? (
        <Card>
          <Empty title="configs/policy/base.yaml not found" />
        </Card>
      ) : (
        SECTIONS.map(([section, title]) => {
          const defs = RULES.filter((r) => r.section === section);
          if (!defs.length) return null;
          return (
            <Card key={section || 'capital'} title={title}>
              {defs.map((r) => {
                const v = get(file.data, r.section, r.key);
                const yamlKey = r.section ? `${r.section}:\n  ${r.key}` : r.key;
                return (
                  <div key={r.key} className={p.rule}>
                    <div>
                      <div className={p.ruleName}>{r.name}</div>
                      <div className={p.ruleMeaning}>{r.meaning}</div>
                    </div>
                    <div>
                      <div className={p.ruleValue}>{fmt(v, r.fmt)}</div>
                      {r.usage && <div className={p.ruleUse}>{r.usage(m, Number(v))}</div>}
                    </div>
                    <div>
                      <div className={p.small + ' ' + p.muted} style={{ marginBottom: 6 }}>
                        Key in <code>{file.path.split('/').pop()}</code>
                      </div>
                      <CopyChip text={r.section ? `${r.section}.${r.key}` : r.key} label={yamlKey} />
                    </div>
                  </div>
                );
              })}
            </Card>
          );
        })
      )}

      <div className={p.grid}>
        <section className={p.s6}>
          <Card title="Instruments" sub={rules.data?.instruments?.path}>
            {instruments.length === 0 ? (
              <Empty title="No instruments file" />
            ) : (
              <div className={p.list}>
                {instruments.map((i) => {
                  const id = String((i.identity as Record<string, unknown> | undefined)?.instrument_id ?? i.instrument_id ?? i.symbol ?? '?');
                  return (
                    <div key={id} className={p.row}>
                      <div className={p.rowMain}>
                        <div className={p.rowTitle}>{id}</div>
                        <div className={p.rowSub}>
                          {String(i.evidence ?? '—')} · min notional {String(i.min_notional ?? '—')} · tick {String(i.price_tick ?? '—')}
                        </div>
                      </div>
                      {i.tradable ? <Chip strong>tradable</Chip> : <Chip>not tradable</Chip>}
                    </div>
                  );
                })}
              </div>
            )}
          </Card>
        </section>
        <section className={p.s6}>
          <Card title="Strategy" sub={rules.data?.strategy?.path}>
            {strategy ? (
              <KV
                items={Object.entries(strategy)
                  .filter(([, v]) => typeof v !== 'object' || v === null)
                  .slice(0, 14)
                  .map(([k, v]) => [k.replaceAll('_', ' '), String(v)])}
              />
            ) : (
              <Empty title="Strategy spec not found" />
            )}
            <p className={p.small + ' ' + p.muted} style={{ marginTop: 12 }}>
              Strategy specs are frozen and hash-pinned; changing one means registering a new research trial.
            </p>
          </Card>
        </section>
      </div>

      <Card title="Owner commands" sub="Run from the repository root in PowerShell. The database URL comes from your .env (load it into the session first).">
        <div className={p.grid}>
          {[
            ['Status: pauses, positions, live orders', 'uv run trader status --db $env:TRADER_DATABASE_URL'],
            ['Pause new entries', 'uv run trader pause --db $env:TRADER_DATABASE_URL --reason "owner review"'],
            ['Resume after an owner pause', 'uv run trader resume --db $env:TRADER_DATABASE_URL --config configs/paper.yaml'],
            ['Resume after a loss breach (after review)', 'uv run trader resume --db $env:TRADER_DATABASE_URL --config configs/paper.yaml --ack-loss-breach --note "what I reviewed"'],
            ['One reconciliation (refused while a worker runs)', 'uv run trader reconcile --config configs/paper.yaml'],
            ['Close all positions', 'uv run trader close-positions --config configs/paper.yaml'],
            ['Start the paper bot', 'uv run trader paper run --config configs/paper.yaml'],
            ['Research log and budget', 'uv run trader research status'],
          ].map(([label, cmd]) => (
            <section key={label} className={p.s6}>
              <div className={p.small} style={{ fontWeight: 600, marginBottom: 6 }}>{label}</div>
              <CopyCommand command={cmd} />
            </section>
          ))}
        </div>
        <div className={p.inline + ' ' + p.small + ' ' + p.muted} style={{ marginTop: 16 }}>
          <TriangleAlert size={14} /> Live trading has no command here: it is refused by design until gates G0–G4 pass.
        </div>
      </Card>
    </div>
  );
}
