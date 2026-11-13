/**
 * The three sleeves behind the Desk's goal bar (ADR-0046): stocks from the paper bot's equity
 * snapshot, crypto and CFDs from their practice journals. Read-only.
 */
import type { DeskGoalState, DeskSleeve } from '../../shared/desk';
import { SLEEVES } from '../../shared/mission';
import { missionState } from '../sources/mission';

export function readSleeves(goal: DeskGoalState): DeskSleeve[] {
  const mission = missionState();
  const { start, portfolioId } = mission.stocks;
  // A snapshot of another portfolio (the console still points at the pilot database) is not
  // this sleeve's money: show the start rather than someone else's balance.
  const mine = goal.source === 'database' && goal.equity != null && (!portfolioId || goal.portfolioId === portfolioId);
  const stocks: DeskSleeve = {
    id: 'stocks',
    label: `${SLEEVES.stocks.title} · ${SLEEVES.stocks.venue}`,
    start,
    equity: mine ? Number(goal.equity) : start,
    detail: mine
      ? 'bot equity snapshot'
      : goal.source === 'database'
        ? "the console is connected to another stocks book's database"
        : 'the bot has not run on this account yet',
  };
  const practice = mission.practice.map((p): DeskSleeve => {
    const open = p.trades.filter((t) => t.state === 'open').length;
    const closed = p.trades.filter((t) => t.state === 'closed').length;
    return {
      id: p.id,
      label: `${SLEEVES[p.id].title} · ${SLEEVES[p.id].venue}`,
      start: p.start,
      equity: p.equity,
      detail: closed + open ? `${closed} closed, ${open} open` : 'no trade yet',
    };
  });
  return [stocks, ...practice];
}
