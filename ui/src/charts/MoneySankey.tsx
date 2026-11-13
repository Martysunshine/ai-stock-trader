/**
 * Where the money is. The flows balance exactly:
 *   initial capital + deposits + realised gains  =  fees + realised losses + cash + invested (cost)
 * because cash = flows - buys + sells - fees and invested = buys - cost of what was sold.
 * Cash is split into the policy's required reserve, cash held for working orders, and free cash.
 * When a ledger entry posts, a particle travels the link it belongs to.
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { AnimatePresence, motion, useReducedMotion } from 'motion/react';
import { sankey, sankeyLinkHorizontal, type SankeyGraph } from 'd3-sankey';
import Decimal from 'decimal.js';
import type { MoneySummary } from '../../shared/derive';
import { usd } from '../design/format';
import { SPRING } from '../design/primitives';
import s from './svg.module.css';

interface N {
  id: string;
  label: string;
  color: string;
  amount: Decimal;
}
interface L {
  source: string;
  target: string;
  value: number;
  amount: Decimal;
  id: string;
}

const MIN_LINK = 0.6; // visual floor so a $0.02 fee is still a visible thread; labels carry the value

export function MoneySankey({ m, flash }: { m: MoneySummary; flash: { id: number; link: string } | null }) {
  const ref = useRef<HTMLDivElement>(null);
  const [width, setWidth] = useState(720);
  const reduce = useReducedMotion();
  useEffect(() => {
    if (!ref.current) return;
    const ro = new ResizeObserver(([e]) => setWidth(Math.max(320, e.contentRect.width)));
    ro.observe(ref.current);
    return () => ro.disconnect();
  }, []);
  const height = 300;

  const graph = useMemo(() => {
    const D = (v: string | null) => new Decimal(v ?? 0);
    const deposits = Decimal.max(D(m.externalFlows).minus(D(m.initialCapital)), 0);
    const gross = D(m.realizedGross);
    const gains = Decimal.max(gross, 0);
    const losses = Decimal.max(gross.neg(), 0);
    const cash = D(m.cash);
    const reserved = Decimal.min(D(m.reserved), Decimal.max(cash, 0));
    const reserve = Decimal.min(D(m.requiredReserve), Decimal.max(cash.minus(reserved), 0));
    const free = Decimal.max(cash.minus(reserved).minus(reserve), 0);

    const nodes: N[] = [
      { id: 'capital', label: 'Initial capital', color: 'var(--series-1)', amount: D(m.initialCapital) },
      { id: 'deposits', label: 'Deposits', color: 'var(--series-1)', amount: deposits },
      { id: 'gains', label: 'Realised gains', color: 'var(--gain-mark)', amount: gains },
      { id: 'account', label: 'Trading account', color: 'var(--ink-2)', amount: D(m.initialCapital).plus(deposits).plus(gains) },
      { id: 'fees', label: 'Fees paid', color: 'var(--series-2)', amount: D(m.fees) },
      { id: 'losses', label: 'Realised losses', color: 'var(--loss-mark)', amount: losses },
      { id: 'reserve', label: 'Required cash reserve', color: 'var(--seq-250)', amount: reserve },
      { id: 'held', label: 'Held for orders', color: 'var(--series-4)', amount: reserved },
      { id: 'free', label: 'Free cash', color: 'var(--series-1)', amount: free },
      { id: 'invested', label: 'Invested', color: 'var(--series-3)', amount: D(m.invested) },
    ];
    const linkDefs: [string, string, Decimal][] = [
      ['capital', 'account', D(m.initialCapital)],
      ['deposits', 'account', deposits],
      ['gains', 'account', gains],
      ['account', 'fees', D(m.fees)],
      ['account', 'losses', losses],
      ['account', 'reserve', reserve],
      ['account', 'held', reserved],
      ['account', 'free', free],
      ['account', 'invested', D(m.invested)],
    ];
    const links: L[] = linkDefs
      .filter(([, , v]) => v.gt(0))
      .map(([a, b, v]) => ({ source: a, target: b, amount: v, value: Math.max(MIN_LINK, v.toNumber()), id: `${a}-${b}` }));
    const used = new Set(links.flatMap((l) => [l.source, l.target]));
    const usedNodes = nodes.filter((n) => used.has(n.id));
    if (!links.length) return null;
    // Leave room for labels on both sides; the SVG renders 1:1 so text keeps its real size.
    const pad = Math.min(170, width * 0.22);
    const layout = sankey<N, L>()
      .nodeId((d) => d.id)
      .nodeWidth(10)
      .nodePadding(16)
      .nodeSort(null)
      .extent([
        [pad, 10],
        [width - pad, height - 10],
      ]);
    return layout({ nodes: usedNodes.map((n) => ({ ...n })), links: links.map((l) => ({ ...l })) }) as SankeyGraph<N, L>;
  }, [m, width]);

  const linkPath = sankeyLinkHorizontal();

  return (
    <div className={s.wrap} ref={ref}>
      {graph && (
        <svg className={s.svg} height={height} width={width} role="img" aria-label="Money flow">
          {graph.links.map((l) => {
            const d = linkPath(l) ?? '';
            const target = l.target as unknown as N;
            return (
              <g key={l.id}>
                <motion.path
                  initial={false}
                  animate={{ d, strokeWidth: Math.max(1, l.width ?? 1) }}
                  transition={SPRING}
                  fill="none"
                  stroke={target.id === 'account' ? (l.source as unknown as N).color : target.color}
                  strokeOpacity={0.22}
                >
                  <title>{`${(l.source as unknown as N).label} → ${target.label}: ${usd(l.amount.toFixed(), { precise: true })}`}</title>
                </motion.path>
                <path id={`link-${l.id}`} d={d} fill="none" stroke="none" />
              </g>
            );
          })}
          <AnimatePresence>
            {!reduce && flash && graph.links.some((l) => l.id === flash.link) && (
              <motion.circle key={flash.id} r={4} fill="var(--ink)" initial={{ opacity: 0 }} animate={{ opacity: [0, 1, 1, 0] }} exit={{ opacity: 0 }} transition={{ duration: 1.6, times: [0, 0.1, 0.85, 1] }}>
                <animateMotion dur="1.6s" repeatCount="1" fill="freeze" keyPoints="0;1" keyTimes="0;1" calcMode="spline" keySplines="0.22 1 0.36 1">
                  <mpath href={`#link-${flash.link}`} />
                </animateMotion>
              </motion.circle>
            )}
          </AnimatePresence>
          {graph.nodes.map((n) => {
            const x0 = n.x0 ?? 0;
            const x1 = n.x1 ?? 0;
            const y0 = n.y0 ?? 0;
            const y1 = n.y1 ?? 0;
            const left = x0 < width / 2;
            return (
              <g key={n.id}>
                <motion.rect initial={false} animate={{ x: x0, y: y0, height: Math.max(2, y1 - y0) }} transition={SPRING} width={x1 - x0} rx={3} fill={n.color} />
                {/* One line per node ("Fees paid  $0.02") so thin neighbouring nodes never overprint. */}
                <text
                  className={s.label}
                  x={left ? x0 - 10 : x1 + 10}
                  y={(y0 + y1) / 2}
                  textAnchor={left ? 'end' : 'start'}
                  dominantBaseline="middle"
                  style={n.id === 'account' ? { display: 'none' } : undefined}
                >
                  {left ? (
                    <>
                      <tspan>{n.label}</tspan>
                      <tspan className={s.value} dx={8}>{usd(n.amount.toFixed(), { precise: true })}</tspan>
                    </>
                  ) : (
                    <>
                      <tspan className={s.value}>{usd(n.amount.toFixed(), { precise: true })}</tspan>
                      <tspan dx={8}>{n.label}</tspan>
                    </>
                  )}
                </text>
              </g>
            );
          })}
        </svg>
      )}
    </div>
  );
}

/** Which link a ledger entry travels along. */
export function linkForLedgerKind(kind: string): string | null {
  if (kind === 'TRADE_BUY') return 'account-invested';
  if (kind === 'TRADE_SELL') return 'account-free';
  if (kind.startsWith('FEE')) return 'account-fees';
  if (kind.startsWith('FLOW_')) return 'capital-account';
  return null;
}
