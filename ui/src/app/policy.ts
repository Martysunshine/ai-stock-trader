import { useMemo } from 'react';
import { DEFAULT_LIMITS, type PolicyLimits } from '../../shared/derive';
import { useRules } from './api';

type Yaml = Record<string, Record<string, unknown> | unknown>;

/** Limits from configs/policy/base.yaml (the file on disk), falling back to the documented defaults. */
export function usePolicyLimits(): PolicyLimits {
  const rules = useRules();
  return useMemo(() => {
    const p = rules.data?.policy?.data as Yaml | undefined;
    if (!p) return DEFAULT_LIMITS;
    const g = (section: string, key: string) => {
      const sec = p[section] as Record<string, unknown> | undefined;
      const v = sec?.[key];
      return v === undefined || v === null ? null : String(v);
    };
    return {
      initialCapital: String(p.initial_trading_capital ?? DEFAULT_LIMITS.initialCapital),
      dailyLossFraction: g('loss_limits', 'daily_loss_fraction') ?? DEFAULT_LIMITS.dailyLossFraction,
      weeklyLossFraction: g('loss_limits', 'weekly_loss_fraction') ?? DEFAULT_LIMITS.weeklyLossFraction,
      maxDrawdownFraction: g('loss_limits', 'max_high_water_drawdown_fraction') ?? DEFAULT_LIMITS.maxDrawdownFraction,
      capitalFloor: g('loss_limits', 'initial_capital_pause_floor') ?? DEFAULT_LIMITS.capitalFloor,
      minCashReserveFraction: g('exposure', 'minimum_cash_reserve_fraction') ?? DEFAULT_LIMITS.minCashReserveFraction,
      minCashReserveAbsolute: g('exposure', 'minimum_cash_reserve_absolute') ?? DEFAULT_LIMITS.minCashReserveAbsolute,
      maxGrossExposureFraction: g('exposure', 'max_gross_exposure_fraction') ?? DEFAULT_LIMITS.maxGrossExposureFraction,
      maxSymbolExposureFraction: g('exposure', 'max_symbol_exposure_fraction') ?? DEFAULT_LIMITS.maxSymbolExposureFraction,
      maxOpenPositions: Number(g('exposure', 'max_open_positions') ?? DEFAULT_LIMITS.maxOpenPositions),
      maxNewEntriesPerSession: Number(g('exposure', 'max_new_entries_per_session') ?? DEFAULT_LIMITS.maxNewEntriesPerSession),
      riskPerTradeFraction: g('exposure', 'risk_per_trade_fraction') ?? DEFAULT_LIMITS.riskPerTradeFraction,
    };
  }, [rules.data]);
}
