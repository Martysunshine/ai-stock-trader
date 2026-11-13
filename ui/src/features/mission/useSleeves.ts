import { useMemo } from 'react';
import { useQuery } from '@tanstack/react-query';
import { positionViews } from '../../../shared/derive';
import { practiceView, type MissionState, type SleeveView } from '../../../shared/mission';
import { getJson } from '../../app/api';
import { botStatus } from '../../app/botStatus';
import { useNow } from '../../app/hooks';
import { sessionAt } from '../../app/session';
import { useConsole, viewNow } from '../../app/store';

/** The practice journals and the stocks sleeve's start; polled, because journals change by hand. */
export const useMission = () =>
  useQuery({
    queryKey: ['mission'],
    queryFn: () => getJson<MissionState>('/api/mission'),
    refetchInterval: 30_000,
    staleTime: 15_000,
  });

export interface Sleeves {
  mission: MissionState | undefined;
  views: SleeveView[];
  total: number;
  start: number;
}

/**
 * The three sleeves with real numbers: stocks from the bot's own tables (only when they belong to
 * the portfolio configs/paper.yaml names), crypto and CFDs from their practice journals.
 */
export function useSleeves(): Sleeves {
  const mission = useMission().data;
  const tables = useConsole((s) => s.tables);
  const status = useConsole((s) => s.status);
  const wall = useNow(30_000);

  return useMemo(() => {
    if (!mission) return { mission, views: [], total: 0, start: 0 };
    const { start, portfolioId } = mission.stocks;
    const now = viewNow(status, wall);
    const isLive = status?.kind === 'live';
    const mine = isLive && tables.portfolios.some((p) => !portfolioId || p.portfolio_id === portfolioId);
    const snaps = mine
      ? tables.equity_snapshots.filter((e) => e.scope === 'PORTFOLIO' && (!portfolioId || e.scope_id === portfolioId))
      : [];
    const curve = snaps.map((e) => ({ t: Date.parse(e.ts) / 1000, v: Number(e.equity) })).sort((a, b) => a.t - b.t);
    const equity = curve.length ? curve[curve.length - 1].v : start;
    const open = mine ? positionViews(tables) : [];
    const approved = mine ? tables.risk_decisions.filter((d) => d.approved).length : 0;
    const bot = botStatus(status, tables.pause_state, now, sessionAt(now));
    const note = !isLive
      ? 'Showing a recorded replay. Switch to Live paper for this sleeve.'
      : tables.portfolios.length && !mine
        ? "The console is reading another stocks book's database. Stop it (Ctrl+C) and run npm run dev again: it switches to this book's database by itself."
        : null;
    const stocks: SleeveView = {
      id: 'stocks',
      start,
      equity,
      curve,
      openCount: open.length,
      closedCount: approved,
      wins: 0,
      tally: mine ? `${approved} ${approved === 1 ? 'entry' : 'entries'} taken` : 'not started on this account',
      statusLine: note ? 'Not started on this account' : open.length ? `${bot.label} · holding ${open.map((p) => p.symbol).join(', ')}` : bot.label,
      live: mine && bot.tone === 'running',
      note,
    };
    const views = [stocks, ...mission.practice.map((p) => practiceView(p, mission.trader))];
    return {
      mission,
      views,
      total: views.reduce((a, v) => a + v.equity, 0),
      start: views.reduce((a, v) => a + v.start, 0),
    };
  }, [mission, tables, status, wall]);
}
