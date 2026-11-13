import { useEffect, useMemo, useRef, useState } from 'react';
import Decimal from 'decimal.js';
import { D, moneySummary, symbolOf } from '../../../shared/derive';
import { usePolicyLimits } from '../../app/policy';
import { useConsole } from '../../app/store';
import { linkForLedgerKind, MoneySankey } from '../../charts/MoneySankey';
import { ChartOrTable } from '../../design/ChartOrTable';
import { clock, day, price, qty, tzLabel, usd } from '../../design/format';
import { LEDGER_KINDS } from '../../design/language';
import { Card, Chip, Delta, Empty, Stat, styles as ps } from '../../design/primitives';
import { SourceNotice } from '../SourceNotice';
import { SleeveSummary, TradesTable } from '../trades/AllSleeves';
import ts from '../trades/trades.module.css';
import p from '../pages.module.css';

export default function Money() {
  const tables = useConsole((s) => s.tables);
  const tz = useConsole((s) => s.tz);
  const limits = usePolicyLimits();
  const m = useMemo(() => moneySummary(tables, limits), [tables, limits]);

  // A particle runs along the link of each newly posted ledger entry.
  const [flash, setFlash] = useState<{ id: number; link: string } | null>(null);
  const lastLedger = useRef<number>(tables.ledger_entries.at(-1)?.id ?? 0);
  useEffect(() => {
    const newest = tables.ledger_entries.at(-1);
    if (!newest || newest.id === lastLedger.current) return;
    lastLedger.current = newest.id;
    const link = linkForLedgerKind(newest.kind);
    if (link) setFlash({ id: newest.id, link });
  }, [tables.ledger_entries]);

  const ledger = useMemo(() => {
    let bal = new Decimal(0);
    return tables.ledger_entries.map((l) => {
      bal = bal.plus(D(l.cash_delta));
      return { ...l, balance: bal.toFixed() };
    });
  }, [tables.ledger_entries]);

  const equity = Number(m.equity ?? m.cash) || Number(m.initialCapital);
  const reserve = Number(m.requiredReserve);
  const deployable = Math.max(0, Number(limits.maxGrossExposureFraction) * equity);
  const inUse = Number(m.invested) + Number(m.reserved);
  const perSymbol = Number(limits.maxSymbolExposureFraction) * equity;
  const unrealized = useMemo(() => {
    // Equity snapshot already marks positions conservatively; the difference to cash + cost is the mark.
    if (m.equity === null) return null;
    return new Decimal(m.equity).minus(D(m.cash)).minus(D(m.invested)).toFixed();
  }, [m]);

  return (
    <div className={p.stack}>
      <SourceNotice />
      <div className={p.head}>
        <div>
          <h1 className={p.title}>Money</h1>
          <p className={p.subtitle}>
            The three sleeves of the mission: what each is worth, what its trades made and cost, and every trade one click from its story. Below that, the
            stock bot's own ledger to the cent.
          </p>
        </div>
      </div>

      <SleeveSummary />
      <TradesTable />
      <div>
        <h2 className={ts.sectionTitle}>Stocks sleeve · the bot's ledger</h2>
        <p className={ts.sectionSub}>
          Where every dollar of the bot's USD {Number(m.initialCapital).toLocaleString('en-US')} subledger is right now, and every entry that moved it. The paper
          account's own ~USD 100,000 balance is never trading capital.
        </p>
      </div>
      <div className={p.tiles}>
        <div className={p.tile}><Stat label="Equity" value={<span className="num">{usd(m.equity ?? m.cash, { precise: true })}</span>} foot="Conservative mark" /></div>
        <div className={p.tile}><Stat label="Total P&L" value={<Delta value={m.totalPnl} precise />} foot="Equity minus external flows" /></div>
        <div className={p.tile}><Stat label="Realised gross" value={<Delta value={m.realizedGross} precise />} foot="Before fees" /></div>
        <div className={p.tile}><Stat label="Fees" value={<span className="num">{usd(m.fees, { precise: true })}</span>} foot="On fills" /></div>
        <div className={p.tile}><Stat label="Realised net" value={<Delta value={m.realizedNet} precise />} foot="After fees" /></div>
        <div className={p.tile}><Stat label="Open mark" value={unrealized !== null ? <Delta value={unrealized} precise /> : '—'} foot="Equity − cash − cost" /></div>
      </div>

      <Card title="Money flow" sub="Capital and realised results on the left; where it sits now on the right. Thin threads are small amounts, labelled exactly.">
        <ChartOrTable
          label="money flow"
          chart={<MoneySankey m={m} flash={flash} />}
          columns={[
            { key: 'item', label: 'Item' },
            { key: 'side', label: 'Side' },
            { key: 'usd', label: 'USD', align: 'right' },
          ]}
          rows={[
            { item: 'Initial capital', side: 'in', usd: usd(m.initialCapital, { precise: true }) },
            { item: 'External flows (incl. initial)', side: 'in', usd: usd(m.externalFlows, { precise: true }) },
            { item: 'Realised gross P&L', side: Number(m.realizedGross) >= 0 ? 'in' : 'out', usd: usd(m.realizedGross, { sign: true, precise: true }) },
            { item: 'Fees paid', side: 'out', usd: usd(m.fees, { precise: true }) },
            { item: 'Cash (ledger)', side: 'now', usd: usd(m.cash, { precise: true }) },
            { item: '  of which required reserve', side: 'now', usd: usd(m.requiredReserve, { precise: true }) },
            { item: '  of which held for orders', side: 'now', usd: usd(m.reserved, { precise: true }) },
            { item: '  of which free', side: 'now', usd: usd(m.freeCash, { precise: true }) },
            { item: 'Invested (cost)', side: 'now', usd: usd(m.invested, { precise: true }) },
            { item: 'Equity (conservative mark)', side: 'now', usd: m.equity ? usd(m.equity, { precise: true }) : '—' },
          ]}
        />
      </Card>

      <Card title="What the policy lets the bot use" sub={`Of ${usd(equity)} equity: at least ${usd(reserve)} stays in cash; at most ${usd(deployable)} may be invested, ${usd(perSymbol)} per symbol.`}>
        <div className={p.capacity} role="img" aria-label="Capacity">
          <div className={p.capacityPart} style={{ flex: reserve, background: 'var(--seq-250)' }} title={`Required reserve ${usd(reserve)}`} />
          <div className={p.capacityPart} style={{ flex: Math.min(inUse, deployable), background: 'var(--series-3)' }} title={`In use ${usd(inUse)}`} />
          <div className={p.capacityPart} style={{ flex: Math.max(0, deployable - inUse), background: 'var(--fill-strong)' }} title={`Unused capacity ${usd(Math.max(0, deployable - inUse))}`} />
        </div>
        <div className={p.legendRow}>
          <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--seq-250)' }} />Required cash reserve {usd(reserve)}</span>
          <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--series-3)' }} />In use {usd(inUse, { precise: true })}</span>
          <span className={p.legendKey}><span className={p.swatch} style={{ background: 'var(--fill-strong)' }} />Unused capacity {usd(Math.max(0, deployable - inUse))}</span>
          <span className={p.legendKey}>Risk per trade {usd(Number(limits.riskPerTradeFraction) * equity, { precise: true })}</span>
        </div>
      </Card>

      <div className={p.grid}>
        <section className={p.s8}>
          <Card title="Ledger" sub="Append-only. Running cash after each entry." flush>
            {ledger.length === 0 ? (
              <Empty title="No ledger entries" body="The first entry is the initial capital, written when the bot first starts." />
            ) : (
              <div className={p.scroll}>
                <table className={ps.table}>
                  <thead>
                    <tr>
                      <th>Time ({tzLabel(tz)})</th>
                      <th>Kind</th>
                      <th>Instrument</th>
                      <th className={ps.r}>Quantity</th>
                      <th className={ps.r}>Price</th>
                      <th className={ps.r}>Cash</th>
                      <th className={ps.r}>Balance</th>
                    </tr>
                  </thead>
                  <tbody>
                    {[...ledger].reverse().map((l) => (
                      <tr key={l.id}>
                        <td>{day(l.entry_time, tz)} {clock(l.entry_time, tz, true)}</td>
                        <td><Chip>{LEDGER_KINDS[l.kind] ?? l.kind}</Chip></td>
                        <td>{l.instrument_id ? symbolOf(l.instrument_id) : '—'}</td>
                        <td className={ps.r}>{l.quantity_delta ? qty(l.quantity_delta) : ''}</td>
                        <td className={ps.r}>{l.price ? price(l.price) : ''}</td>
                        <td className={ps.r}><Delta value={l.cash_delta} precise /></td>
                        <td className={ps.r}>{usd(l.balance, { precise: true })}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </Card>
        </section>
        <section className={p.s4}>
          <div className={p.stack}>
            <Card title="Settlement" sub="Sale proceeds settle on the instrument's calendar.">
              {tables.cash_lots.length === 0 ? (
                <Empty title="Nothing settling" />
              ) : (
                <div className={p.list}>
                  {tables.cash_lots.map((c) => (
                    <div key={c.id} className={p.row}>
                      <div className={p.rowMain}>
                        <div className={p.rowTitle}>{usd(c.amount, { precise: true })}</div>
                        <div className={p.rowSub}>Traded {c.trade_date} · {c.source}</div>
                      </div>
                      <div className={p.rowRight}>
                        <div className={p.rowRightSub}>settles</div>
                        <div>{c.settles_on}</div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            </Card>
            <Card title="Fee accruals" sub="Estimated until the broker's final charge arrives.">
              {tables.fee_accruals.length === 0 ? (
                <Empty title="No pending fees" />
              ) : (
                <div className={p.list}>
                  {tables.fee_accruals.map((f) => (
                    <div key={f.id} className={p.row}>
                      <div className={p.rowMain}>
                        <div className={p.rowTitle}>{f.execution_id}</div>
                        <div className={p.rowSub}>estimated {usd(f.estimated, { precise: true })}{f.final !== null ? ` · final ${usd(f.final, { precise: true })}` : ''}</div>
                      </div>
                      <Chip>{f.status}</Chip>
                    </div>
                  ))}
                </div>
              )}
            </Card>
          </div>
        </section>
      </div>
    </div>
  );
}
