import { useMemo } from 'react';
import type { SleeveId } from '../../../shared/mission';
import { dailyResults, practiceStories, stockStories, tradeStats, type DailyResults, type TradeStats, type TradeStory } from '../../../shared/trades';
import { useNow } from '../../app/hooks';
import { useConsole } from '../../app/store';
import { useMission } from '../mission/useSleeves';

export interface Trades {
  all: TradeStory[];
  bySleeve: Record<SleeveId, TradeStory[]>;
  stats: Record<SleeveId, TradeStats>;
  /** Each sleeve's result per day and its run of losing days (the reader's local days). */
  days: Record<SleeveId, DailyResults>;
  /** The three sleeves together. */
  allDays: DailyResults;
  find: (sleeve: string | undefined, id: string | undefined) => TradeStory | null;
  loading: boolean;
}

/** Every trade of the mission: the bot's stock round trips and the journalled crypto and CFD trades. */
export function useTrades(): Trades {
  const mission = useMission();
  const tables = useConsole((s) => s.tables);
  const now = useNow(60_000);
  return useMemo(() => {
    const stocks = stockStories(tables);
    const practice = mission.data ? practiceStories(mission.data.practice) : [];
    const bySleeve: Record<SleeveId, TradeStory[]> = {
      stocks,
      crypto: practice.filter((t) => t.sleeve === 'crypto'),
      cfd: practice.filter((t) => t.sleeve === 'cfd'),
    };
    const all = [...stocks, ...practice];
    return {
      all,
      bySleeve,
      stats: { stocks: tradeStats(bySleeve.stocks), crypto: tradeStats(bySleeve.crypto), cfd: tradeStats(bySleeve.cfd) },
      days: { stocks: dailyResults(bySleeve.stocks, now), crypto: dailyResults(bySleeve.crypto, now), cfd: dailyResults(bySleeve.cfd, now) },
      allDays: dailyResults(all, now),
      find: (sleeve, id) => all.find((t) => t.sleeve === sleeve && t.id === id) ?? null,
      loading: mission.isLoading,
    };
  }, [mission.data, mission.isLoading, tables, now]);
}

/** "3 h 20 min", "2 d 4 h": how long a trade was (or has been) open. */
export function heldFor(fromIso: string | null, toIso: string | null, now: number): string {
  if (!fromIso) return '-';
  const ms = (toIso ? Date.parse(toIso) : now) - Date.parse(fromIso);
  const min = Math.max(0, Math.round(ms / 60_000));
  if (min < 60) return `${min} min`;
  const h = Math.floor(min / 60);
  if (h < 48) return `${h} h ${min % 60} min`;
  return `${Math.floor(h / 24)} d ${h % 24} h`;
}
