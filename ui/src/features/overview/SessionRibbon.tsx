/**
 * The trading day at a glance: when the bot may open positions, when it may not, where "now" is,
 * and every decision and fill placed on that line. Windows come from the bot's own calendar rules
 * (data/calendars.py): no entries in the first and last 30 minutes, exits only in the last 15.
 */
import { decisionVerdict } from '../../../shared/verdict';
import { useMemo, useState } from 'react';
import { motion } from 'motion/react';
import { symbolOf } from '../../../shared/derive';
import { displaySession, sessionAt } from '../../app/session';
import { useConsole } from '../../app/store';
import { clock, duration, tzLabel } from '../../design/format';
import { SPRING } from '../../design/primitives';
import s from './ribbon.module.css';

interface Mark {
  at: number;
  kind: 'approved' | 'rejected' | 'buy' | 'sell';
  label: string;
}

export function SessionRibbon({ now }: { now: number }) {
  const tz = useConsole((st) => st.tz);
  const decisions = useConsole((st) => st.tables.risk_decisions);
  const candidates = useConsole((st) => st.tables.candidates);
  const fills = useConsole((st) => st.tables.fills);
  const win = useMemo(() => displaySession(now), [now]);
  const info = useMemo(() => sessionAt(now), [now]);
  const [hover, setHover] = useState<Mark | null>(null);

  const marks = useMemo<Mark[]>(() => {
    if (!win) return [];
    const cand = new Map(candidates.map((c) => [c.candidate_id, c]));
    const inWin = (t: number) => t >= win.open - 60_000 && t <= win.close + 60_000;
    const out: Mark[] = [];
    for (const d of decisions) {
      const t = Date.parse(d.decided_at);
      if (!inWin(t)) continue;
      const sym = symbolOf(cand.get(d.candidate_id)?.instrument_id ?? '?');
      out.push({
        at: t,
        kind: d.approved ? 'approved' : 'rejected',
        label: `${sym} ${d.approved ? 'taken' : `${decisionVerdict(d).label.toLowerCase()} · ${decisionVerdict(d).why}`}`,
      });
    }
    for (const f of fills) {
      const t = Date.parse(f.exec_time);
      if (!inWin(t)) continue;
      out.push({ at: t, kind: f.side === 'BUY' ? 'buy' : 'sell', label: `${f.side === 'BUY' ? 'Bought' : 'Sold'} ${symbolOf(f.instrument_id)} @ ${Number(f.price).toFixed(2)}` });
    }
    return out;
  }, [win, decisions, candidates, fills]);

  if (!win) return null;
  const span = win.close - win.open;
  const pos = (t: number) => `${Math.max(0, Math.min(1, (t - win.open) / span)) * 100}%`;
  const hours: number[] = [];
  for (let h = Math.ceil(win.open / 3600_000) * 3600_000; h < win.close; h += 3600_000) hours.push(h);
  const nowInside = now >= win.open && now <= win.close;

  const headline =
    win.state === 'upcoming'
      ? `Opens in ${duration(win.open - now)}`
      : win.state === 'live'
        ? info.phase === 'entry'
          ? `Entry window · ${duration(win.entryTo - now)} left`
          : info.phase === 'opening'
            ? `Opening · entries from ${clock(win.entryFrom, tz)}`
            : info.phase === 'no_new_entries'
              ? 'No new entries · managing exits'
              : `Close procedure · closes ${clock(win.close, tz)}`
        : info.nextOpen
          ? `Closed · next open in ${duration(info.nextOpen - now)}`
          : 'Closed';

  return (
    <div className={s.wrap}>
      <div className={s.head}>
        <div>
          <div className={s.title}>{headline}</div>
          <div className={s.sub}>
            {win.state === 'done' ? 'Last session' : 'Session'} {new Date(win.open).toLocaleDateString('en-GB', { weekday: 'short', day: 'numeric', month: 'short', timeZone: 'America/New_York' })} · {clock(win.open, tz)}–{clock(win.close, tz)} {tzLabel(tz)}
            {win.close - win.open < 6 * 3600_000 ? ' · early close' : ''}
          </div>
        </div>
        <div className={s.legend}>
          <span><i className={s.keyEntry} />Entry window</span>
          <span><i className={s.keyBlocked} />No new entries</span>
          <span><i className={s.keyClose} />Close procedure</span>
        </div>
      </div>
      <div className={s.track} onMouseLeave={() => setHover(null)}>
        <div className={`${s.seg} ${s.blocked}`} style={{ left: 0, width: pos(win.entryFrom) }} />
        <div className={`${s.seg} ${s.entry}`} style={{ left: pos(win.entryFrom), width: `calc(${pos(win.entryTo)} - ${pos(win.entryFrom)})` }} />
        <div className={`${s.seg} ${s.blocked}`} style={{ left: pos(win.entryTo), width: `calc(${pos(win.closeProcedureFrom)} - ${pos(win.entryTo)})` }} />
        <div className={`${s.seg} ${s.close}`} style={{ left: pos(win.closeProcedureFrom), right: 0 }} />
        {win.state !== 'upcoming' && (
          <motion.div className={s.elapsed} initial={false} animate={{ width: nowInside ? pos(now) : '100%' }} transition={{ duration: 0.4, ease: 'linear' }} />
        )}
        {marks.map((m, i) => (
          <span
            key={i}
            className={`${s.mark} ${s[m.kind]}`}
            style={{ left: pos(m.at) }}
            onMouseEnter={() => setHover(m)}
            aria-label={m.label}
          />
        ))}
        {nowInside && (
          <motion.div className={s.now} initial={false} animate={{ left: pos(now) }} transition={SPRING}>
            <span className={s.nowLabel}>{clock(now, tz)}</span>
          </motion.div>
        )}
        {hover && (
          <div className={s.tip} style={{ left: pos(hover.at) }}>
            <b>{clock(hover.at, tz, true)}</b> · {hover.label}
          </div>
        )}
      </div>
      <div className={s.scale}>
        {hours.map((h) => (
          <span key={h} style={{ left: pos(h) }}>
            {clock(h, tz)}
          </span>
        ))}
      </div>
    </div>
  );
}
