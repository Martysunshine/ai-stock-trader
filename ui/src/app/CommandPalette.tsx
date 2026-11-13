import { useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router';
import { AnimatePresence, motion } from 'motion/react';
import { symbolOf } from '../../shared/derive';
import { SPRING } from '../design/primitives';
import { useHistorySymbols, useMarketIndex, useResearch } from './api';
import { useConsole } from './store';
import s from './shell.module.css';

interface Item {
  label: string;
  hint: string;
  go: () => void;
}

/** Ctrl/Cmd+K: jump to a page, a symbol, a decision or a research trial. */
export function CommandPalette() {
  const [open, setOpen] = useState(false);
  const [q, setQ] = useState('');
  const [active, setActive] = useState(0);
  const navigate = useNavigate();
  const input = useRef<HTMLInputElement>(null);
  const candidates = useConsole((st) => st.tables.candidates);
  const setSource = useConsole((st) => st.setSource);
  const symbols = useHistorySymbols();
  const market = useMarketIndex();
  const research = useResearch();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault();
        setOpen((o) => !o);
      }
      if (e.key === 'Escape') setOpen(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);
  useEffect(() => {
    if (open) {
      setQ('');
      setActive(0);
      setTimeout(() => input.current?.focus(), 10);
    }
  }, [open]);

  const items = useMemo<Item[]>(() => {
    const pages: Item[] = [
      ['Overview', '/'],
      ['Decisions', '/decisions'],
      ['Markets', '/markets'],
      ['Money', '/money'],
      ['Risk & health', '/risk'],
      ['Learning', '/learning'],
      ['Rules', '/rules'],
      ['Desk: notes for Claude', '/desk'],
    ].map(([label, to]) => ({ label, hint: 'Page', go: () => navigate(to) }));
    const sources: Item[] = [
      { label: 'Live paper data', hint: 'Source', go: () => setSource('live') },
      { label: 'Replay: baseline', hint: 'Source', go: () => setSource('replay:baseline') },
      { label: 'Replay: loss (pause demo)', hint: 'Source', go: () => setSource('replay:loss') },
      { label: 'Replay: timeout', hint: 'Source', go: () => setSource('replay:timeout') },
      { label: 'Replay: crash and recovery', hint: 'Source', go: () => setSource('replay:crash') },
    ];
    // Markets by id (`stocks:AAPL`, `crypto:BTC`, `cfd:GOLD`): the feed's rows, then stock history
    // and the bot's candidates (both stocks).
    const groups = market.data?.available ? market.data.groups : [];
    const feed = groups.flatMap((g) => g.instruments.map((i) => ({ id: i.id, label: `${i.symbol} · ${i.name}`, hint: g.title })));
    const stockIds = [
      ...(symbols.data ?? []).filter((x) => x.dataset.startsWith('alpaca')).map((x) => x.symbol),
      ...candidates.map((c) => symbolOf(c.instrument_id)),
    ].map((sym) => ({ id: `stocks:${sym}`, label: sym, hint: 'Stocks' }));
    const seen = new Set<string>();
    const syms: Item[] = [...feed, ...stockIds]
      .filter((x) => !seen.has(x.id) && Boolean(seen.add(x.id)))
      .map((x) => ({ label: x.label, hint: x.hint, go: () => navigate(`/markets/${x.id}`) }));
    const decisions: Item[] = candidates
      .slice(-50)
      .reverse()
      .map((c) => ({
        label: `${symbolOf(c.instrument_id)} candidate ${c.candidate_id.slice(5, 13)}`,
        hint: 'Decision',
        go: () => navigate(`/decisions?c=${encodeURIComponent(c.candidate_id)}`),
      }));
    const trials: Item[] = (research.data?.registry.events ?? [])
      .filter((e) => e.event === 'REGISTER')
      .map((e) => {
        const trial = (e.payload.trial ?? {}) as { trial_id?: string; study_id?: string };
        return { label: `${trial.trial_id} · ${trial.study_id}`, hint: 'Trial', go: () => navigate(`/learning?trial=${trial.trial_id}`) };
      });
    return [...pages, ...sources, ...syms, ...decisions, ...trials];
  }, [navigate, setSource, symbols.data, market.data, candidates, research.data]);

  const filtered = useMemo(() => {
    const t = q.trim().toLowerCase();
    return (t ? items.filter((i) => i.label.toLowerCase().includes(t) || i.hint.toLowerCase().includes(t)) : items).slice(0, 40);
  }, [items, q]);

  const run = (i: Item | undefined) => {
    if (!i) return;
    i.go();
    setOpen(false);
  };

  return (
    <AnimatePresence>
      {open && (
        <>
          <motion.div
            style={{ position: 'fixed', inset: 0, background: 'var(--scrim)', zIndex: 59 }}
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            onClick={() => setOpen(false)}
          />
          <motion.div
            className={s.palette}
            role="dialog"
            aria-label="Jump to"
            initial={{ opacity: 0, scale: 0.97, y: -6 }}
            animate={{ opacity: 1, scale: 1, y: 0 }}
            exit={{ opacity: 0, scale: 0.98 }}
            transition={SPRING}
          >
            <input
              ref={input}
              className={s.paletteInput}
              placeholder="Jump to a page, symbol, decision or trial"
              value={q}
              onChange={(e) => {
                setQ(e.target.value);
                setActive(0);
              }}
              onKeyDown={(e) => {
                if (e.key === 'ArrowDown') {
                  e.preventDefault();
                  setActive((a) => Math.min(filtered.length - 1, a + 1));
                } else if (e.key === 'ArrowUp') {
                  e.preventDefault();
                  setActive((a) => Math.max(0, a - 1));
                } else if (e.key === 'Enter') run(filtered[active]);
              }}
            />
            <div className={s.paletteList} role="listbox">
              {filtered.map((i, n) => (
                <button
                  key={`${i.hint}-${i.label}`}
                  role="option"
                  aria-selected={n === active}
                  className={`${s.paletteItem} ${n === active ? s.paletteItemActive : ''}`}
                  onMouseEnter={() => setActive(n)}
                  onClick={() => run(i)}
                >
                  <span>{i.label}</span>
                  <span className={s.paletteHint}>{i.hint}</span>
                </button>
              ))}
            </div>
          </motion.div>
        </>
      )}
    </AnimatePresence>
  );
}
