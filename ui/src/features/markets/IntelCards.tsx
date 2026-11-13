/**
 * Intel beside an instrument's prices (ADR-0047): news, insider filings and pre-market activity,
 * and for one trade, what was known when it was decided.
 *
 * Every headline, summary and filing line was written by someone outside. It is rendered as plain
 * text (React escapes it; there is no HTML or Markdown rendering here), and a link is shown only
 * when it points at one of the feed's own hosts (`safeLink`). This is information for a person:
 * the stocks bot does not read it.
 */
import type { ReactNode } from 'react';
import {
  ageText,
  feedWasStale,
  safeLink,
  type IntelIndex,
  type IntelInsiderSummary,
  type IntelNewsSummary,
  type IntelPremarket,
  type IntelSourceState,
} from '../../../shared/intel';
import { useIntel, useIntelIndex, useIntelKnown } from '../../app/api';
import { ago, compact, dayTime, price, tzLabel, usd, type Tz } from '../../design/format';
import { Card, Delta, Empty, KV } from '../../design/primitives';
import s from './intel.module.css';

const CATEGORY_LABEL: Record<string, string> = {
  earnings: 'Earnings',
  guidance: 'Guidance',
  financing: 'Financing',
  mna: 'Deal',
  analyst: 'Analyst',
  legal_regulatory: 'Legal',
  insider_buy: 'Bought',
  insider_sell: 'Sold',
  insider_mixed: 'Bought and sold',
  insider_other: 'Other',
};

const num = (v: unknown): number | null => {
  if (v === null || v === undefined || v === '') return null;
  const x = Number(v);
  return Number.isFinite(x) ? x : null;
};

function sourceLine(source: IntelSourceState | undefined, now: number): string {
  if (!source) return 'Source not configured';
  if (source.status === 'ok' || source.status === 'partial') return `${source.label} · read ${ago(source.updated_at, now)}`;
  if (source.status === 'waiting') return `${source.label} · waiting`;
  if (source.status === 'idle') return `${source.label} · ${source.updated_at ? `last read ${ago(source.updated_at, now)}` : 'not read yet'}`;
  return `${source.label} · failing since ${source.updated_at ? ago(source.updated_at, now) : 'the start'}`;
}

function Item(props: { at: string; tz: Tz; category?: string; title: string; url: string | null; linkLabel: string }) {
  const href = safeLink(props.url);
  const label = props.category ? CATEGORY_LABEL[props.category] : undefined;
  return (
    <li className={s.item}>
      <div className={s.itemMeta}>
        <span>{dayTime(props.at, props.tz)}</span>
        {label && <span className={s.category}>{label}</span>}
        {href && (
          <a className={s.link} href={href} target="_blank" rel="noopener noreferrer nofollow">
            {props.linkLabel}
          </a>
        )}
      </div>
      <p className={s.itemTitle}>{props.title}</p>
    </li>
  );
}

function newsCounts(news: IntelNewsSummary): ReactNode {
  const cats = Object.entries(news.categories_24h)
    .filter(([k]) => k !== 'unknown')
    .map(([k, v]) => `${CATEGORY_LABEL[k] ?? k} ${v}`)
    .join(' · ');
  return (
    <>
      <b>{news.count_24h}</b> in 24 hours · <b>{news.count_2h}</b> in 2 hours{cats ? ` · ${cats}` : ''}
    </>
  );
}

function premarketRows(pre: IntelPremarket, digits: number): [ReactNode, ReactNode][] {
  const gap = num(pre.gap_pct);
  const volume = num(pre.volume);
  return [
    ['Session', pre.session ?? '—'],
    ['Against prior close', gap !== null ? <Delta key="g" value={gap / 100} kind="pct" pill /> : '—'],
    ['Last', num(pre.last) !== null ? price(num(pre.last), digits) : '—'],
    ['Prior close', num(pre.prior_close) !== null ? price(num(pre.prior_close), digits) : '—'],
    ['Range', num(pre.low) !== null && num(pre.high) !== null ? `${price(num(pre.low), digits)} – ${price(num(pre.high), digits)}` : '—'],
    ['Volume', volume !== null ? compact(volume) : '—'],
  ];
}

function insiderLine(ins: IntelInsiderSummary): ReactNode {
  return (
    <>
      Past {ins.window_days} days: <b>{ins.filings}</b> filings · <b>{ins.buys}</b> with purchases · <b>{ins.sells}</b> with sales · net{' '}
      <b>{usd(num(ins.net_value), { sign: true })}</b>
    </>
  );
}

/** The right-hand cards on the Markets page for one instrument. */
export function IntelCards({ id, tz, now, digits = 2 }: { id: string; tz: Tz; now: number; digits?: number }) {
  const indexQuery = useIntelIndex();
  const index: IntelIndex | null = indexQuery.data?.available ? indexQuery.data : null;
  const target = index?.targets.find((t) => t.id === id) ?? null;
  const latest = useIntel(target ? id : null);
  if (!index) {
    return (
      <Card title="News and filings">
        <Empty title="The intel feed has not run yet" body="It starts with the console (npm run dev) and reads news, insider filings and pre-market prices." />
      </Card>
    );
  }
  if (!target) return null;
  const source = (kind: string) => index.sources.find((x) => x.kind === kind);
  const data = latest.data ?? null;
  const snap = data?.snapshot ?? null;
  const edgar = source('insider');

  return (
    <>
      <Card title="News" sub={target.has.news ? sourceLine(source('news'), now) : undefined}>
        {!target.has.news ? (
          <Empty title="No news source for this instrument" body="No free feed covers it. Nothing is shown rather than something unrelated." />
        ) : (
          <>
            {target.news_proxy && <p className={s.proxy}>Stand-in: these headlines are about {target.news_proxy}, not this instrument itself.</p>}
            {snap?.news && <p className={s.counts}>{newsCounts(snap.news)}</p>}
            {data && data.news.length > 0 ? (
              <ul className={s.items}>
                {data.news.slice(0, 8).map((n) => (
                  <Item key={n.id} at={n.at} tz={tz} category={n.category} title={n.title} url={n.url} linkLabel="Read" />
                ))}
              </ul>
            ) : (
              <Empty title={latest.isLoading ? 'Loading…' : 'No headlines yet'} />
            )}
            <p className={s.foot}>Times in {tzLabel(tz)}. Categories come from fixed keyword rules; most headlines have none.</p>
          </>
        )}
      </Card>

      {(target.has.premarket || target.has.insiders) && (
        <div className={s.pair}>
          {target.has.premarket && (
            <Card title="Pre-market" sub={sourceLine(source('premarket'), now)}>
              {snap?.premarket?.session ? (
                <>
                  <KV items={premarketRows(snap.premarket, digits)} />
                  <p className={s.foot}>
                    04:00 to 09:30 New York, through {snap.premarket.through ? dayTime(snap.premarket.through, tz) : '—'} {tzLabel(tz)}. Prices arrive 16
                    minutes late. The bot trades in regular hours only.
                  </p>
                </>
              ) : (
                <Empty title="Nothing read yet" body="Pre-market bars are read on trading days from 04:16 to 09:56 New York time." />
              )}
            </Card>
          )}

          {target.has.insiders && (
            <Card title="Insider filings" sub={sourceLine(edgar, now)}>
              {edgar?.status === 'waiting' ? (
                <Empty
                  title="Waiting for your contact line"
                  body="The SEC asks automated readers to name a contact. Add SEC_EDGAR_CONTACT=your email address to .env and insider filings start on the next run."
                />
              ) : !edgar?.updated_at ? (
                // Never read: "0 filings" would look like a fact about the company.
                <Empty title="Not read yet" body="Insider filings are read every 30 minutes once the feed is running." />
              ) : (
                <>
                  {snap?.insiders && <p className={s.counts}>{insiderLine(snap.insiders)}</p>}
                  {data && data.insiders.length > 0 ? (
                    <ul className={s.items}>
                      {data.insiders.slice(0, 4).map((f) => (
                        <Item key={f.id} at={f.at} tz={tz} category={f.category} title={f.title} url={f.url} linkLabel="SEC filing" />
                      ))}
                    </ul>
                  ) : (
                    <Empty title={latest.isLoading ? 'Loading…' : 'No filings in this window'} />
                  )}
                  <p className={s.foot}>
                    Form 4 from SEC EDGAR. Only open-market purchases and sales are counted; grants, option exercises and gifts are listed, not counted.
                  </p>
                </>
              )}
            </Card>
          )}
        </div>
      )}
    </>
  );
}

/** On a trade's story: the stored snapshot of what was known when the trade was decided. */
export function WhatWasKnown({ id, at, tz, evidence, byBot }: { id: string; at: string; tz: Tz; evidence: string[]; byBot: boolean }) {
  const indexQuery = useIntelIndex();
  const listed = indexQuery.data?.available ? indexQuery.data.targets.some((t) => t.id === id) : false;
  const known = useIntelKnown(listed ? id : null, listed ? at : null);
  const k = known.data ?? null;
  const snap = k?.snapshot ?? null;
  const sub = byBot
    ? 'The bot did not use any of this. It is shown so the trade can be read against it.'
    : 'What the intel feed had recorded when this trade was planned.';

  const rows: [ReactNode, ReactNode][] = [];
  if (snap?.news) {
    rows.push(['News', newsCounts(snap.news)]);
    if (snap.news.proxy) rows.push(['News is about', `${snap.news.proxy} (a stand-in)`]);
  }
  if (snap?.premarket?.session) {
    const gap = num(snap.premarket.gap_pct);
    rows.push(['Pre-market', gap !== null ? <Delta key="g" value={gap / 100} kind="pct" pill /> : `session ${snap.premarket.session}`]);
  }
  if (snap?.insiders) {
    // Never read by then: "0 filings" would look like a fact about the company.
    const read = snap.insiders.filings > 0 || ['ok', 'partial'].includes(snap.sources.edgar_form4 ?? '');
    rows.push(['Insiders', read ? insiderLine(snap.insiders) : 'not read by then']);
  }
  if (k && snap) rows.push(['Recorded', `${dayTime(snap.as_of, tz)} ${tzLabel(tz)} · ${ageText(k.snapshotAgeSeconds)} before`]);
  if (k) rows.push(['Feed last ran', k.lastRunAt ? `${dayTime(k.lastRunAt, tz)} ${tzLabel(tz)} · ${ageText(k.lastRunAgeSeconds)} before` : 'never before this trade']);

  return (
    <Card title="What was known" sub={sub}>
      {!listed || known.isError ? (
        <Empty title="Nothing recorded" body="The intel feed does not follow this instrument, or had not run yet." />
      ) : known.isLoading ? (
        <Empty title="Loading…" />
      ) : !snap ? (
        <Empty title="Nothing recorded by then" body="The intel feed had not written anything for this instrument before this trade." />
      ) : (
        <>
          {k && feedWasStale(k) && (
            <p className={s.warn}>The feed was not running at the time: its last run was {ageText(k.lastRunAgeSeconds)} earlier. What is shown is older than the trade.</p>
          )}
          <KV items={rows} />
          {snap.news && snap.news.latest.length > 0 && (
            <ul className={s.items} style={{ marginTop: 14 }}>
              {snap.news.latest.map((n) => (
                <Item key={n.id} at={n.at} tz={tz} category={n.category} title={n.title} url={n.url} linkLabel="Read" />
              ))}
            </ul>
          )}
        </>
      )}
      {evidence.length > 0 && (
        <>
          <p className={s.foot}>The plan cited:</p>
          <div className={s.refs}>
            {evidence.map((ref) => (
              <span key={ref} className={s.ref}>
                {ref}
              </span>
            ))}
          </div>
        </>
      )}
      {!byBot && evidence.length === 0 && listed && <p className={s.foot}>The plan cited no intel reference.</p>}
    </Card>
  );
}
